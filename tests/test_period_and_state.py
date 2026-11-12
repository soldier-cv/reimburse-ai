"""归属月、应收状态机、批次撤回与批次到账日期。"""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.models import AuditLog, Receivable
from app.services.money import today_str


def create_company(client, auth, name="测试公司", payday=15):
    response = client.post(
        "/api/companies",
        json={"name": name, "short_name": name[:4], "invoice_title": name, "payday": payday, "payment_days": 30},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    return response.json()


def create_receivable(client, auth, company_id, title="打车费", amount=128.0, occurred_on="2026-11-03", period=None, **extra):
    payload = {"company_id": company_id, "title": title, "amount": amount, "occurred_on": occurred_on}
    if period is not None:
        payload["period"] = period
    payload.update(extra)
    response = client.post("/api/receivables", json=payload, headers=auth)
    assert response.status_code == 200, response.text
    return response.json()


def make_batch(client, auth, company_id, ids, period="2026-11", title="11 月报销"):
    response = client.post(
        "/api/batches",
        json={"company_id": company_id, "period": period, "title": title, "receivable_ids": ids},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    return response.json()["batch_id"]


def submit(client, auth, batch_id, submitted_on="2026-11-05"):
    response = client.post(f"/api/batches/{batch_id}/submit", json={"submitted_on": submitted_on}, headers=auth)
    assert response.status_code == 200, response.text
    return response.json()


def test_confirm_uses_explicit_period_not_occurred_on(client, auth):
    from PIL import Image
    import io

    company = create_company(client, auth, name="归属月公司")
    image = Image.new("RGB", (40, 40), (200, 200, 200))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    upload = client.post(
        "/api/capture",
        files=[("files", ("ticket.png", buf.getvalue(), "image/png"))],
        data={"company_id": str(company["id"])},
        headers=auth,
    )
    assert upload.status_code == 200, upload.text
    draft_id = upload.json()["drafts"][0]["id"]
    patched = client.patch(
        f"/api/capture/drafts/{draft_id}",
        json={"amount": "88.00", "occurred_on": "2026-10-28", "period": "2026-11", "title": "跨月发票"},
        headers=auth,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["period"] == "2026-11"
    assert patched.json()["occurred_on"] == "2026-10-28"

    confirm = client.post(
        "/api/capture/confirm",
        json={"draft_ids": [draft_id], "company_id": company["id"]},
        headers=auth,
    )
    assert confirm.status_code == 200, confirm.text
    item = confirm.json()["created"][0]
    assert item["occurred_on"] == "2026-10-28"
    assert item["period"] == "2026-11"


def test_confirm_group_period_override(client, auth):
    from PIL import Image
    import io

    company = create_company(client, auth, name="批量归属")
    drafts = []
    for name, color in (("a.png", (180, 180, 180)), ("b.png", (20, 90, 200))):
        buf = io.BytesIO()
        Image.new("RGB", (20, 20), color).save(buf, format="PNG")
        upload = client.post(
            "/api/capture",
            files=[("files", (name, buf.getvalue(), "image/png"))],
            data={"company_id": str(company["id"])},
            headers=auth,
        )
        draft_id = upload.json()["drafts"][0]["id"]
        client.patch(
            f"/api/capture/drafts/{draft_id}",
            json={"amount": "10.00", "occurred_on": "2026-09-12"},
            headers=auth,
        )
        drafts.append(draft_id)
    confirm = client.post(
        "/api/capture/confirm",
        json={"draft_ids": drafts, "company_id": company["id"], "period": "2026-10"},
        headers=auth,
    )
    assert confirm.status_code == 200, confirm.text
    periods = {item["period"] for item in confirm.json()["created"]}
    assert periods == {"2026-10"}


def test_manual_create_accepts_independent_period(client, auth):
    company = create_company(client, auth)
    item = create_receivable(client, auth, company["id"], occurred_on="2026-12-31", period="2027-01", amount=50)
    assert item["period"] == "2027-01"
    assert item["occurred_on"] == "2026-12-31"


def test_batch_rejects_mixed_periods(client, auth):
    company = create_company(client, auth)
    first = create_receivable(client, auth, company["id"], title="十月", occurred_on="2026-10-08", period="2026-10")
    second = create_receivable(client, auth, company["id"], title="十一月", occurred_on="2026-11-08", period="2026-11")
    response = client.post(
        "/api/batches",
        json={"company_id": company["id"], "period": "2026-11", "receivable_ids": [first["id"], second["id"]]},
        headers=auth,
    )
    assert response.status_code == 400, response.text
    assert "归属月" in response.json()["detail"]


def test_submitted_locks_fields_and_forbids_delete(client, auth):
    company = create_company(client, auth)
    item = create_receivable(client, auth, company["id"], title="锁定事项", amount=200, period="2026-11")
    batch_id = make_batch(client, auth, company["id"], [item["id"]])
    submit(client, auth, batch_id)

    for payload in (
        {"amount": 300},
        {"title": "改名"},
        {"company_id": company["id"], "title": "仍然锁定"},
        {"period": "2026-12"},
    ):
        response = client.put(f"/api/receivables/{item['id']}", json=payload, headers=auth)
        assert response.status_code == 400, (payload, response.text)
        assert "锁定" in response.json()["detail"] or "撤回" in response.json()["detail"]

    deleted = client.delete(f"/api/receivables/{item['id']}", headers=auth)
    assert deleted.status_code == 400
    assert "放弃" in deleted.json()["detail"]

    abandoned = client.put(
        f"/api/receivables/{item['id']}",
        json={"status": "abandoned"},
        headers=auth,
    )
    assert abandoned.status_code == 400
    abandoned = client.put(
        f"/api/receivables/{item['id']}",
        json={"status": "abandoned", "reason": "不再报销"},
        headers=auth,
    )
    assert abandoned.status_code == 200, abandoned.text
    assert abandoned.json()["status"] == "abandoned"


def test_illegal_status_changes_rejected(client, auth):
    company = create_company(client, auth)
    item = create_receivable(client, auth, company["id"])
    # 不能手工改成已提交
    response = client.put(f"/api/receivables/{item['id']}", json={"status": "submitted"}, headers=auth)
    assert response.status_code == 400
    # 待提交可以放弃
    response = client.put(f"/api/receivables/{item['id']}", json={"status": "abandoned"}, headers=auth)
    assert response.status_code == 200, response.text
    # 终态不能再改
    response = client.put(f"/api/receivables/{item['id']}", json={"status": "pending"}, headers=auth)
    assert response.status_code == 400
    response = client.put(f"/api/receivables/{item['id']}", json={"title": "再改"}, headers=auth)
    assert response.status_code == 400


def test_rejected_can_return_to_pending(client, auth):
    company = create_company(client, auth)
    item = create_receivable(client, auth, company["id"])
    rejected = client.put(f"/api/receivables/{item['id']}", json={"status": "rejected"}, headers=auth)
    assert rejected.status_code == 200
    pending = client.put(f"/api/receivables/{item['id']}", json={"status": "pending"}, headers=auth)
    assert pending.status_code == 200
    assert pending.json()["status"] == "pending"


def test_unsubmit_requires_reason_and_writes_audit(client, auth, test_db):
    company = create_company(client, auth)
    first = create_receivable(client, auth, company["id"], title="A", amount=100)
    second = create_receivable(client, auth, company["id"], title="B", amount=50)
    batch_id = make_batch(client, auth, company["id"], [first["id"], second["id"]])
    submit(client, auth, batch_id, submitted_on="2026-11-08")

    missing = client.post(f"/api/batches/{batch_id}/unsubmit", json={"reason": ""}, headers=auth)
    assert missing.status_code == 400

    result = client.post(f"/api/batches/{batch_id}/unsubmit", json={"reason": "漏了一张票"}, headers=auth)
    assert result.status_code == 200, result.text
    assert result.json()["original_submitted_on"] == "2026-11-08"
    assert result.json()["count"] == 2

    detail = client.get(f"/api/batches/{batch_id}", headers=auth).json()
    assert detail["status"] == "draft"
    assert detail["submitted_on"] == ""
    assert all(item["status"] == "pending" for item in detail["items"])
    assert all(item["batch_id"] == batch_id for item in detail["items"])

    logs = test_db.query(AuditLog).filter(AuditLog.action == "撤回提交").all()
    assert logs
    assert "漏了一张票" in logs[-1].summary
    assert "2026-11-08" in logs[-1].summary


def test_cannot_unsubmit_or_split_after_payment(client, auth):
    company = create_company(client, auth)
    item = create_receivable(client, auth, company["id"], amount=200)
    batch_id = make_batch(client, auth, company["id"], [item["id"]])
    submit(client, auth, batch_id)
    paid = client.post(
        f"/api/batches/{batch_id}/payment",
        json={"paid_on": "2026-11-15", "note": "到账"},
        headers=auth,
    )
    assert paid.status_code == 200, paid.text
    unsubmit = client.post(f"/api/batches/{batch_id}/unsubmit", json={"reason": "想改"}, headers=auth)
    assert unsubmit.status_code == 400
    cancel = client.post(f"/api/batches/{batch_id}/cancel", headers=auth)
    assert cancel.status_code == 400
    items = client.post(
        f"/api/batches/{batch_id}/items",
        json={"remove_ids": [item["id"]]},
        headers=auth,
    )
    assert items.status_code == 400


def test_draft_batch_add_and_remove_items(client, auth):
    company = create_company(client, auth)
    first = create_receivable(client, auth, company["id"], title="原有", amount=80)
    extra = create_receivable(client, auth, company["id"], title="后加", amount=40)
    leftover = create_receivable(client, auth, company["id"], title="待移出", amount=20)
    batch_id = make_batch(client, auth, company["id"], [first["id"], leftover["id"]])

    added = client.post(
        f"/api/batches/{batch_id}/items",
        json={"add_ids": [extra["id"]]},
        headers=auth,
    )
    assert added.status_code == 200, added.text
    ids = {item["id"] for item in added.json()["items"]}
    assert ids == {first["id"], leftover["id"], extra["id"]}

    removed = client.post(
        f"/api/batches/{batch_id}/items",
        json={"remove_ids": [leftover["id"]]},
        headers=auth,
    )
    assert removed.status_code == 200, removed.text
    ids = {item["id"] for item in removed.json()["items"]}
    assert ids == {first["id"], extra["id"]}
    leftover_detail = client.get(f"/api/receivables/{leftover['id']}", headers=auth).json()
    assert leftover_detail["batch_id"] in (None, 0)
    assert leftover_detail["status"] == "pending"

    cancel = client.post(f"/api/batches/{batch_id}/cancel", headers=auth)
    assert cancel.status_code == 200
    assert cancel.json()["count"] == 2


def test_batch_payment_explicit_date_and_default_today(client, auth, test_db):
    company = create_company(client, auth)
    first = create_receivable(client, auth, company["id"], title="显式日期", amount=100)
    batch_id = make_batch(client, auth, company["id"], [first["id"]])
    submit(client, auth, batch_id)
    paid = client.post(
        f"/api/batches/{batch_id}/payment",
        json={"paid_on": "2026-11-18", "note": "指定日期到账"},
        headers=auth,
    )
    assert paid.status_code == 200, paid.text
    detail = client.get(f"/api/receivables/{first['id']}", headers=auth).json()
    assert detail["paid_on"] == "2026-11-18"
    payments = client.get("/api/payments", headers=auth).json()
    assert payments[0]["paid_on"] == "2026-11-18"

    second = create_receivable(client, auth, company["id"], title="默认今天", amount=60)
    batch2 = make_batch(client, auth, company["id"], [second["id"]], title="第二批")
    submit(client, auth, batch2)
    paid2 = client.post(f"/api/batches/{batch2}/payment", json={"note": "批次到账"}, headers=auth)
    assert paid2.status_code == 200, paid2.text
    today = today_str()
    shanghai = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    assert today == shanghai
    detail2 = client.get(f"/api/receivables/{second['id']}", headers=auth).json()
    assert detail2["paid_on"] == today
    payments = client.get("/api/payments", headers=auth).json()
    latest = next(item for item in payments if item["note"] == "批次到账")
    assert latest["paid_on"] == today


def test_pending_and_rejected_can_be_collected(client, auth):
    company = create_company(client, auth)
    pending = create_receivable(client, auth, company["id"], title="待提交")
    rejected = create_receivable(client, auth, company["id"], title="被驳回")
    client.put(f"/api/receivables/{rejected['id']}", json={"status": "rejected"}, headers=auth)
    listing = client.get("/api/receivables", headers=auth).json()["items"]
    by_id = {item["id"]: item for item in listing}
    assert by_id[pending["id"]]["can_collect"] is True
    assert by_id[rejected["id"]]["can_collect"] is True
    batch_id = make_batch(client, auth, company["id"], [pending["id"], rejected["id"]])
    detail = client.get(f"/api/batches/{batch_id}", headers=auth).json()
    assert all(item["status"] == "pending" for item in detail["items"])
