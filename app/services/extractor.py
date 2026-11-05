"""PDF 文本层本地解析。

只处理标签稳定、能精确定位金额的版式（增值税/全电发票、铁路电子客票、通行费汇总单）。
其它版式（支付截图导出、银行回单、行程单、各类小票）一律返回空，交给 AI 识别。
"""

import fnmatch
import json
import re

try:
    import pymupdf as fitz  # PyMuPDF
except Exception:  # pragma: no cover
    try:
        import fitz
    except Exception:
        fitz = None

MAX_TEXT_PAGES = 5
LOCAL_CONFIDENCE = 0.99


def is_valid_file_by_pattern(filename: str, patterns_str: str) -> bool:
    """
    检查文件名是否符合该分类的有效文件名模式。
    如果未设置模式（为空或空列表），默认所有文件均有效；
    支持 JSON 列表格式（如 ["*.pdf"]）或逗号分隔格式。
    """
    if not patterns_str or not str(patterns_str).strip():
        return True

    filename_lower = filename.lower()
    patterns = []
    text = str(patterns_str).strip()
    if text.startswith("[") and text.endswith("]"):
        try:
            arr = json.loads(text)
            patterns = [str(p).strip().lower() for p in arr if str(p).strip()]
        except Exception:
            patterns = [p.strip().lower() for p in text.strip("[]").split(",") if p.strip()]
    else:
        patterns = [p.strip().lower() for p in text.split(",") if p.strip()]

    if not patterns:
        return True

    for p in patterns:
        if fnmatch.fnmatch(filename_lower, p):
            return True
    return False


def extract_pdf_text(pdf_path: str, max_pages: int = MAX_TEXT_PAGES) -> str:
    """按版面坐标排序提取文本层，左右并排的栏目会留在同一行并以多个空格分隔。"""
    if fitz is None:
        return ""
    try:
        with fitz.open(pdf_path) as document:
            parts = []
            for index in range(min(len(document), max_pages)):
                text = document.load_page(index).get_text("text", sort=True)
                if text and text.strip():
                    parts.append(text)
            return "\n".join(parts)
    except Exception:
        return ""


def has_usable_text(text: str) -> bool:
    """文本层是否足以直接交给模型（扫描件通常没有文本层或只有零星字符）。"""
    compact = re.sub(r"\s+", "", text or "")
    return len(compact) >= 30 and bool(re.search(r"\d", compact))


def parse_clean_amount(val_str: str) -> float:
    """清洗金额字符串（去除货币符号、千分位逗号与空格）并转换为浮点数"""
    if not val_str:
        return 0.0
    cleaned = re.sub(r"[^\d.]", "", str(val_str).replace(",", "").strip())
    try:
        return float(cleaned)
    except Exception:
        return 0.0


AMOUNT = r"([\d,]+(?:\.\d{1,2})?)"
NAME_STOP = r"(?=\s{2,}|\s*名\s*称\s*[：:]|\s*(?:统一社会信用代码|纳税人识别号)|\s*$)"
TAX_ID_PATTERN = (
    r"(?:统一社会信用代码\s*/\s*纳税人识别号|纳税人识别号|统一社会信用代码)\s*[：:]\s*([0-9A-Z]{15,20})"
)


def _search(pattern: str, text: str, flags: int = 0):
    return re.search(pattern, text, flags)


def _first(pattern: str, text: str, flags: int = 0) -> str:
    match = re.search(pattern, text, flags)
    return match.group(1).strip() if match else ""


def _parties(text: str) -> tuple[list[str], list[str]]:
    """名称与税号按出现顺序排列：购买方在前，销售方在后（竖排与左右并排两种版式都成立）。"""
    names = [
        item.strip(" ：:")
        for item in re.findall(r"名\s*称\s*[：:]\s*(.+?)" + NAME_STOP, text, re.M)
        if item.strip(" ：:")
    ]
    tax_ids = re.findall(TAX_ID_PATTERN, text)
    return names, tax_ids


def _issue_date(text: str) -> str:
    raw = _first(
        r"开\s*票\s*日\s*期\s*[：:]?\s*(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}-\d{1,2}-\d{1,2})",
        text,
    )
    return raw.replace(" ", "")


def _invoice_no(text: str) -> str:
    return _first(r"发\s*票\s*号\s*码\s*[：:]?\s*(\d{8,20})", text)


def _base(template: str, doc_type: str, subtype: str) -> dict:
    return {
        "doc_type": doc_type,
        "doc_subtype": subtype,
        "template": template,
        "amount": None,
        "date": "",
        "merchant": "",
        "item": "",
        "title": "",
        "invoice_code": "",
        "invoice_no": "",
        "buyer": "",
        "buyer_tax_id": "",
        "seller": "",
        "seller_tax_id": "",
        "net_amount": None,
        "tax_amount": None,
        "confidence": {"amount": LOCAL_CONFIDENCE},
        "evidence": "",
        "method": "native_pdf",
    }


