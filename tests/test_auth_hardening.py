import os
import stat
import hmac
import hashlib
import secrets
from datetime import datetime
import pytest
from fastapi.testclient import TestClient

from app.models import User, Invoice
from app.services.auth import (
    hash_password, verify_password_and_check_rehash, load_or_create_jwt_secret
)
from app.services.auth_guard import guard_storage, unban
from app.services.download_tokens import make_download_token

def test_1_legacy_password_transparent_upgrade(client: TestClient, test_db):
    """
    场景 1：旧记录透明升级
    直接使用 legacy pbkdf2 算法插入旧用户，验证首次登录 200 且透明升级为 $argon2id$，
    再次使用原口令登录依然 200。
    """
    username = "legacy_user_test"
    raw_password = "LegacyPassword123!"
    salt = secrets.token_hex(16)
    legacy_hash = hashlib.pbkdf2_hmac(
        "sha256",
        raw_password.encode("utf-8"),
        salt.encode("utf-8"),
        100000
    ).hex()

    user = User(
        username=username,
        password_hash=legacy_hash,
        salt=salt,
        session_version=0,
        role="user",
        created_at=datetime.now()
    )
    test_db.add(user)
    test_db.commit()

    # 首次登录：使用旧口令
    resp1 = client.post("/api/auth/login", json={"username": username, "password": raw_password})
    assert resp1.status_code == 200, f"Legacy 登录失败: {resp1.text}"
    data1 = resp1.json()
    assert data1["success"] is True
    assert "token" in data1

    # 查库验证密码哈希升级为 argon2id，且 salt 置空
    test_db.refresh(user)
    assert user.password_hash.startswith("$argon2id$"), f"哈希未升级为 argon2id: {user.password_hash}"
    assert user.salt == "", f"升级后 salt 未清空: {user.salt}"

    # 再次登录：验证升级后使用同一口令依然成功
    resp2 = client.post("/api/auth/login", json={"username": username, "password": raw_password})
    assert resp2.status_code == 200, f"升级后二次登录失败: {resp2.text}"
    assert resp2.json()["success"] is True

def test_2_rehash_failure_does_not_lock_account(client: TestClient, test_db, monkeypatch):
    """
    场景 2：升级失败不锁死账号
    当写库升级哈希抛异常时，登录依然照常成功，账号可用。
    """
    username = "rehash_fail_user"
    raw_password = "RehashPassword123!"
    salt = secrets.token_hex(16)
    legacy_hash = hashlib.pbkdf2_hmac(
        "sha256",
        raw_password.encode("utf-8"),
        salt.encode("utf-8"),
        100000
    ).hex()

    user = User(
        username=username,
        password_hash=legacy_hash,
        salt=salt,
        session_version=0,
        role="user",
        created_at=datetime.now()
    )
    test_db.add(user)
    test_db.commit()

    # 模拟重哈希写库发生故障
    def mock_rehash_failure(*args, **kwargs):
        raise RuntimeError("Database write error during password rehash")

    monkeypatch.setattr("app.main.rehash_user_password_if_needed", mock_rehash_failure)

    # 即使升级写库失败，用户登录依然必须成功
    resp = client.post("/api/auth/login", json={"username": username, "password": raw_password})
    assert resp.status_code == 200
    assert resp.json()["success"] is True

def test_3_password_change_invalidates_old_tokens(client: TestClient):
    """
    场景 3：改密即失效旧 token
    验证 Header 与 Cookie 两条路径下旧 token 均被 401 拦截，新 token 正常 200。
    """
    username = "pwd_change_user"
    pwd1 = "OldPassword123!"
    pwd2 = "NewPassword456!"

    # 注册用户
    reg_resp = client.post("/api/auth/register", json={"username": username, "password": pwd1})
    assert reg_resp.status_code == 200

    # 登录获取 tokenA
    login_resp = client.post("/api/auth/login", json={"username": username, "password": pwd1})
    assert login_resp.status_code == 200
    tokenA = login_resp.json()["token"]

    # 验证 tokenA 当前有效
    me_resp1 = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tokenA}"})
    assert me_resp1.status_code == 200

    # 改密
    change_resp = client.post(
        "/api/auth/change-password",
        headers={"Authorization": f"Bearer {tokenA}"},
        json={"old_password": pwd1, "new_password": pwd2}
    )
    assert change_resp.status_code == 200
    tokenB = change_resp.json()["token"]
    assert tokenB != tokenA

    # 1. Header 路径断言：tokenA 必须 401，tokenB 必须 200
    me_old_header = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tokenA}"})
    assert me_old_header.status_code == 401, f"改密后旧 token Header 路径未失效: {me_old_header.text}"

    me_new_header = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tokenB}"})
    assert me_new_header.status_code == 200

    # 2. Cookie 路径断言：access_token=<tokenA> 必须 401，access_token=<tokenB> 必须 200
    me_old_cookie = client.get("/api/auth/me", cookies={"access_token": tokenA})
    assert me_old_cookie.status_code == 401, f"改密后旧 token Cookie 路径未失效: {me_old_cookie.text}"

    me_new_cookie = client.get("/api/auth/me", cookies={"access_token": tokenB})
    assert me_new_cookie.status_code == 200

