import os
import hmac
import hashlib
import secrets
from datetime import datetime, timedelta
import jwt
import argon2
import argon2.exceptions
from fastapi import HTTPException, status, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from ..database import get_db, DATA_DIR
from ..models import User

# JWT 密钥初始化与持久化（权限收紧为 0600，且 fail-closed）
SECRET_FILE = os.path.join(DATA_DIR, "jwt_secret.key")

def load_or_create_jwt_secret() -> str:
    env_secret = os.environ.get("JWT_SECRET_KEY", "").strip()
    if env_secret:
        if os.path.exists(SECRET_FILE):
            try:
                os.chmod(SECRET_FILE, 0o600)
            except OSError:
                pass
        return env_secret

    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            raise RuntimeError(f"JWT secret file {SECRET_FILE} exists but is empty or only whitespace (fail-closed)")
        try:
            os.chmod(SECRET_FILE, 0o600)
        except OSError:
            pass
        return content

    new_secret = secrets.token_hex(32)
    try:
        fd = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with open(fd, "w", encoding="utf-8") as f:
            f.write(new_secret)
        return new_secret
    except Exception as e:
        raise RuntimeError(f"Failed to persist JWT secret to {SECRET_FILE} (fail-closed): {e}")

JWT_SECRET = load_or_create_jwt_secret()

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 7 # 会话有效期收窄至 7 天

security = HTTPBearer(auto_error=False)
pwd_hasher = argon2.PasswordHasher()

def _verify_legacy_pbkdf2(plain_password: str, salt: str, hashed: str) -> bool:
    # 仅用于校验/迁移旧记录（PBKDF2-HMAC-SHA256 遗留兼容分支）
    legacy_hash = hashlib.pbkdf2_hmac(
        "sha256",
        plain_password.encode("utf-8"),
        salt.encode("utf-8"),
        100000
    ).hex()
    return hmac.compare_digest(legacy_hash, hashed)

def hash_password(password: str, salt: str = None) -> tuple[str, str]:
    """使用 argon2id 进行密码哈希，salt 作为遗留兼容返回值固定为空字符串"""
    hashed = pwd_hasher.hash(password)
    return hashed, ""

def verify_password_and_check_rehash(plain_password: str, salt: str, hashed: str) -> tuple[bool, bool]:
    """
    校验密码并判断是否需要升级哈希：
    返回 (is_valid, needs_rehash)。
    若为 legacy pbkdf2 密码，校验成功后 needs_rehash 为 True。
    若为 argon2 哈希，捕获所有校验异常返回 False，避免冒泡成 500。
    """
    if not plain_password or not hashed:
        return False, False

    if hashed.startswith("$argon2"):
        try:
            pwd_hasher.verify(hashed, plain_password)
            needs_rehash = pwd_hasher.check_needs_rehash(hashed)
            return True, needs_rehash
        except (
            argon2.exceptions.VerifyMismatchError,
            argon2.exceptions.VerificationError,
            argon2.exceptions.InvalidHashError,
        ):
            return False, False
        except Exception:
            return False, False
    else:
        valid = _verify_legacy_pbkdf2(plain_password, salt or "", hashed)
        return valid, True if valid else False

def verify_password(plain_password: str, salt: str, hashed: str) -> bool:
    valid, _ = verify_password_and_check_rehash(plain_password, salt, hashed)
    return valid

def rehash_user_password_if_needed(db: Session, user: User, plain_password: str, needs_rehash: bool) -> None:
    """
    登录成功且 needs_rehash（含 legacy）时重算并写回 argon2 哈希。
    升级失败用 try/except 包住，绝不锁死账号。
    """
    if not needs_rehash:
        return
    try:
        new_hash, _ = hash_password(plain_password)
        user.password_hash = new_hash
        user.salt = ""
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[AUTH WARNING] 密码哈希透明升级写库失败，跳过以保障登录正常: {e}")

def create_access_token(data: dict, expires_delta: timedelta = None) -> str:
    to_encode = data.copy()
    now = datetime.utcnow()
    expire = now + (expires_delta or timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS))
    to_encode.update({
        "exp": expire,
        "iat": int(now.timestamp())
    })
    return jwt.encode(to_encode, JWT_SECRET, algorithm=ALGORITHM)

def decode_access_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        return payload
    except jwt.PyJWTError:
        return None

def get_current_user(
    request: Request,
    auth: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
) -> User:
    """
    鉴权中间件 / 依赖：
    支持以下两种凭据传递方式（彻底禁止 query param 传 token）：
    1. Header: Authorization: Bearer <token>
    2. Cookie: access_token=<token> 或 expense_token=<token>
    """
    token = None
    if auth and auth.credentials:
        token = auth.credentials
    elif "access_token" in request.cookies:
        token = request.cookies.get("access_token")
    elif "expense_token" in request.cookies:
        token = request.cookies.get("expense_token")

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="请先登录系统",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_access_token(token)
    if not payload or "sub" not in payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="认证令牌已失效或无效，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )

    username = payload["sub"]
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 校验 session_version：缺失或与数据库不一致时判 401（改密即失效旧 token，存量旧 token 统一要求重新登录）
    token_sv = payload.get("sv")
    if token_sv is None or token_sv != user.session_version:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="认证令牌已失效或无效，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user

def init_default_admin(db: Session):
    """
    系统管理员账号初始化策略：
    1. 若已有 role='admin' 的管理员，直接跳过；
    2. 若已有账号名为 'admin'，升级其 role 为 'admin'；
    3. 若显式配置了环境变量 ADMIN_USERNAME 与 ADMIN_PASSWORD，自动初始化；
    4. 否则保持未初始化状态，由 Web 端首次启动界面向导引导用户自主设置管理员账密。
    """
    admin_user = db.query(User).filter(User.role == "admin").first()
    if admin_user:
        return

    existing_named_admin = db.query(User).filter(User.username == "admin").first()
    if existing_named_admin:
        existing_named_admin.role = "admin"
        db.commit()
        return

    init_username = os.environ.get("ADMIN_USERNAME", "").strip()
    init_password = os.environ.get("ADMIN_PASSWORD", "").strip()
    if init_username and init_password:
        hashed, salt = hash_password(init_password)
        new_user = User(
            username=init_username,
            password_hash=hashed,
            salt=salt,
            session_version=0,
            role="admin",
            created_at=datetime.now()
        )
        db.add(new_user)
        db.commit()
        print(f"[AUTH INIT] 已根据环境变量自动初始化超级管理员: {init_username}")
