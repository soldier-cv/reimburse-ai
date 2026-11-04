"""多模态视觉识别：把发票、支付截图、工资条、聊天记录转成结构化草稿。"""

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


DEFAULT_SYSTEM_PROMPT = """你是个人应收台账的票据识别引擎。请从图片或 PDF 中提取财务信息，并且只输出一个标准 JSON 对象，不要输出 markdown、解释或多余文字。

输出字段：
{
  "doc_type": "invoice | payment | salary | chat | other",
  "amount": 128.00,
  "amount_candidates": [128.00, 1280.00],
  "date": "2026-11-03",
  "merchant": "收款方或商户名称",
  "item": "费用项目或商品服务名称",
  "title": "一句话摘要",
  "invoice_code": "发票代码，没有则为空字符串",
  "invoice_no": "发票号码，没有则为空字符串",
  "buyer": "发票购买方名称",
  "seller": "销售方名称",
  "gross_amount": null,
  "net_amount": null,
  "confidence": {"amount": 0.0, "date": 0.0},
  "evidence": "金额对应的原文片段",
  "entries": []
}

识别规则：
1. 微信/支付宝支付截图：amount 取实付金额，不要取优惠前金额、余额或红包金额。
2. 电子发票：amount 取价税合计；同时提取发票代码、号码、购买方、销售方。
3. 工资条：amount 取实发金额，gross_amount 取应发金额。
4. 聊天记录：只提取明确提到的金额；无法确定时 amount 为 null，置信度给低。
5. 一张图里有多笔独立交易时，entries 输出每笔的对象数组，amount 为这些交易的合计。
6. 看不清或不确定的字段必须填 null 或空字符串，禁止编造。
7. 日期统一为 YYYY-MM-DD。
8. 金额单位是人民币元，保留两位小数。"""


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
    if fitz is None:
        return []
    segments = []
    try:
        document = fitz.open(file_path)
        for page_index in range(min(len(document), 3)):
            page = document.load_page(page_index)
            pixmap = page.get_pixmap(dpi=150, alpha=False)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            segments.append(_image_to_jpeg_bytes(image, max_edge=1800))
        document.close()
    except Exception:
        return segments
    return segments


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
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").replace("￥", "").replace("¥", "")
    text = re.sub(r"[^\d.\-]", "", text)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


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

    for key in (
        "date",
        "merchant",
        "item",
        "title",
        "invoice_code",
        "invoice_no",
        "buyer",
        "seller",
        "evidence",
        "doc_type",
    ):
        value = data.get(key)
        result[key] = "" if value is None else str(value).strip()

    if not result.get("doc_type"):
        result["doc_type"] = "other"
    if result["doc_type"] not in ("invoice", "payment", "salary", "chat", "other"):
        result["doc_type"] = "other"
    if not isinstance(result.get("confidence"), dict):
        result["confidence"] = {}
    return result


async def extract_document_with_ai(
    file_path: str,
    base_url: str,
    api_key: str,
    model_name: str,
    system_prompt: Optional[str] = None,
    mime_hint: str = "",
) -> dict:
    """调用 OpenAI 兼容的多模态接口，返回规范化后的结构化结果。"""
    if not (base_url and api_key and model_name):
        return {}

    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".pdf" or "pdf" in (mime_hint or ""):
        segments = _load_pdf_segments(file_path)
    else:
        segments = _load_image_segments(file_path)
    if not segments:
        return {"_error": "无法读取文件内容，请确认是有效的图片或 PDF"}

    effective_prompt = (system_prompt or "").strip() or DEFAULT_SYSTEM_PROMPT
    content = [
        {
            "type": "text",
            "text": "请识别这些图片中的财务单据，并按约定 JSON 输出。多张图片属于同一份单据的不同页或不同片段。",
        }
    ]
    for segment in segments:
        encoded = base64.b64encode(segment).decode("utf-8")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
            }
        )

    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": effective_prompt},
            {"role": "user", "content": content},
        ],
        "temperature": 0.1,
        "max_tokens": 1200,
    }

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(_normalize_endpoint(base_url), json=payload, headers=headers)
            if response.status_code != 200:
                return {"_error": f"AI 接口返回 HTTP {response.status_code}: {response.text[:300]}"}
            body = response.json()
            text = body["choices"][0]["message"]["content"]
            data = _extract_json(text)
            if not data:
                return {"_error": "AI 未返回可解析的 JSON"}
            data["method"] = "ai_ocr"
            return normalize_extraction(data)
    except Exception as exc:
        return {"_error": f"AI 识别失败: {exc}"}


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