def test_4_rate_limiting_and_ban_on_five_failures(client: TestClient):
    """
    场景 4：连续 5 次错误口令 → 第 6 次即使口令正确也 429 封禁拦截。
    解封后使用正确口令恢复 200。
    """
    username = "brute_force_user"
    correct_pwd = "CorrectPassword123!"
    wrong_pwd = "WrongPassword999!"

    # 注册用户
    reg_resp = client.post("/api/auth/register", json={"username": username, "password": correct_pwd})
    assert reg_resp.status_code == 200

    # 连续 5 次输入错误口令
    statuses = []
    for i in range(5):
        resp = client.post("/api/auth/login", json={"username": username, "password": wrong_pwd})
        statuses.append(resp.status_code)
        assert resp.status_code == 400, f"第 {i+1} 次错误口令期望 400，实际 {resp.status_code}"

    # 第 6 次请求：传入正确口令，断言即使口令正确也被 429 封禁拦截！
    resp6 = client.post("/api/auth/login", json={"username": username, "password": correct_pwd})
    assert resp6.status_code == 429, f"第 6 次正确口令未被封禁拦截，状态码为: {resp6.status_code}"
    assert resp6.json()["detail"] == "尝试过于频繁，请稍后再试"

    # 人工解除封禁
    unban("testclient")

    # 恢复后再次尝试正确口令登录，断言 200
    resp_recovered = client.post("/api/auth/login", json={"username": username, "password": correct_pwd})
    assert resp_recovered.status_code == 200
    assert resp_recovered.json()["success"] is True

def test_5_signed_download_links_security(client: TestClient, tmp_path, test_db):
    """
    场景 5：下载签名链接：正常签名 200、篡改 403、过期 403、越权跨用户 403。
    """
    # 注册两名用户 user1 与 user2
    client.post("/api/auth/register", json={"username": "user_dl_1", "password": "Password123!"})
    login1 = client.post("/api/auth/login", json={"username": "user_dl_1", "password": "Password123!"})
    token1 = login1.json()["token"]
    u1_id = login1.json().get("uid", 1)

    client.post("/api/auth/register", json={"username": "user_dl_2", "password": "Password123!"})
    login2 = client.post("/api/auth/login", json={"username": "user_dl_2", "password": "Password123!"})
    token2 = login2.json()["token"]
    u2_id = login2.json().get("uid", 2)

    # 在 tmp 目录下创建物理测试文件
    inv_file = tmp_path / "test_invoice.pdf"
    inv_file.write_bytes(b"%PDF-1.4 test invoice content 1234567890")

    inv = Invoice(
        user_id=u1_id,
        category_code="fuel",
        category_name="燃油费",
        month="2026-10",
        invoice_no="INV_TEST_001",
        amount=100.0,
        raw_filename="test_invoice.pdf",
        stored_path=str(inv_file)
    )
    test_db.add(inv)
    test_db.commit()
    test_db.refresh(inv)

    # 1. user1 正常请求签名链接
    sign_resp = client.get(
        f"/api/files/sign?kind=invoice&id={inv.id}",
        headers={"Authorization": f"Bearer {token1}"}
    )
    assert sign_resp.status_code == 200
    sign_data = sign_resp.json()
    assert "url" in sign_data
    assert sign_data["expires_in"] == 300

    # 2. 不带任何 Header/Cookie，直接访问签名 URL 下载文件 -> 200
    signed_url = sign_data["url"]
    dl_resp = client.get(signed_url)
    assert dl_resp.status_code == 200
    assert dl_resp.content == b"%PDF-1.4 test invoice content 1234567890"

    # 3. 篡改签名 t 的最后一位字符 -> 403
    tampered_url = signed_url + "tampered"
    tampered_resp = client.get(tampered_url)
    assert tampered_resp.status_code == 403
    assert tampered_resp.json()["detail"] == "下载链接无效或已过期"

    # 4. 已过期签名 -> 403
    expired_token = make_download_token(kind="invoice", obj_id=inv.id, user_id=u1_id)
    # 通过 monkeypatch 将 verify 的 max_age 改为 -1 来模拟过期
    from app.services import download_tokens
    orig_verify = download_tokens.verify_download_token
    def mock_verify_expired(tok, max_age=300):
        return orig_verify(tok, max_age=-1)
    
    import app.main
    monkeypatch_expired = pytest.MonkeyPatch()
    monkeypatch_expired.setattr(app.main, "verify_download_token", mock_verify_expired)
    expired_resp = client.get(f"/api/files/download/invoice/{inv.id}?t={expired_token}")
    assert expired_resp.status_code == 403
    assert expired_resp.json()["detail"] == "下载链接无效或已过期"
    monkeypatch_expired.undo()

    # 5. 跨用户越权：使用 user2 的签名去下载 user1 的 invoice -> 403
    u2_token = make_download_token(kind="invoice", obj_id=inv.id, user_id=u2_id)
    cross_resp = client.get(f"/api/files/download/invoice/{inv.id}?t={u2_token}")
    assert cross_resp.status_code == 403
    assert cross_resp.json()["detail"] == "下载链接无效或已过期"

