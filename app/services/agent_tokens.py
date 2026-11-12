"""Agent 访问令牌：签发、校验与权限范围。

明文令牌只在创建时返回一次；库中只保存 SHA-256 哈希。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from ..models import AgentToken

TOKEN_PREFIX = "ragt_"
TOKEN_DISPLAY_PREFIX_LEN = 12
ALL_SCOPES = ("capture", "ledger", "export")
SCOPE_LABELS = {
    "capture": "采集",
    "ledger": "台账",
    "export": "导出",
}
MAX_TOKENS_PER_USER = 20
AGENT_FORBIDDEN_MESSAGE = "Agent 令牌不能用于账号管理、令牌管理或 AI 密钥设置"

# 这些路径即使已通过 Agent 鉴权也一律拒绝
AGENT_BLOCKED_PREFIXES = (
    "/api/admin",
    "/api/settings/ai",
    "/api/settings/agent-tokens",
    "/api/auth/change-password",
    "/api/auth/logout",
    "/api/auth/register",
    "/api/system/init-admin",
)

# 已鉴权的 Agent 可访问、且不要求业务范围的路径
AGENT_OPEN_PATHS = {"/api/auth/me", "/api/settings/ai/summary", "/api/help/user-guide"}

# 按前缀匹配业务范围；更长的前缀应排在前面
AGENT_SCOPE_PREFIXES = (
    ("/api/capture", "capture"),
    ("/api/export", "export"),
    ("/api/companies", "ledger"),
    ("/api/categories", "ledger"),
    ("/api/receivables", "ledger"),
    ("/api/batches", "ledger"),
    ("/api/payments", "ledger"),
    ("/api/dashboard", "ledger"),
    ("/api/attachments", "ledger"),
    ("/api/audit-logs", "ledger"),
    ("/api/payroll-plans", "ledger"),
)


def hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_plaintext_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def display_prefix(plaintext: str) -> str:
    return plaintext[:TOKEN_DISPLAY_PREFIX_LEN]


def parse_scopes(raw) -> list[str]:
    if raw is None:
        return list(ALL_SCOPES)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return list(ALL_SCOPES)
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            raw = [part.strip() for part in text.split(",") if part.strip()]
    if not isinstance(raw, (list, tuple)):
        raise HTTPException(status_code=400, detail="权限范围格式不正确")
    scopes = []
    for item in raw:
        name = str(item).strip().lower()
        if not name:
            continue
        if name not in ALL_SCOPES:
            raise HTTPException(status_code=400, detail=f"不支持的权限范围：{name}")
        if name not in scopes:
            scopes.append(name)
    if not scopes:
        raise HTTPException(status_code=400, detail="至少需要选择一个权限范围")
    return scopes


def scopes_from_row(row: AgentToken) -> list[str]:
    try:
        data = json.loads(row.scopes or "[]")
        if isinstance(data, list):
            return [str(item) for item in data if str(item) in ALL_SCOPES]
    except json.JSONDecodeError:
        pass
    return []


def store_scopes(scopes: list[str]) -> str:
    return json.dumps(scopes, ensure_ascii=False)


def parse_expires_at(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            dt = datetime.fromisoformat(text).replace(hour=23, minute=59, second=59)
        else:
            dt = datetime.fromisoformat(text)
    except ValueError:
        raise HTTPException(status_code=400, detail="过期时间格式不正确，请使用 YYYY-MM-DD 或 ISO 8601")
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _as_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def token_is_expired(row: AgentToken, now: datetime | None = None) -> bool:
    if row.expires_at is None:
        return False
    current = _as_naive_utc(now or datetime.now(timezone.utc))
    return _as_naive_utc(row.expires_at) <= current


def format_dt(dt: datetime | None) -> str | None:
    if not dt:
        return None
    return _as_naive_utc(dt).strftime("%Y-%m-%d %H:%M:%S")


def serialize_token(row: AgentToken) -> dict:
    scopes = scopes_from_row(row)
    expired = token_is_expired(row)
    if row.revoked_at is not None:
        status_label = "revoked"
        status_text = "已吊销"
    elif not row.enabled:
        status_label = "disabled"
        status_text = "已停用"
    elif expired:
        status_label = "expired"
        status_text = "已过期"
    else:
        status_label = "active"
        status_text = "有效"
    return {
        "id": row.id,
        "name": row.name,
        "token_prefix": row.token_prefix,
        "scopes": scopes,
        "enabled": bool(row.enabled) and row.revoked_at is None,
        "status": status_label,
        "status_label": status_text,
        "expires_at": format_dt(row.expires_at),
        "last_used_at": format_dt(row.last_used_at),
        "created_at": format_dt(row.created_at),
        "revoked_at": format_dt(row.revoked_at),
    }


def lookup_agent_token(db: Session, plaintext: str) -> AgentToken | None:
    if not plaintext or not plaintext.startswith(TOKEN_PREFIX):
        return None
    digest = hash_token(plaintext)
    row = db.query(AgentToken).filter(AgentToken.token_hash == digest).first()
    if not row or not hmac.compare_digest(row.token_hash, digest):
        return None
    return row


def authenticate_agent_token(db: Session, plaintext: str) -> AgentToken:
    row = lookup_agent_token(db, plaintext)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="认证令牌已失效或无效，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if row.revoked_at is not None or not row.enabled or token_is_expired(row):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="认证令牌已失效或无效，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return row


def touch_last_used(db: Session, token_id: int) -> None:
    """更新最近使用时间；写库失败不影响本次请求。"""
    try:
        # 使用与当前请求相同的引擎，避免测试覆盖 get_db 后写到默认库
        from sqlalchemy.orm import sessionmaker

        session = sessionmaker(bind=db.get_bind())()
        try:
            session.query(AgentToken).filter(AgentToken.id == token_id).update(
                {"last_used_at": datetime.now()},
                synchronize_session=False,
            )
            session.commit()
        finally:
            session.close()
    except Exception:
        pass


def required_scope_for_path(path: str, request=None) -> str | None:
    """返回该路径所需范围；None 表示不在业务范围表中（需另作判断）。"""
    normalized = path.rstrip("/") or "/"
    if normalized.startswith("/api/files/sign"):
        kind = ""
        if request is not None:
            kind = (request.query_params.get("kind") or "").strip()
        return "export" if kind == "export" else "ledger"
    for prefix, scope in AGENT_SCOPE_PREFIXES:
        if normalized == prefix or normalized.startswith(prefix + "/"):
            return scope
    return None


def enforce_agent_access(path: str, scopes: list[str], request=None) -> None:
    normalized = (path or "").rstrip("/") or "/"
    if normalized in AGENT_OPEN_PATHS:
        return
    for prefix in AGENT_BLOCKED_PREFIXES:
        if normalized == prefix or normalized.startswith(prefix + "/"):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=AGENT_FORBIDDEN_MESSAGE)
    scope = required_scope_for_path(normalized, request)
    if not scope:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=AGENT_FORBIDDEN_MESSAGE)
    if scope not in scopes:
        label = SCOPE_LABELS.get(scope, scope)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"当前 Agent 令牌缺少「{label}」权限",
        )


def reject_if_agent_bearer(authorization: str | None) -> None:
    header = authorization or ""
    scheme, _, credential = header.partition(" ")
    if scheme.lower() == "bearer" and credential.strip().startswith(TOKEN_PREFIX):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=AGENT_FORBIDDEN_MESSAGE)
