"""票据解析回归：多种 PDF 版式、截图负数金额、文件名提示、文本层直送模型、哈希复用。"""

import io
import json

import pytest
from PIL import Image

from app.services import extractor
from app.services.ai_service import normalize_extraction

try:
    import pymupdf as fitz
except Exception:  # pragma: no cover
    import fitz


def make_png() -> bytes:
    image = Image.new("RGB", (500, 700), (245, 245, 245))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def make_pdf(lines: list[tuple[float, float, str]]) -> bytes:
    """按坐标写入文本，模拟真实 PDF 的版面（含左右并排栏目）。"""
    document = fitz.open()
    page = document.new_page()
    for x, y, text in lines:
        page.insert_text((x, y), text, fontname="china-s", fontsize=9)
    data = document.tobytes()
    document.close()
    return data


SIDE_BY_SIDE_INVOICE = [
    (200, 40, "电子发票（普通发票）"),
    (330, 60, "发票号码:25312000000123456789"),
    (330, 75, "开票日期:2025年11月03日"),
    (30, 100, "购买方信息  名称：上海甲科技有限公司"),
    (300, 100, "销售方信息  名称：乙酒店管理有限公司"),
    (30, 115, "统一社会信用代码/纳税人识别号:91310000MA1TEST01X"),
    (300, 115, "统一社会信用代码/纳税人识别号:91310000MA1SELL02Y"),
    (30, 140, "*住宿服务*住宿费  晚  2  283.02  566.04  6%  33.96"),
    (30, 160, "合  计  ¥566.04  ¥33.96"),
    (30, 180, "价税合计（大写）  陆佰圆整   （小写）¥600.00"),
]

RAILWAY_TICKET = [
    (200, 40, "电子发票（铁路电子客票）"),
    (330, 60, "发票号码:25119110000012345678"),
    (330, 75, "开票日期:2025年11月05日"),
    (30, 100, "上海虹桥站   G7  次   南京南站"),
    (30, 115, "2025年11月04日 08:15开   05车12F号   二等座"),
    (30, 130, "票价:¥139.50"),
    (30, 150, "购买方名称：上海甲科技有限公司   统一社会信用代码:91310000MA1TEST01X"),
]

TOLL_SUMMARY = [
    (200, 40, "收费公路通行费电子票据汇总单（按票据索引）"),
    (30, 70, "汇总单号：HZ202511030001"),
    (30, 85, "开票申请日期:2025-11-03"),
    (30, 100, "车牌号码 沪A12345"),
    (30, 115, "行程数量 6   票据数量 4"),
    (30, 130, "交易金额 ¥1,005.60"),
]

BANK_RECEIPT = [
    (200, 40, "中国工商银行 网上银行电子回单"),
    (30, 70, "付款人户名 上海甲科技有限公司   收款人户名 张三"),
    (30, 85, "交易金额 ¥8,000.00   用途 代发工资"),
    (30, 100, "交易时间 2025-11-10 10:03:12"),
]


def test_side_by_side_vat_invoice_splits_buyer_and_seller():
    text = extractor.parse_pdf_text("\n".join(t for _, _, t in SIDE_BY_SIDE_INVOICE))
    assert text["amount"] == 600.0  # 只拿到合并后的行也能认出金额

    path_text = _pdf_text(SIDE_BY_SIDE_INVOICE)
    result = extractor.parse_pdf_text(path_text)
    assert result["template"] == "vat_invoice"
    assert result["amount"] == 600.0
    assert result["buyer"] == "上海甲科技有限公司"
    assert result["seller"] == "乙酒店管理有限公司"
    assert result["buyer_tax_id"] == "91310000MA1TEST01X"
    assert result["invoice_no"] == "25312000000123456789"
    assert result["date"] == "2025年11月03日"
    assert result["net_amount"] == 566.04 and result["tax_amount"] == 33.96
    assert result["item"] == "*住宿服务*住宿费"