def parse_toll_summary(text: str, filename: str = "") -> dict:
    """收费公路通行费电子票据汇总单。"""
    if "通行费" not in text or not ("汇总单" in text or "交易金额" in text):
        return {}
    match = _search(r"交\s*易\s*金\s*额\s*[：:]?\s*[¥￥]?\s*" + AMOUNT, text)
    if not match:
        match = _search(r"[（(]\s*小\s*写\s*[）)]\s*[¥￥]?\s*" + AMOUNT, text)
    if not match:
        return {}
    amount = parse_clean_amount(match.group(1))
    if amount <= 0:
        return {}

    result = _base("toll_summary", "invoice", "通行费电子票据汇总单")
    result["amount"] = amount
    result["evidence"] = match.group(0).strip()
    result["date"] = _first(r"开票申请日期\s*[：:]?\s*([\d\-]+)", text)
    result["buyer"] = _first(r"购买方名称\s*[：:]?\s*(\S+)", text)
    tax = _first(r"税\s*额[^\n\r]*?[¥￥]\s*" + AMOUNT, text)
    result["tax_amount"] = parse_clean_amount(tax) if tax else None

    trips = _first(r"行程数量\s*[：:]?\s*(\d+)", text) or _first(r"共(\d+)段行程", text)
    tickets = _first(r"票据数量\s*[：:]?\s*(\d+)", text)
    plate = _first(r"车牌号码\s*[：:]?\s*(\S+)", text)
    result["plate"] = plate
    result["is_trip_detail"] = "按行程索引" in text or "(行程)" in filename or "（行程）" in filename
    result["seller"] = "全国收费公路通行费"
    result["merchant"] = result["seller"]
    result["item"] = f"通行费 (车牌:{plate}, 行程:{trips or 0}段, 票据:{tickets or 0}张)"
    result["title"] = "通行费"

    summary_no = _first(r"汇总单号\s*[：:]?\s*([A-Za-z0-9]+)", text)
    if summary_no:
        result["invoice_no"] = summary_no
    elif plate and result["date"]:
        # 票据单与行程单生成相同的确定性编号，用于防重
        result["invoice_no"] = f"TOLL_{plate}_{result['date'].replace('-', '')}_{int(round(amount * 100))}"
    return result


def parse_railway_ticket(text: str) -> dict:
    """电子发票（铁路电子客票）。"""
    if "铁路电子客票" not in text:
        return {}
    match = _search(r"票\s*价\s*[：:]?\s*[¥￥]\s*" + AMOUNT, text)
    if not match:
        return {}
    amount = parse_clean_amount(match.group(1))
    if amount <= 0:
        return {}

    result = _base("railway_ticket", "ticket", "铁路电子客票")
    result["amount"] = amount
    result["evidence"] = match.group(0).strip()
    travel = _first(r"(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)\s*\d{1,2}:\d{2}\s*开", text)
    result["date"] = travel.replace(" ", "") or _issue_date(text)
    result["invoice_no"] = _invoice_no(text)
    result["buyer"] = _first(r"购买方名称\s*[：:]\s*(\S+)", text)
    result["buyer_tax_id"] = _first(r"统一社会信用代码\s*[：:]\s*([0-9A-Z]{15,20})", text)
    result["seller"] = "中国国家铁路集团有限公司"
    result["merchant"] = "铁路客票"
    result["item"] = "火车票"
    result["title"] = "火车票"
    return result


def parse_vat_invoice(text: str) -> dict:
    """增值税发票与全电发票（含价税合计小写）。"""
    match = _search(r"[（(]\s*小\s*写\s*[）)]\s*[¥￥]?\s*" + AMOUNT, text)
    if not match:
        return {}
    amount = parse_clean_amount(match.group(1))
    if amount <= 0:
        return {}

    subtype = _first(
        r"(电子发票\s*[（(][^）)\n]{2,16}[）)]|增值税电子(?:普通|专用)发票|增值税(?:普通|专用)发票)",
        text,
    )
    result = _base("vat_invoice", "invoice", re.sub(r"\s+", "", subtype) or "发票")
    result["amount"] = amount
    result["evidence"] = match.group(0).strip()
    result["date"] = _issue_date(text)
    result["invoice_no"] = _invoice_no(text)
    result["invoice_code"] = _first(r"发\s*票\s*代\s*码\s*[：:]?\s*(\d{10,12})", text)

    names, tax_ids = _parties(text)
    if names:
        result["buyer"] = names[0]
    if len(names) > 1:
        result["seller"] = names[1]
    if tax_ids:
        result["buyer_tax_id"] = tax_ids[0]
    if len(tax_ids) > 1:
        result["seller_tax_id"] = tax_ids[1]
    result["merchant"] = result["seller"]

    totals = _search(r"合\s*计\s*[¥￥]?\s*([\d,]+\.\d{2})\s+[¥￥]?\s*([\d,]+\.\d{2})", text)
    if totals:
        result["net_amount"] = parse_clean_amount(totals.group(1))
        result["tax_amount"] = parse_clean_amount(totals.group(2))

    item = _search(r"\*[^*\s]{1,20}\*[^\s]*", text)
    if item:
        result["item"] = item.group(0)
    result["title"] = result["item"] or result["seller"] or "发票"
    return result


TEMPLATES = (parse_toll_summary, parse_railway_ticket, parse_vat_invoice)


def parse_pdf_text(text: str, filename: str = "") -> dict:
    """按模板顺序匹配；都不命中返回空字典。"""
    if not text:
        return {}
    for template in TEMPLATES:
        try:
            result = template(text, filename) if template is parse_toll_summary else template(text)
        except Exception:
            result = {}
        if result and result.get("amount"):
            return result
    return {}


def filename_hints(filename: str) -> dict:
    """文件名里的金额与 20 位全电发票号，只作为低置信候选。"""
    name = filename or ""
    hints = {}
    amount = re.search(r"[_\-\s]([\d,]+\.\d{1,2})元", name)
    if amount:
        value = parse_clean_amount(amount.group(1))
        if value > 0:
            hints["amount"] = value
    number = re.search(r"(?<!\d)(\d{20})(?!\d)", name)
    if number:
        hints["invoice_no"] = number.group(1)
    return hints
