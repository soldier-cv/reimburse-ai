"""银行到账附言拆单核销：AI 拆明细 → 匹配已提交报销应收 → 生成一笔核销。"""

import io

from PIL import Image

from app.models import Receivable, User


def _make_png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (400, 600), (245, 245, 245)).save(buffer, format="PNG")
    return buffer.getvalue()


def _enable_ai(client, auth):
    saved = client.post(
        "/api/settings/ai",
        json={
            "enabled": True,
            "base_url": "https://example.invalid/v1",
            "api_key": "k",
            "model_name": "m",
            "system_prompt": "",
        },
        headers=auth,
    )
    assert saved.status_code == 200, saved.text


def _company(client, auth, name="杭州识微科技有限公司"):
    response = client.post("/api/companies", json={"name": name, "payday": 15}, headers=auth)
    assert response.status_code == 200, response.text
    return response.json()


def _category(client, auth, name):
    categories = client.get("/api/categories", headers=auth).json()
    for category in categories:
        if category["name"] == name:
            return category
    raise AssertionError(f"缺少默认类别: {name}")


def _submitted_receivable(test_db, user, company_id, category_id, title, period, cents, kind="reimbursement"):
    item = Receivable(
        user_id=user.id,
        company_id=company_id,
        category_id=category_id,
        kind=kind,
        title=title,
        period=period,
        occurred_on=f"{period}-05",
        amount_cents=cents,
        status="submitted",
        submitted_on=f"{period}-05",
    )
    test_db.add(item)
    test_db.commit()
    test_db.refresh(item)
    return item


def _bank_entries(total=2190.00):
    return {
        "doc_type": "bank",
        "direction": "in",
        "amount": total,
        "date": "2026-05-09",
        "payer": "杭州识微科技有限公司",
        "remark": "通行费：2月512.30、3月876.5、4月801.20",
        "entries": [
            {"period": "2026-02", "item": "通行费", "amount": 512.30, "confidence": 0.9, "direction": "in"},
            {"period": "2026-03", "item": "通行费", "amount": 876.50, "confidence": 0.9, "direction": "in"},
            {"period": "2026-04", "item": "通行费", "amount": 801.20, "confidence": 0.9, "direction": "in"},
        ],
        "method": "ai_vision",
    }


def _capture(client, auth, monkeypatch, extracted):
    async def fake_ai(**kwargs):
        return extracted

    monkeypatch.setattr("app.services.capture.extract_document_with_ai", fake_ai)
    response = client.post(
        "/api/capture",
        files=[("files", ("bank.png", _make_png(), "image/png"))],
        headers=auth,
    )
    assert response.status_code == 200, response.text
    return response.json()["drafts"][0]


def test_normalize_extraction_keeps_entry_period_and_confidence():
    from app.services.ai_service import normalize_extraction

    data = normalize_extraction(
        {
            "doc_type": "bank",
            "direction": "in",
            "amount": 2190.00,
            "entries": [
                {"period": "2026年2月", "item": "通行费", "amount": "512.30", "confidence": 0.8, "direction": "in"},
                {"period": "bad", "item": "汽油费", "amount": 100, "confidence": "x"},
            ],
        }
    )
    assert len(data["entries"]) == 2
    assert data["entries"][0]["period"] == "2026-02"
    assert data["entries"][0]["confidence"] == 0.8
    assert data["entries"][1]["period"] == ""
    assert data["entries"][1]["confidence"] == 0.0


def test_entries_are_allocated_to_submitted_receivables(client, auth, test_db, monkeypatch):
    _enable_ai(client, auth)
    company = _company(client, auth)
    category = _category(client, auth, "城市交通")
    user = test_db.query(User).first()
    items = [
        _submitted_receivable(test_db, user, company["id"], category["id"], f"{month}月通行费", f"2026-{month:02d}", cents)
        for month, cents in ((2, 51230), (3, 87650), (4, 80120))
    ]

    draft = _capture(client, auth, monkeypatch, _bank_entries())
    match = draft["reimbursement_match"]
    assert match is not None
    assert draft["payroll_match"] is None
    assert len(match["allocations"]) == 3
    assert match["issues"] == []
    assert match["total_cents"] == 219000

    applied = client.post(
        f"/api/capture/drafts/{draft['id']}/apply-payment",
        json={},
        headers=auth,
    )
    assert applied.status_code == 200, applied.text
    assert len(applied.json()["allocations"]) == 3

    payments = client.get("/api/payments", headers=auth).json()
    assert len(payments) == 1
    assert payments[0]["amount"] == 2190.00

    for item in items:
        detail = client.get(f"/api/receivables/{item.id}", headers=auth).json()
        assert detail["status"] == "paid"
        assert detail["remaining_cents"] == 0
        assert len(detail["attachments"]) == 1
        assert detail["attachments"][0]["kind"] == "payment_proof"


