import json

from tests.test_receivables_flow import create_company, create_receivable, make_png
from app.models import CaptureDraft


def _category_id(client, auth, name):
    categories = client.get("/api/categories", headers=auth).json()
    match = next(item for item in categories if item["name"] == name)
    return match["id"]


def _upload_draft(client, auth, company_id, filename="shot.png"):
    upload = client.post(
        "/api/capture",
        files=[("files", (filename, make_png(), "image/png"))],
        data={"company_id": str(company_id)},
        headers=auth,
    )
    assert upload.status_code == 200, upload.text
    return upload.json()["drafts"][0]


def test_append_draft_to_existing_receivable_without_amount(client, auth):
    company = create_company(client, auth, name="追加公司")
    item = create_receivable(client, auth, company["id"], "已有报销", 200.0)
    draft = _upload_draft(client, auth, company["id"], "extra.png")
    assert draft["amount_cents"] is None

    appended = client.post(
        "/api/capture/confirm",
        json={"draft_ids": [draft["id"]], "target_receivable_id": item["id"]},
        headers=auth,
    )
    assert appended.status_code == 200, appended.text

    detail = client.get(f"/api/receivables/{item['id']}", headers=auth).json()
    assert detail["amount_cents"] == 20000
    assert detail["attachment_count"] == 1
    assert detail["attachments"][0]["filename"] == "extra.png"


def test_materials_status_and_filter(client, auth):
    company = create_company(client, auth, name="材料公司")
    travel_id = _category_id(client, auth, "差旅住宿")
    payroll_id = _category_id(client, auth, "工资")

    travel = client.post(
        "/api/receivables",
        json={
            "company_id": company["id"],
            "category_id": travel_id,
            "title": "酒店发票待补",
            "amount": 480.0,
            "occurred_on": "2026-11-03",
        },
        headers=auth,
    ).json()
    payroll = client.post(
        "/api/receivables",
        json={
            "company_id": company["id"],
            "category_id": payroll_id,
            "title": "11 月工资",
            "amount": 10000.0,
            "occurred_on": "2026-11-05",
        },
        headers=auth,
    ).json()

    travel_detail = client.get(f"/api/receivables/{travel['id']}", headers=auth).json()
    payroll_detail = client.get(f"/api/receivables/{payroll['id']}", headers=auth).json()
    assert travel_detail["requires_invoice"] is True
    assert travel_detail["missing_invoice"] is True
    assert travel_detail["materials_ok"] is False
    assert payroll_detail["requires_invoice"] is False
    assert payroll_detail["materials_ok"] is True

    dashboard = client.get("/api/dashboard", headers=auth).json()
    assert dashboard["kpis"]["missing_materials_count"] == 1

    missing = client.get("/api/receivables?missing_materials=true", headers=auth).json()
    assert missing["total"] == 1
    assert missing["items"][0]["id"] == travel["id"]

    draft = _upload_draft(client, auth, company["id"], "hotel_invoice.png")
    client.patch(
        f"/api/capture/drafts/{draft['id']}",
        json={"amount": "480.00", "occurred_on": "2026-11-03", "doc_type": "invoice", "title": "酒店发票"},
        headers=auth,
    )
    confirm = client.post(
        "/api/capture/confirm",
        json={"draft_ids": [draft["id"]], "target_receivable_id": travel["id"]},
        headers=auth,
    )
    assert confirm.status_code == 200, confirm.text

    travel_detail = client.get(f"/api/receivables/{travel['id']}", headers=auth).json()
    assert travel_detail["has_invoice"] is True
    assert travel_detail["materials_ok"] is True
    assert client.get("/api/dashboard", headers=auth).json()["kpis"]["missing_materials_count"] == 0
    assert client.get("/api/receivables?missing_materials=true", headers=auth).json()["total"] == 0


def test_buyer_mismatch_warns_and_can_be_forced(client, auth, test_db):
    company = create_company(client, auth, name="抬头公司")
    client.put(
        f"/api/companies/{company['id']}",
        json={"invoice_title": "某某科技有限公司"},
        headers=auth,
    )
    draft = _upload_draft(client, auth, company["id"], "invoice.png")
    client.patch(
        f"/api/capture/drafts/{draft['id']}",
        json={"amount": "88.00", "occurred_on": "2026-11-04", "doc_type": "invoice", "title": "餐饮发票"},
        headers=auth,
    )
    row = test_db.query(CaptureDraft).filter(CaptureDraft.id == draft["id"]).one()
    row.doc_type = "invoice"
    row.extracted_json = json.dumps({"buyer": "另一家贸易有限公司", "doc_type": "invoice"}, ensure_ascii=False)
    test_db.commit()

    confirm = client.post(
        "/api/capture/confirm",
        json={"draft_ids": [draft["id"]], "company_id": company["id"]},
        headers=auth,
    )
    assert confirm.status_code == 409, confirm.text
    warnings = confirm.json()["detail"]["warnings"]
    assert any(item["type"] == "buyer_mismatch" for item in warnings)

    forced = client.post(
        "/api/capture/confirm",
        json={"draft_ids": [draft["id"]], "company_id": company["id"], "force": True},
        headers=auth,
    )
    assert forced.status_code == 200, forced.text
