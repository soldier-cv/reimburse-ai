"""到账草稿归属月推断：备注月份优先，其次按待发工资推断，手填不被覆盖。"""

import io

from PIL import Image

from app.models import Receivable, User


def _png():
    b = io.BytesIO()
    Image.new("RGB", (240, 320), (240, 240, 240)).save(b, format="PNG")
    return b.getvalue()


def _enable_ai(client, auth):
    client.post(
        "/api/settings/ai",
        json={"enabled": True, "base_url": "https://x/v1", "api_key": "k", "model_name": "m", "system_prompt": ""},
        headers=auth,
    )


def _company(client, auth, name="推断公司"):
    return client.post("/api/companies", json={"name": name, "payday": 15}, headers=auth).json()


def _payroll(test_db, company_id, period, cents, status="submitted"):
    user = test_db.query(User).first()
    item = Receivable(
        user_id=user.id, company_id=company_id, kind="payroll",
        title=f"{period} 工资", period=period, occurred_on=f"{period}-15",
        amount_cents=cents, status=status,
    )
    test_db.add(item)
    test_db.commit()
    test_db.refresh(item)
    return item


def _capture(client, auth, monkeypatch, company_id, extracted):
    async def fake_ai(**kwargs):
        return extracted

    monkeypatch.setattr("app.services.capture.extract_document_with_ai", fake_ai)
    resp = client.post(
        "/api/capture",
        files=[("files", ("s.png", _png(), "image/png"))],
        data={"company_id": company_id},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["drafts"][0]


def test_remark_month_sets_draft_period(client, auth, test_db, monkeypatch):
    _enable_ai(client, auth)
    company = _company(client, auth)
    _payroll(test_db, company["id"], "2026-01", 1800000)
    draft = _capture(client, auth, monkeypatch, company["id"], {
        "doc_type": "salary", "direction": "in", "amount": 14554.5,
        "date": "2026-02-10", "remark": "2026年1月工资",
        "entries": [], "method": "ai_vision",
    })
    assert draft["period"] == "2026-01"
    assert draft["period_source"] == "remark"
    assert draft["period_suggestion"]["source"] == "remark"


def test_no_remark_infers_earliest_unpaid(client, auth, test_db, monkeypatch):
    _enable_ai(client, auth)
    company = _company(client, auth)
    _payroll(test_db, company["id"], "2026-01", 1800000)
    _payroll(test_db, company["id"], "2026-02", 1800000)
    draft = _capture(client, auth, monkeypatch, company["id"], {
        "doc_type": "salary", "direction": "in", "amount": 14554.5,
        "date": "2026-03-10", "remark": "",
        "entries": [], "method": "ai_vision",
    })
    assert draft["period"] == "2026-01"
    assert draft["period_source"] == "order"


def test_manual_period_not_overridden(client, auth, test_db, monkeypatch):
    _enable_ai(client, auth)
    company = _company(client, auth)
    _payroll(test_db, company["id"], "2026-01", 1800000)
    draft = _capture(client, auth, monkeypatch, company["id"], {
        "doc_type": "salary", "direction": "in", "amount": 14554.5,
        "date": "2026-02-10", "remark": "2026年1月工资",
        "entries": [], "method": "ai_vision",
    })
    patched = client.patch(
        f"/api/capture/drafts/{draft['id']}", json={"period": "2025-12"}, headers=auth
    ).json()
    assert patched["period"] == "2025-12"
    import json as _json
    _d = test_db.get(__import__("app.models", fromlist=["CaptureDraft"]).CaptureDraft, draft["id"])
    print("DBG before retry", _d.period, _json.loads(_d.extracted_json).get("period_source"))
    # 重新识别不应覆盖手填归属月
    client.post(f"/api/capture/drafts/{draft['id']}/retry", headers=auth)
    from app.models import CaptureDraft
    test_db.expire_all()
    refreshed = test_db.get(CaptureDraft, draft["id"])
    assert refreshed.period == "2025-12"
