"""工资到账：归属月推断与一键核销。"""

import io

from PIL import Image

from app.models import Receivable, User
from app.services.payroll_match import parse_month_mentions, propose_allocation


def make_png() -> bytes:
    image = Image.new("RGB", (400, 600), (250, 250, 250))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _company(client, auth, name="工资公司"):
    response = client.post("/api/companies", json={"name": name, "payday": 10}, headers=auth)
    assert response.status_code == 200, response.text
    return response.json()


def _enable_ai(client, auth):
    saved = client.post(
        "/api/settings/ai",
        json={"enabled": True, "base_url": "https://example.invalid/v1", "api_key": "k", "model_name": "m", "system_prompt": ""},
        headers=auth,
    )
    assert saved.status_code == 200, saved.text


def _payroll(db, company_id, period, amount_cents, status="pending"):
    user = db.query(User).first()
    item = Receivable(
        user_id=user.id,
        company_id=company_id,
        kind="payroll",
        title=f"{period} 工资",
        period=period,
        occurred_on=f"{period}-05",
        amount_cents=amount_cents,
        status=status,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _draft_from_screenshot(client, auth, company_id, ai_result, monkeypatch):
    _enable_ai(client, auth)

    async def fake_ai(**kwargs):
        return dict(ai_result)

    monkeypatch.setattr("app.services.capture.extract_document_with_ai", fake_ai)
    upload = client.post(
        "/api/capture",
        files=[("files", ("salary.png", make_png(), "image/png"))],
        data={"company_id": str(company_id)},
        headers=auth,
    )
    assert upload.status_code == 200, upload.text
    return upload.json()["drafts"][0]


# ---------------------------------------------------------------------------
# 月份解析
# ---------------------------------------------------------------------------
def test_parse_months_from_remark():
    assert parse_month_mentions("6,7月工资", "2025-11-05") == ["2025-06", "2025-07"]
    assert parse_month_mentions("6、7月工资", "2025-11-05") == ["2025-06", "2025-07"]
    assert parse_month_mentions("6-8月工资", "2025-11-05") == ["2025-06", "2025-07", "2025-08"]
    assert parse_month_mentions("六、七月工资", "2025-11-05") == ["2025-06", "2025-07"]
    assert parse_month_mentions("2025年6月工资", "2026-11-05") == ["2025-06"]
    assert parse_month_mentions("9月工资", "2025-11-05") == ["2025-09"]


def test_parse_months_handles_year_boundary():
    # 12 月工资次年 1 月发
    assert parse_month_mentions("12月工资", "2026-01-05") == ["2025-12"]
    assert parse_month_mentions("1月工资", "2026-01-05") == ["2026-01"]


def test_parse_months_ignores_dates_and_unrelated_months():
    assert parse_month_mentions("工资", "2025-11-05") == []
    assert parse_month_mentions("2025年11月03日 转账", "2025-11-05") == []
    # 有工资关键词时只取邻近关键词的月份
    assert parse_month_mentions("5月1日消费 6月工资", "2025-11-05") == ["2025-06"]


# ---------------------------------------------------------------------------
# 核销建议
# ---------------------------------------------------------------------------
def test_remark_wins_over_oldest_unpaid(client, auth, test_db):
    company = _company(client, auth)
    june = _payroll(test_db, company["id"], "2025-06", 750000)
    july = _payroll(test_db, company["id"], "2025-07", 750000)
    september = _payroll(test_db, company["id"], "2025-09", 750000)

    proposal = propose_allocation(test_db, june.user_id, company["id"], 750000, "2025-11-05", "9月工资")
    assert proposal["source"] == "remark"
    assert proposal["months"] == ["2025-09"]
    assert [item["receivable_id"] for item in proposal["allocations"]] == [september.id]
    assert july.id not in [item["receivable_id"] for item in proposal["allocations"]]


def test_without_remark_oldest_unpaid_first(client, auth, test_db):
    company = _company(client, auth)
    june = _payroll(test_db, company["id"], "2025-06", 750000)
    july = _payroll(test_db, company["id"], "2025-07", 750000)
    _payroll(test_db, company["id"], "2025-08", 750000)

    one_month = propose_allocation(test_db, june.user_id, company["id"], 750000, "2025-11-05", "")
    assert one_month["source"] == "order"
    assert [item["receivable_id"] for item in one_month["allocations"]] == [june.id]

    two_months = propose_allocation(test_db, june.user_id, company["id"], 1500000, "2025-11-05", "工资")
    assert [item["receivable_id"] for item in two_months["allocations"]] == [june.id, july.id]
    assert two_months["remaining_cents"] == 0


def test_amount_heuristic_uses_reference_salary(client, auth, test_db):
    company = _company(client, auth)
    june = _payroll(test_db, company["id"], "2025-06", 700000)
    july = _payroll(test_db, company["id"], "2025-07", 700000)
    _payroll(test_db, company["id"], "2025-08", 700000)

    # 到账金额不是整月合计，但落在 2 个月工资的容差内
    proposal = propose_allocation(test_db, june.user_id, company["id"], 1405000, "2025-11-05", "工资")
    assert proposal["source"] == "amount"
    assert [item["receivable_id"] for item in proposal["allocations"]] == [june.id, july.id]
    assert proposal["remaining_cents"] == 5000


def test_remark_month_without_receivable_is_reported(client, auth, test_db):
    company = _company(client, auth)
    june = _payroll(test_db, company["id"], "2025-06", 750000)
    proposal = propose_allocation(test_db, june.user_id, company["id"], 750000, "2025-11-05", "9月工资")
    assert proposal["allocations"] == []
    assert proposal["missing_periods"] == ["2025-09"]
    assert "9月" in proposal["reason"]


# ---------------------------------------------------------------------------
# 端到端：截图 → 推断 → 一键核销
# ---------------------------------------------------------------------------
def test_inflow_screenshot_allocates_to_remark_months(client, auth, test_db, monkeypatch):
    company = _company(client, auth)
    june = _payroll(test_db, company["id"], "2025-06", 750000)
    july = _payroll(test_db, company["id"], "2025-07", 750000)

    draft = _draft_from_screenshot(
        client,
        auth,
        company["id"],
        {
            "doc_type": "salary",
            "doc_subtype": "银行交易明细",
            "direction": "in",
            "amount": 15000.0,
            "date": "2025-11-05",
            "remark": "6,7月工资",
            "evidence": "2025-11-05 +15,000.00 6,7月工资",
            "method": "ai_vision",
        },
        monkeypatch,
    )
    assert draft["is_inflow"] is True
    assert draft["remark"] == "6,7月工资"
    assert draft["payroll_match"]["months"] == ["2025-06", "2025-07"]
    assert draft["payroll_match"]["remaining_cents"] == 0

    applied = client.post(f"/api/capture/drafts/{draft['id']}/apply-payment", json={}, headers=auth)
    assert applied.status_code == 200, applied.text
    payload = applied.json()
    assert payload["draft"]["status"] == "confirmed"
    assert len(payload["allocations"]) == 2

    test_db.expire_all()
    for receivable in (june, july):
        refreshed = test_db.get(Receivable, receivable.id)
        assert refreshed.status == "paid"
        assert refreshed.received_cents == 750000

    payments = client.get("/api/payments", headers=auth).json()
    assert len(payments) == 1
    assert payments[0]["amount"] == 15000.0
    # 重复核销被拦住
    again = client.post(f"/api/capture/drafts/{draft['id']}/apply-payment", json={}, headers=auth)
    assert again.status_code == 400


def test_outflow_screenshot_is_not_allocated(client, auth, test_db, monkeypatch):
    company = _company(client, auth)
    june = _payroll(test_db, company["id"], "2025-06", 750000)
    draft = _draft_from_screenshot(
        client,
        auth,
        company["id"],
        {"doc_type": "payment", "direction": "out", "amount": 88.0, "date": "2025-11-05", "method": "ai_vision"},
        monkeypatch,
    )
    assert draft["is_inflow"] is False
    assert draft["payroll_match"] is None
    assert test_db.get(Receivable, june.id).status == "pending"


def test_apply_payment_rejects_when_nothing_to_allocate(client, auth, test_db, monkeypatch):
    company = _company(client, auth)
    _payroll(test_db, company["id"], "2025-06", 750000)
    draft = _draft_from_screenshot(
        client,
        auth,
        company["id"],
        {"doc_type": "salary", "direction": "in", "amount": 7500.0, "date": "2025-11-05", "remark": "9月工资", "method": "ai_vision"},
        monkeypatch,
    )
    response = client.post(f"/api/capture/drafts/{draft['id']}/apply-payment", json={}, headers=auth)
    assert response.status_code == 400
    assert "工资应收" in response.text


def test_apply_payment_rejects_while_still_processing(client, auth, test_db, monkeypatch):
    company = _company(client, auth)
    _payroll(test_db, company["id"], "2025-06", 750000)
    draft = _draft_from_screenshot(
        client,
        auth,
        company["id"],
        {"doc_type": "salary", "direction": "in", "amount": 7500.0, "date": "2025-11-05", "remark": "6月工资", "method": "ai_vision"},
        monkeypatch,
    )
    from app.models import CaptureDraft

    row = test_db.get(CaptureDraft, draft["id"])
    row.status = "processing"
    test_db.commit()

    response = client.post(f"/api/capture/drafts/{draft['id']}/apply-payment", json={}, headers=auth)
    assert response.status_code == 409


def test_apply_payment_accepts_explicit_allocations(client, auth, test_db, monkeypatch):
    company = _company(client, auth)
    june = _payroll(test_db, company["id"], "2025-06", 750000)
    draft = _draft_from_screenshot(
        client,
        auth,
        company["id"],
        {"doc_type": "salary", "direction": "in", "amount": 7500.0, "date": "2025-11-05", "method": "ai_vision"},
        monkeypatch,
    )
    response = client.post(
        f"/api/capture/drafts/{draft['id']}/apply-payment",
        json={"allocations": [{"receivable_id": june.id, "amount": "7500.00"}]},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    test_db.expire_all()
    assert test_db.get(Receivable, june.id).status == "paid"
