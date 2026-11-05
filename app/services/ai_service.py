"""视觉与文本识别：把发票、客票、小票、支付截图、订单、银行回单、工资条、聊天记录转成结构化草稿。"""

import asyncio
import base64
import io
import json
import os
import re
from typing import Optional

import httpx

try:
    from PIL import Image, ImageOps
except Exception:  # pragma: no cover
    Image = None
    ImageOps = None

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception:  # pragma: no cover
    pass

try:
    import pymupdf as fitz  # PyMuPDF
except Exception:  # pragma: no cover
    try:
        import fitz
    except Exception:
        fitz = None


DOC_TYPES = ("invoice", "ticket", "receipt", "payment", "order", "bank", "salary", "chat", "other")

DEFAULT_SYSTEM_PROMPT = """你是个人应收台账的凭证识别引擎。输入可能是各种样式的单据图片、截图或 PDF 文本，不一定是发票。只输出一个 JSON 对象，不要输出 markdown、解释或多余文字。

先判断单据类型 doc_type（只能取下列值之一），再按该类型的口径取金额：
- invoice：各类发票。增值税发票、全电发票（电子发票（普通发票）/（增值税专用发票））、定额发票、机打卷票、通行费电子票据、出租车票。金额取价税合计（小写）；定额发票取面额；出租车票取金额栏。
- ticket：交通客票与行程单。铁路电子客票取票价；航空电子客票行程单取合计（票价+民航发展基金+燃油附加费）；网约车/滴滴行程单取合计金额。
- receipt：小票、收据、POS 签购单、酒店水单、医院收费票据。取实收/合计/应付金额，不取找零、积分、会员价差。
- payment：微信、支付宝、云闪付、银行 App 的支付成功页或账单详情。取实付金额，不取原价、优惠、红包、余额；截图中常带负号，金额输出正数。
- order：外卖、电商、酒店、机票等平台的订单详情截图。取实付款，不取商品原价合计。
- bank：银行转账回单、电子回单、交易流水。取交易金额，payer 填付款方户名。
- salary：工资条、薪资通知、代发工资到账短信。amount 取实发金额，gross_amount 取应发金额，payer 填发薪单位。
- chat：聊天记录。只取明确约定要报销或支付的金额，不确定时 amount 为 null。
- other：以上都不是。

输出字段：
{
  "doc_type": "invoice",
  "doc_subtype": "具体样式，如 全电发票、微信支付账单详情、滴滴行程单、酒店水单",
  "direction": "out",
  "remark": "备注、摘要、用途、附言原文，没有为空字符串",
  "amount": 128.00,
  "amount_candidates": [128.00, 150.00],
  "date": "YYYY-MM-DD",
  "merchant": "收款方或商户名称",
  "item": "费用项目或商品服务名称",
  "title": "不超过 20 字的摘要，如 滴滴打车 机场-公司",
  "invoice_code": "仅发票填写发票代码，否则为空字符串",
  "invoice_no": "仅发票或客票填写票号，否则为空字符串",
  "order_no": "交易单号、订单号或流水号，没有为空字符串",
  "buyer": "发票购买方名称（抬头）",
  "buyer_tax_id": "购买方纳税人识别号",
  "seller": "销售方名称",
  "payer": "付款方或发薪单位",
  "gross_amount": null,
  "net_amount": null,
  "confidence": {"amount": 0.0, "date": 0.0},
  "evidence": "金额对应的原文片段",
  "entries": []
}

规则：
1. direction 判断资金方向：钱进到我的账户（工资到账、报销到账、退款）填 in；我付出去（消费、缴费、转账给他人）填 out；看不出来填 out。
2. 银行回单、交易明细、到账截图：remark 原样抄写备注栏、摘要、用途或附言，例如「6,7月工资」「XX公司报销款」。
3. 日期取交易、开票或乘车日期，不取截图时间或打印时间；统一为 YYYY-MM-DD。
4. 一份材料里有多笔独立交易（如账单列表、多张小票拼图）时，entries 逐笔输出 {"date","merchant","item","amount","direction","remark"}，amount 填合计。
5. amount_candidates 列出画面中其它可能是费用金额的数字，便于人工核对，不要放余额、单价、税额。
6. 看不清或不确定的字段填 null 或空字符串，禁止编造；confidence 如实给出 0 到 1 的把握。
7. 金额单位人民币元，保留两位小数，一律输出正数。"""


def _normalize_endpoint(base_url: str) -> str:
    url = (base_url or "").strip().rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    return url + "/chat/completions"


