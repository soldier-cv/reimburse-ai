"""Agent 令牌与故事测试：使用可复用发票夹具，不依赖在线 AI。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from app.models import AgentToken, CaptureDraft

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "invoices"
MANIFEST = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
INVOICES = {item["id"]: item for item in MANIFEST["invoices"]}

STORY_USERNAME = "story-tester"
STORY_PASSWORD = "StoryTesterPass123!"
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "AdminPass123!"
OTHER_USERNAME = "other-user"
OTHER_PASSWORD = "OtherUserPass123!"


def fixture_image(name: str) -> tuple[dict, bytes]:
    item = INVOICES[name]
    return item, (FIXTURE_DIR / item["image"]).read_bytes()


def headers_of(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def init_admin(client) -> dict:
    response = client.post(
        "/api/system/init-admin",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return headers_of(response.json()["token"])


def create_member(client, admin_headers, username: str, password: str) -> None:
    response = client.post(
        "/api/admin/users",
        json={"username": username, "password": password, "role": "user"},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text


def login(client, username: str, password: str) -> dict:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return headers_of(response.json()["token"])


def issue_token(client, session, name="故事测试", scopes=None, expires_at=None) -> tuple[str, dict]:
    payload = {"name": name}
    if scopes is not None:
        payload["scopes"] = scopes
    if expires_at is not None:
        payload["expires_at"] = expires_at
    response = client.post("/api/settings/agent-tokens", json=payload, headers=session)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token"].startswith("ragt_")
    return body["token"], body


def category_id(client, headers, name: str) -> int:
    categories = client.get("/api/categories", headers=headers).json()
    match = next(item for item in categories if item["name"] == name)
    return match["id"]


def create_company(client, headers, name="杭州识微科技有限公司"):
    response = client.post(
        "/api/companies",
        json={
            "name": name,
            "short_name": "识微",
            "invoice_title": MANIFEST["buyer_title"],
            "tax_id": MANIFEST["buyer_tax_id"],
            "payday": 15,
            "payment_days": 30,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def upload_and_fill(client, headers, company_id: int, fixture_id: str, test_db, category: str | None = None):
    item, image = fixture_image(fixture_id)
    upload = client.post(
        "/api/capture",
        files=[("files", (item["image"], image, "image/png"))],
        data={"company_id": str(company_id)},
        headers=headers,
    )
    assert upload.status_code == 200, upload.text
    draft = upload.json()["drafts"][0]
    chosen_category = category_id(client, headers, category or item["category"])
    patch = client.patch(
        f"/api/capture/drafts/{draft['id']}",
        json={
            "company_id": company_id,
            "category_id": chosen_category,
            "amount": item["amount"],
            "occurred_on": item["occurred_on"],
            "title": item["title"],
            "merchant": item["merchant"],
            "doc_type": item["doc_type"],
            "invoice_no": item["invoice_no"],
            "invoice_code": item["invoice_code"],
        },
        headers=headers,
    )
    assert patch.status_code == 200, patch.text
    row = test_db.query(CaptureDraft).filter(CaptureDraft.id == draft["id"]).one()
    extracted = {}
    try:
        loaded = json.loads(row.extracted_json or "{}")
        if isinstance(loaded, dict):
            extracted = loaded
    except json.JSONDecodeError:
        extracted = {}
    extracted.update(
        {
            "buyer": item["buyer"],
            "doc_type": item["doc_type"],
            "invoice_no": item["invoice_no"],
            "amount": item["amount"],
        }
    )
    row.extracted_json = json.dumps(extracted, ensure_ascii=False)
    row.doc_type = item["doc_type"]
    test_db.commit()
    return patch.json(), item, chosen_category


def confirm(client, headers, draft_id: int, company_id: int, category_id_value: int | None = None, force: bool = False, target_receivable_id: int | None = None):
    payload = {"draft_ids": [draft_id], "company_id": company_id, "force": force}
    if category_id_value is not None:
        payload["category_id"] = category_id_value
    if target_receivable_id is not None:
        payload["target_receivable_id"] = target_receivable_id
    return client.post("/api/capture/confirm", json=payload, headers=headers)


def setup_story_user(client):
    admin = init_admin(client)
    create_member(client, admin, STORY_USERNAME, STORY_PASSWORD)
    session = login(client, STORY_USERNAME, STORY_PASSWORD)
    return admin, session


def test_agent_token_management_and_session_only_admin(client):
    admin, session = setup_story_user(client)
    me = client.get("/api/auth/me", headers=session)
    assert me.status_code == 200
    assert me.json()["username"] == STORY_USERNAME
    assert me.json()["auth_method"] == "session"

    token, created = issue_token(client, session, name="默认令牌")
    assert set(created["scopes"]) == {"capture", "ledger", "export"}
    assert created["token_prefix"].startswith("ragt_")
    assert created["token"].startswith(created["token_prefix"])

    listed = client.get("/api/settings/agent-tokens", headers=session)
    assert listed.status_code == 200
    assert all("token" not in item for item in listed.json())
    assert listed.json()[0]["name"] == "默认令牌"

    agent = headers_of(token)
    agent_me = client.get("/api/auth/me", headers=agent)
    assert agent_me.status_code == 200
    body = agent_me.json()
    assert body["auth_method"] == "agent"
    assert body["token_name"] == "默认令牌"
    assert set(body["scopes"]) == {"capture", "ledger", "export"}
    assert body["username"] == STORY_USERNAME

    for path, method, payload in (
        ("/api/settings/agent-tokens", "get", None),
        ("/api/settings/agent-tokens", "post", {"name": "不应签发"}),
        (f"/api/settings/agent-tokens/{created['id']}", "patch", {"enabled": False}),
        (f"/api/settings/agent-tokens/{created['id']}/revoke", "post", {}),
    ):
        response = getattr(client, method)(path, json=payload, headers=agent) if payload is not None or method != "get" else client.get(path, headers=agent)
        assert response.status_code == 403, response.text

    logout = client.post("/api/auth/logout", headers=agent)
    assert logout.status_code == 403


def test_story_capture_ledger_payment_export_flow(client, test_db):
    _, session = setup_story_user(client)
    token, _ = issue_token(client, session, name="故事全流程")
    agent = headers_of(token)
    company = create_company(client, agent)

    taxi_draft, taxi, taxi_cat = upload_and_fill(client, agent, company["id"], "taxi", test_db)
    taxi_confirm = confirm(client, agent, taxi_draft["id"], company["id"], taxi_cat)
    assert taxi_confirm.status_code == 200, taxi_confirm.text
    taxi_item = taxi_confirm.json()["created"][0]
    assert taxi_item["amount_cents"] == taxi["amount_cents"]
    assert taxi_item["category_name"] == "城市交通"
    assert taxi_item["title"] == taxi["title"]
    assert taxi_item["attachments"][0]["invoice_no"] == taxi["invoice_no"] if taxi_item.get("attachments") else True
    detail = client.get(f"/api/receivables/{taxi_item['id']}", headers=agent).json()
    assert detail["amount_cents"] == 12800
    assert detail["category_name"] == "城市交通"
    assert any(att["invoice_no"] == taxi["invoice_no"] for att in detail["attachments"])

    hotel_draft, hotel, hotel_cat = upload_and_fill(client, agent, company["id"], "hotel", test_db)
    hotel_confirm = confirm(client, agent, hotel_draft["id"], company["id"], hotel_cat)
    assert hotel_confirm.status_code == 200, hotel_confirm.text
    hotel_item = hotel_confirm.json()["created"][0]
    assert hotel_item["amount_cents"] == hotel["amount_cents"]
    assert hotel_item["category_name"] == "差旅住宿"

    meal_draft, meal, meal_cat = upload_and_fill(client, agent, company["id"], "meal", test_db)
    meal_confirm = confirm(client, agent, meal_draft["id"], company["id"], meal_cat)
    assert meal_confirm.status_code == 200, meal_confirm.text
    meal_item = meal_confirm.json()["created"][0]
    assert meal_item["amount_cents"] == meal["amount_cents"]
    assert meal_item["category_name"] == "餐饮招待"

    original_amount = detail["amount_cents"]
    office_draft, office, _ = upload_and_fill(client, agent, company["id"], "office", test_db)
    appended = confirm(
        client,
        agent,
        office_draft["id"],
        company["id"],
        target_receivable_id=taxi_item["id"],
    )
    assert appended.status_code == 200, appended.text
    after_append = client.get(f"/api/receivables/{taxi_item['id']}", headers=agent).json()
    assert after_append["amount_cents"] == original_amount
    assert after_append["attachment_count"] == 2
    assert any(att["filename"] == office["image"] for att in after_append["attachments"])

    mismatch_draft, mismatch, mismatch_cat = upload_and_fill(client, agent, company["id"], "mismatch", test_db)
    blocked = confirm(client, agent, mismatch_draft["id"], company["id"], mismatch_cat)
    assert blocked.status_code == 409, blocked.text
    warnings = blocked.json()["detail"]["warnings"]
    assert any(item["type"] == "buyer_mismatch" for item in warnings)
    forced = confirm(client, agent, mismatch_draft["id"], company["id"], mismatch_cat, force=True)
    assert forced.status_code == 200, forced.text
    mismatch_item = forced.json()["created"][0]
    assert mismatch_item["amount_cents"] == mismatch["amount_cents"]

    travel_cat = category_id(client, agent, "差旅住宿")
    incomplete = client.post(
        "/api/receivables",
        json={
            "company_id": company["id"],
            "category_id": travel_cat,
            "title": "待补发票的住宿费",
            "amount": "320.00",
            "occurred_on": "2026-11-08",
        },
        headers=agent,
    )
    assert incomplete.status_code == 200, incomplete.text
    incomplete_item = incomplete.json()
    incomplete_detail = client.get(f"/api/receivables/{incomplete_item['id']}", headers=agent).json()
    assert incomplete_detail["requires_invoice"] is True
    assert incomplete_detail["missing_invoice"] is True
    assert incomplete_detail["materials_ok"] is False
    dashboard = client.get("/api/dashboard", headers=agent).json()
    assert dashboard["kpis"]["missing_materials_count"] >= 1
    missing = client.get("/api/receivables?missing_materials=true", headers=agent).json()
    assert any(item["id"] == incomplete_item["id"] for item in missing["items"])

    batch_ids = [taxi_item["id"], hotel_item["id"], meal_item["id"]]
    batch = client.post(
        "/api/batches",
        json={"company_id": company["id"], "period": "2026-11", "title": "2026年11月报销", "receivable_ids": batch_ids},
        headers=agent,
    )
    assert batch.status_code == 200, batch.text
    batch_id = batch.json()["batch_id"]
    submitted = client.post(
        f"/api/batches/{batch_id}/submit",
        json={"submitted_on": "2026-11-10"},
        headers=agent,
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["expected_on"] == "2026-11-15"

    first_pay = client.post(
        "/api/payments",
        json={
            "company_id": company["id"],
            "amount": "100.00",
            "paid_on": "2026-11-16",
            "allocations": [{"receivable_id": taxi_item["id"], "amount": "100.00"}],
        },
        headers=agent,
    )
    assert first_pay.status_code == 200, first_pay.text
    taxi_partial = client.get(f"/api/receivables/{taxi_item['id']}", headers=agent).json()
    assert taxi_partial["status"] == "partial"
    assert taxi_partial["received_cents"] == 10000
    assert taxi_partial["remaining_cents"] == 2800

    rest_pay = client.post(
        "/api/payments",
        json={
            "company_id": company["id"],
            "amount": "28.00",
            "paid_on": "2026-11-20",
            "allocations": [{"receivable_id": taxi_item["id"], "amount": "28.00"}],
        },
        headers=agent,
    )
    assert rest_pay.status_code == 200, rest_pay.text
    taxi_paid = client.get(f"/api/receivables/{taxi_item['id']}", headers=agent).json()
    assert taxi_paid["status"] == "paid"
    assert taxi_paid["received_cents"] == 12800
    assert taxi_paid["remaining_cents"] == 0

    packed = client.post(
        "/api/export/finance-pack",
        json={"receivable_ids": batch_ids, "include_originals": True},
        headers=agent,
    )
    assert packed.status_code == 200, packed.text
    assert packed.headers["content-type"].startswith("application/zip") or "zip" in packed.headers.get("content-type", "")
    assert len(packed.content) > 1000

    excel = client.get("/api/export/excel", headers=agent)
    assert excel.status_code == 200
    assert len(excel.content) > 1000

    records = client.get("/api/export/records", headers=agent)
    assert records.status_code == 200
    assert records.json()


def test_scope_denial(client, test_db):
    _, session = setup_story_user(client)
    company = create_company(client, session)
    capture_token, _ = issue_token(client, session, name="仅采集", scopes=["capture"])
    export_token, _ = issue_token(client, session, name="仅导出", scopes=["export"])
    ledger_token, _ = issue_token(client, session, name="仅台账", scopes=["ledger"])

    capture = headers_of(capture_token)
    export = headers_of(export_token)
    ledger = headers_of(ledger_token)

    companies = client.get("/api/companies", headers=capture)
    assert companies.status_code == 403
    exported = client.get("/api/export/excel", headers=capture)
    assert exported.status_code == 403

    capture_upload = client.post(
        "/api/capture",
        files=[("files", (INVOICES["taxi"]["image"], fixture_image("taxi")[1], "image/png"))],
        data={"company_id": str(company["id"])},
        headers=export,
    )
    assert capture_upload.status_code == 403, capture_upload.text

    listing = client.get("/api/receivables", headers=ledger)
    assert listing.status_code == 200
    denied_export = client.get("/api/export/excel", headers=ledger)
    assert denied_export.status_code == 403
    denied_capture = client.post(
        "/api/capture",
        files=[("files", (INVOICES["taxi"]["image"], fixture_image("taxi")[1], "image/png"))],
        data={"company_id": str(company["id"])},
        headers=ledger,
    )
    assert denied_capture.status_code == 403


def test_revoked_disabled_expired_token_unauthorized(client, test_db):
    _, session = setup_story_user(client)
    active, created = issue_token(client, session, name="可停用")
    disabled = client.patch(
        f"/api/settings/agent-tokens/{created['id']}",
        json={"enabled": False},
        headers=session,
    )
    assert disabled.status_code == 200, disabled.text
    assert client.get("/api/auth/me", headers=headers_of(active)).status_code == 401

    enabled = client.patch(
        f"/api/settings/agent-tokens/{created['id']}",
        json={"enabled": True},
        headers=session,
    )
    assert enabled.status_code == 200
    assert client.get("/api/auth/me", headers=headers_of(active)).status_code == 200

    revoked = client.post(f"/api/settings/agent-tokens/{created['id']}/revoke", headers=session)
    assert revoked.status_code == 200
    assert client.get("/api/auth/me", headers=headers_of(active)).status_code == 401
    again = client.patch(
        f"/api/settings/agent-tokens/{created['id']}",
        json={"enabled": True},
        headers=session,
    )
    assert again.status_code == 400

    expired_plain, expired_row = issue_token(
        client,
        session,
        name="已过期",
        expires_at=(datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"),
    )
    assert client.get("/api/auth/me", headers=headers_of(expired_plain)).status_code == 401
    stored = test_db.query(AgentToken).filter(AgentToken.id == expired_row["id"]).one()
    assert stored.expires_at is not None


def test_agent_cannot_access_ai_settings_or_admin(client):
    admin, session = setup_story_user(client)
    user_token, _ = issue_token(client, session, name="成员令牌")
    admin_token, _ = issue_token(client, admin, name="管理员令牌")

    for token in (user_token, admin_token):
        agent = headers_of(token)
        assert client.get("/api/settings/ai", headers=agent).status_code == 403
        assert client.post("/api/settings/ai", json={"enabled": True}, headers=agent).status_code == 403
        assert client.post("/api/settings/ai/models", json={"base_url": "https://example.com"}, headers=agent).status_code == 403
        assert client.post("/api/settings/ai/test", json={"base_url": "https://example.com"}, headers=agent).status_code == 403
        assert client.get("/api/admin/users", headers=agent).status_code == 403
        assert client.post(
            "/api/auth/change-password",
            json={"old_password": STORY_PASSWORD, "new_password": "NewPass123!"},
            headers=agent,
        ).status_code == 403
        assert client.post(
            "/api/system/init-admin",
            json={"username": "hacker", "password": "HackerPass123"},
            headers=agent,
        ).status_code == 403
        assert client.post(
            "/api/auth/register",
            json={"username": "nobody123", "password": "NobodyPass123"},
            headers=agent,
        ).status_code == 403


def test_agent_token_cannot_see_other_users_ledger(client, test_db):
    admin, session = setup_story_user(client)
    create_member(client, admin, OTHER_USERNAME, OTHER_PASSWORD)
    other_session = login(client, OTHER_USERNAME, OTHER_PASSWORD)
    other_company = create_company(client, other_session, name="他人公司")
    other_item = client.post(
        "/api/receivables",
        json={
            "company_id": other_company["id"],
            "title": "他人应收",
            "amount": "999.00",
            "occurred_on": "2026-11-01",
        },
        headers=other_session,
    )
    assert other_item.status_code == 200, other_item.text
    other_id = other_item.json()["id"]

    token, _ = issue_token(client, session, name="隔离令牌")
    agent = headers_of(token)
    listing = client.get("/api/receivables", headers=agent)
    assert listing.status_code == 200
    assert listing.json()["total"] == 0
    hidden = client.get(f"/api/receivables/{other_id}", headers=agent)
    assert hidden.status_code == 404
    companies = client.get("/api/companies", headers=agent)
    assert companies.status_code == 200
    assert all(item["id"] != other_company["id"] for item in companies.json())