def test_unmatched_entry_blocks_whole_payment(client, auth, test_db, monkeypatch):
    _enable_ai(client, auth)
    company = _company(client, auth)
    category = _category(client, auth, "城市交通")
    user = test_db.query(User).first()
    for month, cents in ((2, 51230), (3, 87650)):
        _submitted_receivable(test_db, user, company["id"], category["id"], f"{month}月通行费", f"2026-{month:02d}", cents)

    draft = _capture(client, auth, monkeypatch, _bank_entries())
    match = draft["reimbursement_match"]
    assert len(match["allocations"]) == 2
    assert any("2026-04" in issue and "通行费" in issue for issue in match["issues"])

    applied = client.post(
        f"/api/capture/drafts/{draft['id']}/apply-payment",
        json={},
        headers=auth,
    )
    assert applied.status_code == 400
    assert "2026-04" in applied.json()["detail"]["proposal"]["issues"][0]
    assert client.get("/api/payments", headers=auth).json() == []


def test_pending_receivable_cannot_be_used_by_explicit_allocation(client, auth, test_db, monkeypatch):
    _enable_ai(client, auth)
    company = _company(client, auth)
    category = _category(client, auth, "城市交通")
    user = test_db.query(User).first()
    for month, cents in ((2, 51230), (3, 87650), (4, 80120)):
        _submitted_receivable(test_db, user, company["id"], category["id"], f"{month}月通行费", f"2026-{month:02d}", cents)
    pending = Receivable(
        user_id=user.id,
        company_id=company["id"],
        category_id=category["id"],
        kind="reimbursement",
        title="未提交通行费",
        period="2026-04",
        amount_cents=80120,
        status="pending",
    )
    test_db.add(pending)
    test_db.commit()
    test_db.refresh(pending)

    draft = _capture(client, auth, monkeypatch, _bank_entries())
    applied = client.post(
        f"/api/capture/drafts/{draft['id']}/apply-payment",
        json={
            "allocations": [
                {"receivable_id": pending.id, "amount": 801.20},
                {"receivable_id": draft["reimbursement_match"]["allocations"][0]["receivable_id"], "amount": 1418.82},
            ]
        },
        headers=auth,
    )
    assert applied.status_code == 400
    assert any("已提交" in issue for issue in applied.json()["detail"]["issues"])


def test_salary_inflow_still_uses_payroll_match(client, auth, test_db, monkeypatch):
    _enable_ai(client, auth)
    company = _company(client, auth, "发薪公司")
    category = _category(client, auth, "工资")
    user = test_db.query(User).first()
    salary = _submitted_receivable(test_db, user, company["id"], category["id"], "3月工资", "2026-03", 2000000, kind="payroll")

    extracted = {
        "doc_type": "salary",
        "direction": "in",
        "amount": 15000.0,
        "date": "2026-04-10",
        "payer": "发薪公司",
        "remark": "3月工资",
        "entries": [],
        "method": "ai_vision",
    }
    draft = _capture(client, auth, monkeypatch, extracted)
    assert draft["payroll_match"] is not None
    assert draft["payroll_match"]["allocations"]
    assert draft["reimbursement_match"] is None

    applied = client.post(
        f"/api/capture/drafts/{draft['id']}/apply-payment",
        json={},
        headers=auth,
    )
    assert applied.status_code == 200, applied.text
    detail = client.get(f"/api/receivables/{salary.id}", headers=auth).json()
    assert detail["status"] == "paid"
    assert detail["received_cents"] == 1500000
    assert detail["waived_cents"] == 500000