def _image_to_jpeg_bytes(image: "Image.Image", max_edge: int = 1800) -> bytes:
    """压缩图片，控制长边和体积。"""
    image = ImageOps.exif_transpose(image)
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    width, height = image.size
    scale = min(1.0, float(max_edge) / max(width, height))
    if scale < 1.0:
        image = image.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=86, optimize=True)
    return buffer.getvalue()


def _load_image_segments(file_path: str) -> list[bytes]:
    """把图片转成若干 JPEG 片段；超长截图自动切片，避免文字被压没。"""
    if Image is None:
        return []
    try:
        with Image.open(file_path) as image:
            image = ImageOps.exif_transpose(image)
            width, height = image.size
            if width <= 0 or height <= 0:
                return []
            # 极长截图：按高度切片，最多 4 段
            if height / max(width, 1) > 3.5:
                segment_height = max(width * 2, 800)
                count = min(4, max(1, (height + segment_height - 1) // segment_height))
                segment_height = (height + count - 1) // count
                segments = []
                for index in range(count):
                    top = index * segment_height
                    bottom = min(height, top + segment_height)
                    if bottom <= top:
                        break
                    crop = image.crop((0, top, width, bottom))
                    segments.append(_image_to_jpeg_bytes(crop, max_edge=1800))
                return segments
            return [_image_to_jpeg_bytes(image, max_edge=1800)]
    except Exception:
        return []


def _load_pdf_segments(file_path: str) -> list[bytes]:
    if fitz is None or Image is None:
        return []
    segments = []
    try:
        with fitz.open(file_path) as document:
            for page_index in range(min(len(document), 3)):
                page = document.load_page(page_index)
                pixmap = page.get_pixmap(dpi=150, alpha=False)
                image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
                segments.append(_image_to_jpeg_bytes(image, max_edge=1800))
    except Exception:
        return segments
    return segments


def _load_segments(file_path: str, mime_hint: str = "") -> list[bytes]:
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".pdf" or "pdf" in (mime_hint or ""):
        return _load_pdf_segments(file_path)
    return _load_image_segments(file_path)


def _extract_json(content: str) -> dict:
    if not content:
        return {}
    text = content.strip()
    text = text.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _coerce_amount(value) -> Optional[float]:
    """金额统一为正数：支付截图常带负号表示支出。"""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return abs(float(value))
    text = str(value).replace(",", "").replace("￥", "").replace("¥", "")
    text = re.sub(r"[^\d.]", "", text)
    if not text:
        return None
    try:
        return abs(float(text))
    except ValueError:
        return None


def _coerce_confidence(value) -> dict:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, raw in value.items():
        try:
            result[str(key)] = max(0.0, min(1.0, float(raw)))
        except (TypeError, ValueError):
            continue
    return result


TEXT_FIELDS = (
    "date",
    "merchant",
    "item",
    "title",
    "invoice_code",
    "invoice_no",
    "order_no",
    "buyer",
    "buyer_tax_id",
    "seller",
    "seller_tax_id",
    "payer",
    "remark",
    "evidence",
    "doc_type",
    "doc_subtype",
)


def normalize_extraction(data: dict) -> dict:
    """把模型输出规范化为稳定结构，过滤掉不可用字段。"""
    if not isinstance(data, dict):
        return {}
    result = dict(data)
    result["amount"] = _coerce_amount(data.get("amount"))
    result["gross_amount"] = _coerce_amount(data.get("gross_amount"))
    result["net_amount"] = _coerce_amount(data.get("net_amount"))

    candidates = []
    for item in data.get("amount_candidates") or []:
        value = _coerce_amount(item)
        if value is not None and value > 0 and value not in candidates:
            candidates.append(value)
    if result["amount"] and result["amount"] > 0 and result["amount"] not in candidates:
        candidates.insert(0, result["amount"])
    result["amount_candidates"] = candidates[:5]

    entries = []
    for entry in data.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        normalized = dict(entry)
        normalized["amount"] = _coerce_amount(entry.get("amount"))
        if normalized["amount"] and normalized["amount"] > 0:
            entries.append(normalized)
    result["entries"] = entries

    for key in TEXT_FIELDS:
        value = data.get(key)
        result[key] = "" if value is None else str(value).strip()

    result["doc_type"] = result["doc_type"].lower()
    if result["doc_type"] not in DOC_TYPES:
        result["doc_type"] = "other"
    direction = str(data.get("direction") or "").strip().lower()
    result["direction"] = "in" if direction in ("in", "income", "收入", "进账", "到账") else "out"
    result["confidence"] = _coerce_confidence(data.get("confidence"))
    return result


_clients: dict = {}


def _http_client() -> httpx.AsyncClient:
    """复用连接池；按事件循环区分，避免跨循环复用连接。"""
    loop = asyncio.get_running_loop()
    client = _clients.get(id(loop))
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=10.0))
        _clients[id(loop)] = client
    return client


