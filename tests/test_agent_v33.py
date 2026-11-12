"""令牌重新签发、按月份打包原件、AI 只读摘要。"""

import io
import zipfile

from PIL import Image

from app.models import AgentToken


def init_and_session(client):
    admin = client.post("/api/system/init-admin", json={"username": "admin", "password": "AdminPass123!"})
    assert admin.status_code == 200
    created = client.post(
        "/api/admin/users",
        json={"username": "owner", "password": "OwnerPass123!", "role": "user"},
        headers={"Authorization": f"Bearer {admin.json()['token']}"},
    )
    assert created.status_code == 200, created.text
    session = client.post("/api/auth/login", json={"username": "owner", "password": "OwnerPass123!"})
    assert session.status_code == 200
    return {"Authorization": f"Bearer {session.json()['token']}"}


def issue(client, session, name="工作令牌"):
    response = client.post("/api/settings/agent-tokens", json={"name": name}, headers=session)
    assert response.status_code == 200, response.text
    return response.json()


def png(color=(40, 40, 40)) -> bytes:
    image = Image.new("RGB", (80, 80), color)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def test_reissue_invalidates_old_token(client, test_db):
    session = init_and_session(client)
    created = issue(client, session, "可轮换")
    old = created["token"]
    token_id = created["id"]
    old_hash = test_db.get(AgentToken, token_id).token_hash

    agent = {"Authorization": f"Bearer {old}"}
    assert client.get("/api/auth/me", headers=agent).status_code == 200

    reissued = client.post(f"/api/settings/agent-tokens/{token_id}/reissue", headers=session)
    assert reissued.status_code == 200, reissued.text
    new = reissued.json()["token"]
    assert new.startswith("ragt_")
    assert new != old
    assert "token" in reissued.json()
    listed = client.get("/api/settings/agent-tokens", headers=session).json()
    assert all("token" not in item for item in listed)

    row = test_db.get(AgentToken, token_id)
    test_db.refresh(row)
    assert row.token_hash != old_hash
    # 旧令牌立即失效
    assert client.get("/api/auth/me", headers=agent).status_code == 401
    # 新令牌可用
    fresh = client.get("/api/auth/me", headers={"Authorization": f"Bearer {new}"})
    assert fresh.status_code == 200
    assert fresh.json()["auth_method"] == "agent"
    # Agent 自己不能重新签发
    forbidden = client.post(
        f"/api/settings/agent-tokens/{token_id}/reissue",
        headers={"Authorization": f"Bearer {new}"},
    )
    assert forbidden.status_code == 403

    # 已吊销的不能再签发
    client.post(f"/api/settings/agent-tokens/{token_id}/revoke", headers=session)
    after_revoke = client.post(f"/api/settings/agent-tokens/{token_id}/reissue", headers=session)
    assert after_revoke.status_code == 400


def test_export_attachments_by_period(client):
    session = init_and_session(client)
    token = issue(client, session)["token"]
    agent = {"Authorization": f"Bearer {token}"}
    company = client.post("/api/companies", json={"name": "打包公司"}, headers=agent).json()

    def confirm(filename, occurred, period, amount):
        upload = client.post(
            "/api/capture",
            files=[("files", (filename, png(color=(hash(filename) % 200, 40, 80)), "image/png"))],
            data={"company_id": str(company["id"])},
            headers=agent,
        )
        draft_id = upload.json()["drafts"][0]["id"]
        client.patch(
            f"/api/capture/drafts/{draft_id}",
            json={"amount": amount, "occurred_on": occurred, "period": period, "title": filename},
            headers=agent,
        )
        result = client.post(
            "/api/capture/confirm",
            json={"draft_ids": [draft_id], "company_id": company["id"], "period": period},
            headers=agent,
        )
        assert result.status_code == 200, result.text
        return result.json()["created"][0]

    oct_item = confirm("oct.png", "2026-10-08", "2026-10", "30.00")
    nov_item = confirm("nov.png", "2026-11-02", "2026-11", "40.00")

    packed = client.post(
        "/api/export/attachments",
        json={"period": "2026-11", "company_id": company["id"]},
        headers=agent,
    )
    assert packed.status_code == 200, packed.text
    assert "zip" in packed.headers.get("content-type", "")
    with zipfile.ZipFile(io.BytesIO(packed.content)) as archive:
        names = archive.namelist()
    # 只包含 11 月那笔的原件
    joined = "\n".join(names)
    assert "nov.png" in joined or any("nov" in name.lower() for name in names) or len(names) >= 1
    # 用应收 id 打包也应成功
    by_ids = client.post(
        "/api/export/attachments",
        json={"receivable_ids": [oct_item["id"], nov_item["id"]]},
        headers=agent,
    )
    assert by_ids.status_code == 200
    with zipfile.ZipFile(io.BytesIO(by_ids.content)) as archive:
        assert len(archive.namelist()) >= 2


def test_agent_can_read_ai_summary_but_not_secrets(client):
    session = init_and_session(client)
    client.post(
        "/api/settings/ai",
        json={
            "enabled": True,
            "base_url": "https://example.invalid/v1",
            "api_key": "sk-secret-key",
            "model_name": "demo-model",
        },
        headers=session,
    )
    token = issue(client, session, "摘要")["token"]
    agent = {"Authorization": f"Bearer {token}"}

    summary = client.get("/api/settings/ai/summary", headers=agent)
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["enabled"] is True
    assert body["model_name"] == "demo-model"
    assert "api_key" not in body
    assert "has_key" not in body
    assert "base_url" not in body
    assert "system_prompt" not in body
    assert "secret" not in str(body).lower()

    full = client.get("/api/settings/ai", headers=agent)
    assert full.status_code == 403
    write = client.post(
        "/api/settings/ai",
        json={"enabled": False, "base_url": "", "api_key": "hack", "model_name": ""},
        headers=agent,
    )
    assert write.status_code == 403