def test_6_query_token_rejected_on_all_endpoints(client: TestClient):
    """
    场景 6：query 传凭证被拒
    验证 /api/invoices/{id}/file?token=<有效JWT> 与 /api/auth/me?token=<有效JWT> 均 401。
    """
    # 注册并登录获取合法 token
    client.post("/api/auth/register", json={"username": "query_token_user", "password": "Password123!"})
    login_resp = client.post("/api/auth/login", json={"username": "query_token_user", "password": "Password123!"})
    token = login_resp.json()["token"]

    # 显式清除持久化 Cookies，确保请求只带 query token 而无任何 Header/Cookie 凭据
    client.cookies.clear()

    # 1. /api/invoices/1/file?token=<token> 不带 Header/Cookie -> 401
    resp1 = client.get(f"/api/invoices/1/file?token={token}")
    assert resp1.status_code == 401, f"带 query token 仍通过了: {resp1.status_code}"

    # 2. /api/auth/me?token=<token> 不带 Header/Cookie -> 401
    resp2 = client.get(f"/api/auth/me?token={token}")
    assert resp2.status_code == 401, f"带 query token 仍通过了: {resp2.status_code}"

def test_7_password_boundary_validation(client: TestClient):
    """
    场景 7：口令边界测试
    空口令注册 4xx 非 500、129 字符口令 4xx、中文字符与 emoji 口令成功、登录空口令 4xx。
    """
    # 1. 空口令注册 -> 400
    r_empty = client.post("/api/auth/register", json={"username": "user_empty_pwd", "password": ""})
    assert 400 <= r_empty.status_code < 500
    assert r_empty.status_code != 500

    # 2. 129 字符口令注册 -> 400
    r_129 = client.post("/api/auth/register", json={"username": "user_129_pwd", "password": "A" * 129})
    assert 400 <= r_129.status_code < 500

    # 3. 含中文与特殊字符口令 -> 注册与登录均成功
    special_pwd = "密码Complex@888中文符号"
    r_spec_reg = client.post("/api/auth/register", json={"username": "special_pwd_user", "password": special_pwd})
    assert r_spec_reg.status_code == 200

    r_spec_login = client.post("/api/auth/login", json={"username": "special_pwd_user", "password": special_pwd})
    assert r_spec_login.status_code == 200
    assert r_spec_login.json()["success"] is True

    # 4. 登录传空口令 -> 400
    r_login_empty = client.post("/api/auth/login", json={"username": "special_pwd_user", "password": ""})
    assert 400 <= r_login_empty.status_code < 500

def test_8_secret_file_permissions_and_fail_closed(tmp_path, monkeypatch):
    """
    场景 8：SECRET_FILE 权限收紧为 0600 且 fail-closed（空文件抛 RuntimeError）。
    """
    import app.services.auth as auth_mod

    # 1. 验证新生成 secret 文件权限为 0600
    test_key_file = tmp_path / "test_jwt_secret.key"
    monkeypatch.setattr(auth_mod, "SECRET_FILE", str(test_key_file))
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)

    secret = auth_mod.load_or_create_jwt_secret()
    assert len(secret) > 0
    file_mode = os.stat(test_key_file).st_mode & 0o777
    assert file_mode == 0o600, f"权限不是 0600: {oct(file_mode)}"

    # 2. 验证空文件 fail-closed：文件存在但内容为空时抛 RuntimeError
    test_key_file.write_text("   \n  ", encoding="utf-8")
    with pytest.raises(RuntimeError) as exc_info:
        auth_mod.load_or_create_jwt_secret()
    assert "fail-closed" in str(exc_info.value)