async def close_http_clients() -> None:
    for client in list(_clients.values()):
        try:
            await client.aclose()
        except Exception:
            pass
    _clients.clear()


async def _chat_json(base_url: str, api_key: str, model_name: str, system_prompt: str, user_content) -> dict:
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.1,
        "max_tokens": 1500,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        response = await _http_client().post(_normalize_endpoint(base_url), json=payload, headers=headers)
        if response.status_code != 200:
            return {"_error": f"AI 接口返回 HTTP {response.status_code}: {response.text[:300]}"}
        body = response.json()
        data = _extract_json(body["choices"][0]["message"]["content"])
        if not data:
            return {"_error": "AI 未返回可解析的 JSON"}
        return normalize_extraction(data)
    except Exception as exc:
        return {"_error": f"AI 识别失败: {exc}"}


async def extract_document_with_ai(
    file_path: str,
    base_url: str,
    api_key: str,
    model_name: str,
    system_prompt: Optional[str] = None,
    mime_hint: str = "",
    text: str = "",
) -> dict:
    """调用 OpenAI 兼容接口识别凭证。

    传入 text（PDF 文本层）时先只发文本，便宜且快；文本里认不出金额再渲染成图片走视觉识别。
    """
    if not (base_url and api_key and model_name):
        return {}
    effective_prompt = (system_prompt or "").strip() or DEFAULT_SYSTEM_PROMPT

    text_error = ""
    if text and text.strip():
        result = await _chat_json(
            base_url,
            api_key,
            model_name,
            effective_prompt,
            "下面是一份 PDF 凭证的文本层，已按版面从上到下、从左到右排序，左右并排的栏目用多个空格分隔。请识别并按约定 JSON 输出。\n\n"
            + text[:8000],
        )
        if result.get("amount"):
            result["method"] = "ai_text"
            return result
        text_error = result.get("_error", "")

    segments = await asyncio.to_thread(_load_segments, file_path, mime_hint)
    if not segments:
        return {"_error": text_error or "无法读取文件内容，请确认是有效的图片或 PDF"}

    content = [
        {
            "type": "text",
            "text": "请识别图片中的凭证并按约定 JSON 输出。多张图片属于同一份材料的不同页或不同片段。",
        }
    ]
    for segment in segments:
        encoded = base64.b64encode(segment).decode("utf-8")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})

    result = await _chat_json(base_url, api_key, model_name, effective_prompt, content)
    if not result.get("_error"):
        result["method"] = "ai_vision"
    return result


async def test_ai_connection(base_url: str, api_key: str, model_name: str) -> dict:
    if not base_url or not api_key or not model_name:
        return {"success": False, "message": "接口地址、密钥和模型名称都不能为空"}
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": "Reply with pong only."}],
        "max_tokens": 10,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(_normalize_endpoint(base_url), json=payload, headers=headers)
            if response.status_code == 200:
                return {"success": True, "message": "连接成功，AI 接口正常响应"}
            return {"success": False, "message": f"连接失败，HTTP {response.status_code}: {response.text[:200]}"}
    except Exception as exc:
        return {"success": False, "message": f"连接异常: {exc}"}


async def fetch_available_models(base_url: str, api_key: str) -> dict:
    if not base_url or not api_key:
        return {"success": False, "message": "接口地址和密钥不能为空", "models": []}
    url = base_url.strip().rstrip("/")
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
    if not url.endswith("/models"):
        url += "/models"
    headers = {"Authorization": f"Bearer {api_key.strip()}"}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(url, headers=headers)
            if response.status_code != 200:
                return {"success": False, "message": f"服务商返回 HTTP {response.status_code}", "models": []}
            data = response.json()
            models = []
            for item in data.get("data", []):
                if isinstance(item, dict) and item.get("id"):
                    models.append(str(item["id"]))
                elif isinstance(item, str):
                    models.append(item)

            def priority(name: str):
                lower = name.lower()
                if any(key in lower for key in ("4o", "vl", "vision", "gemini", "claude")):
                    return (0, lower)
                return (1, lower)

            models = sorted(set(models), key=priority)
            return {"success": True, "message": f"获取到 {len(models)} 个模型", "models": models}
    except Exception as exc:
        return {"success": False, "message": f"获取模型失败: {exc}", "models": []}