def test_railway_ticket_uses_fare_and_travel_date():
    result = extractor.parse_pdf_text(_pdf_text(RAILWAY_TICKET))
    assert result["template"] == "railway_ticket"
    assert result["doc_type"] == "ticket"
    assert result["amount"] == 139.5
    assert result["date"] == "2025年11月04日"
    assert result["buyer_tax_id"] == "91310000MA1TEST01X"


def test_toll_summary_is_not_confused_with_vat_invoice():
    result = extractor.parse_pdf_text(_pdf_text(TOLL_SUMMARY))
    assert result["template"] == "toll_summary"
    assert result["amount"] == 1005.6
    assert result["invoice_no"] == "HZ202511030001"


def test_non_invoice_pdf_is_left_to_ai():
    text = _pdf_text(BANK_RECEIPT)
    assert extractor.parse_pdf_text(text) == {}
    assert extractor.has_usable_text(text)


def test_filename_hints_ignore_dates():
    assert extractor.filename_hints("IMG_20261103_153012.pdf") == {}
    hints = extractor.filename_hints("dzfp_25312000000123456789_328.61元.pdf")
    assert hints == {"amount": 328.61, "invoice_no": "25312000000123456789"}


def test_payment_screenshot_negative_amount_becomes_positive():
    result = normalize_extraction({"doc_type": "payment", "amount": "-128.00", "amount_candidates": ["-150.00"]})
    assert result["amount"] == 128.0
    assert result["amount_candidates"] == [150.0, 128.0] or 128.0 in result["amount_candidates"]


def test_unknown_doc_type_falls_back_to_other_and_new_types_kept():
    assert normalize_extraction({"doc_type": "BANK"})["doc_type"] == "bank"
    assert normalize_extraction({"doc_type": "receipt"})["doc_type"] == "receipt"
    assert normalize_extraction({"doc_type": "发票"})["doc_type"] == "other"


def _pdf_text(lines) -> str:
    document = fitz.open(stream=make_pdf(lines), filetype="pdf")
    text = document[0].get_text("text", sort=True)
    document.close()
    return text


# ---------------------------------------------------------------------------
# 采集流程
# ---------------------------------------------------------------------------
def _company(client, auth, name="解析公司", tax_id=""):
    response = client.post("/api/companies", json={"name": name, "payday": 10, "tax_id": tax_id}, headers=auth)
    assert response.status_code == 200, response.text
    return response.json()


def _enable_ai(client, auth):
    saved = client.post(
        "/api/settings/ai",
        json={"enabled": True, "base_url": "https://example.invalid/v1", "api_key": "k", "model_name": "m", "system_prompt": ""},
        headers=auth,
    )
    assert saved.status_code == 200, saved.text


def test_template_pdf_skips_ai_and_matches_company_by_tax_id(client, auth, monkeypatch):
    company = _company(client, auth, "甲公司简称不在发票里", tax_id="91310000MA1TEST01X")
    _enable_ai(client, auth)
    calls = []

    async def fake_ai(**kwargs):
        calls.append(kwargs)
        return {}

    monkeypatch.setattr("app.services.capture.extract_document_with_ai", fake_ai)
    upload = client.post(
        "/api/capture",
        files=[("files", ("hotel.pdf", make_pdf(SIDE_BY_SIDE_INVOICE), "application/pdf"))],
        headers=auth,
    )
    assert upload.status_code == 200, upload.text
    draft = upload.json()["drafts"][0]
    assert calls == []
    assert draft["amount_cents"] == 60000
    assert draft["company_id"] == company["id"]
    assert draft["merchant"] == "乙酒店管理有限公司"
    assert draft["occurred_on"] == "2025-11-03"
    assert draft["doc_type"] == "invoice"


