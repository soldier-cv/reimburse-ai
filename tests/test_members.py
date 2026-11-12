"""成员停用、软删除与创建管理员二次确认。"""

import os
from pathlib import Path

from app.models import Company, Receivable, User
from app.services.capture import UPLOAD_ROOT, user_dir


def admin_headers(client):
    response = client.post("/api/system/init-admin", json={"username": "admin", "password": "AdminPass123!"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


MEMBER_PASSWORD = "TestMemberPass123"


def create_member(client, admin, username="alice", password=MEMBER_PASSWORD, role="user", confirm_admin=False):
    payload = {"username": username, "password": password, "role": role}
    if confirm_admin:
        payload["confirm_admin"] = True
    response = client.post("/api/admin/users", json=payload, headers=admin)
    return response


def login(client, username, password):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    return response


def test_creating_admin_requires_explicit_confirmation(client):
    admin = admin_headers(client)
    denied = create_member(client, admin, username="boss", role="admin")
    assert denied.status_code == 400
    assert "二次确认" in denied.json()["detail"]
    allowed = create_member(client, admin, username="boss", role="admin", confirm_admin=True)
    assert allowed.status_code == 200, allowed.text
    member = create_member(client, admin, username="normal", role="user")
    assert member.status_code == 200


def test_disable_blocks_login_and_invalidates_session(client):
    admin = admin_headers(client)
    assert create_member(client, admin, username="alice").status_code == 200
    logged = login(client, "alice", MEMBER_PASSWORD)
    assert logged.status_code == 200
    token = logged.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200

    users = client.get("/api/admin/users", headers=admin).json()
    alice = next(item for item in users if item["username"] == "alice")
    disabled = client.post(f"/api/admin/users/{alice['id']}/disable", headers=admin)
    assert disabled.status_code == 200, disabled.text

    # 旧会话立即失效
    stale = client.get("/api/auth/me", headers=headers)
    assert stale.status_code == 401
    # 无法再登录
    again = login(client, "alice", MEMBER_PASSWORD)
    assert again.status_code == 400
    assert "停用" in again.json()["detail"]

    enabled = client.post(f"/api/admin/users/{alice['id']}/enable", headers=admin)
    assert enabled.status_code == 200
    restored = login(client, "alice", MEMBER_PASSWORD)
    assert restored.status_code == 200


def test_soft_delete_cleans_uploads_and_keeps_db_rows(client, test_db):
    admin = admin_headers(client)
    assert create_member(client, admin, username="bob").status_code == 200
    logged = login(client, "bob", MEMBER_PASSWORD)
    assert logged.status_code == 200, logged.text
    headers = {"Authorization": f"Bearer {logged.json()['token']}"}
    company = client.post("/api/companies", json={"name": "鲍勃的公司"}, headers=headers)
    assert company.status_code == 200
    receivable = client.post(
        "/api/receivables",
        json={"company_id": company.json()["id"], "title": "报销", "amount": 10, "occurred_on": "2026-11-01"},
        headers=headers,
    )
    assert receivable.status_code == 200

    users = client.get("/api/admin/users", headers=admin).json()
    bob = next(item for item in users if item["username"] == "bob")
    # 写入该用户上传目录
    upload_dir = user_dir(bob["id"], "drafts")
    sample = Path(upload_dir) / "keep-me.txt"
    sample.write_text("secret", encoding="utf-8")
    assert sample.exists()
    # 另一个用户目录不应被误删
    other_dir = user_dir(1, "drafts")
    other = Path(other_dir) / "admin-file.txt"
    other.write_text("admin", encoding="utf-8")

    deleted = client.delete(f"/api/admin/users/{bob['id']}", headers=admin)
    assert deleted.status_code == 200, deleted.text

    row = test_db.get(User, bob["id"])
    assert row is not None
    assert row.deleted_at is not None
    assert row.is_active is False
    # 账本数据仍在
    assert test_db.get(Receivable, receivable.json()["id"]) is not None
    # 该用户上传已清理
    assert not Path(os.path.join(UPLOAD_ROOT, f"user_{bob['id']}")).exists()
    # 其他用户文件还在
    assert other.exists()

    stale = client.get("/api/auth/me", headers=headers)
    assert stale.status_code == 401
    again = login(client, "bob", MEMBER_PASSWORD)
    assert again.status_code == 400

    listed = client.get("/api/admin/users", headers=admin).json()
    bob_row = next(item for item in listed if item["username"] == "bob")
    assert bob_row["deleted"] is True
    assert bob_row["status"] == "已删除"

    enable = client.post(f"/api/admin/users/{bob['id']}/enable", headers=admin)
    assert enable.status_code == 400