def test_text_pdf_of_unknown_style_sends_text_to_ai(client, auth, monkeypatch):
    _company(client, auth)
    _enable_ai(client, auth)
    seen = {}

    async def fake_ai(**kwargs):
        seen.update(kwargs)
        return {"doc_type": "bank", "amount": 8000.0, "date": "2025-11-10", "payer": "上海甲科技有限公司", "method": "ai_text"}

    monkeypatch.setattr("app.services.capture.extract_document_with_ai", fake_ai)
    upload = client.post(
        "/api/capture",
        files=[("files", ("回单.pdf", make_pdf(BANK_RECEIPT), "application/pdf"))],
        headers=auth,
    )
    draft = upload.json()["drafts"][0]
    assert "交易金额" in seen["text"]
    assert draft["doc_type"] == "bank"
    assert draft["amount_cents"] == 800000


def test_same_file_reuses_previous_result(client, auth, monkeypatch):
    _company(client, auth)
    _enable_ai(client, auth)
    calls = []

    async def fake_ai(**kwargs):
        calls.append(1)
        return {"doc_type": "payment", "amount": 128.0, "date": "2025-11-03", "merchant": "滴滴出行", "method": "ai_vision"}

    monkeypatch.setattr("app.services.capture.extract_document_with_ai", fake_ai)
    png = make_png()
    first = client.post("/api/capture", files=[("files", ("a.png", png, "image/png"))], headers=auth).json()["drafts"][0]
    second = client.post("/api/capture", files=[("files", ("b.png", png, "image/png"))], headers=auth).json()["drafts"][0]
    assert len(calls) == 1
    assert second["amount_cents"] == first["amount_cents"] == 12800

    # 用户主动重试时不复用
    retry = client.post(f"/api/capture/drafts/{second['id']}/retry", headers=auth)
    assert retry.status_code == 200, retry.text
    assert len(calls) == 2


def test_low_confidence_and_missing_date_produce_hints(client, auth, monkeypatch):
    _company(client, auth)
    _enable_ai(client, auth)

    async def fake_ai(**kwargs):
        return {
            "doc_type": "receipt",
            "amount": 56.0,
            "amount_candidates": [56.0, 60.0],
            "date": "",
            "confidence": {"amount": 0.4},
            "evidence": "合计 56.00",
            "method": "ai_vision",
        }

    monkeypatch.setattr("app.services.capture.extract_document_with_ai", fake_ai)
    draft = client.post("/api/capture", files=[("files", ("小票.png", make_png(), "image/png"))], headers=auth).json()["drafts"][0]
    assert draft["amount_candidates_cents"] == [6000]
    assert draft["evidence"] == "合计 56.00"
    assert len(draft["hints"]) == 2

    patched = client.patch(
        f"/api/capture/drafts/{draft['id']}", json={"amount": "60.00", "occurred_on": "2025-11-02"}, headers=auth
    ).json()
    assert patched["hints"] == []
    assert patched["amount_cents"] == 6000


def test_receipt_draft_confirms_as_receipt_attachment(client, auth, monkeypatch):
    company = _company(client, auth)
    _enable_ai(client, auth)

    async def fake_ai(**kwargs):
        return {"doc_type": "bank", "amount": 10.0, "date": "2025-11-03", "method": "ai_vision"}

    monkeypatch.setattr("app.services.capture.extract_document_with_ai", fake_ai)
    draft = client.post(
        "/api/capture", files=[("files", ("x.png", make_png(), "image/png"))], data={"company_id": str(company["id"])}, headers=auth
    ).json()["drafts"][0]
    created = client.post("/api/capture/confirm", json={"draft_ids": [draft["id"]]}, headers=auth).json()["created"][0]
    detail = client.get(f"/api/receivables/{created['id']}", headers=auth).json()
    kinds = [item["kind"] for item in detail.get("attachments", [])]
    assert kinds == ["payment_proof"]


def test_legacy_default_prompt_is_treated_as_default():
    from app.services.ai_config import is_default_prompt
    from app.services.ai_service import DEFAULT_SYSTEM_PROMPT

    assert is_default_prompt(DEFAULT_SYSTEM_PROMPT)
    assert not is_default_prompt("只识别发票")
