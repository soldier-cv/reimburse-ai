import os
import hashlib
import secrets
from datetime import datetime, timedelta
import jwt
from fastapi import HTTPException, status, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from ..database import get_db, DATA_DIR
from ..models import User

# JWT 密钥初始化与持久化
SECRET_FILE = os.path.join(DATA_DIR, "jwt_secret.key")
if os.path.exists(SECRET_FILE):
    with open(SECRET_FILE, "r", encoding="utf-8") as f:
        JWT_SECRET = f.read().strip()
else:
    JWT_SECRET = os.environ.get("JWT_SECRET_KEY", secrets.token_hex(32))
    try:
        with open(SECRET_FILE, "w", encoding="utf-8") as f:
            f.write(JWT_SECRET)
    except Exception:
        pass

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 30 # 默认30天有效，个人使用更便利

security = HTTPBearer(auto_error=False)

def hash_password(password: str, salt: str = None) -> tuple[str, str]:
    """使用 PBKDF2-HMAC-SHA256 进行密码哈希"""
    if not salt:
        salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        100000
    ).hex()
    return hashed, salt

def verify_password(plain_password: str, salt: str, hashed: str) -> bool:
    new_hashed, _ = hash_password(plain_password, salt)
    return secrets.compare_digest(new_hashed, hashed)

def create_access_token(data: dict, expires_delta: timedelta = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS))
    to_encode.update({"exp": expire})
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
    支持以下三种方式提取 Token：
    1. Header: Authorization: Bearer <token>
    2. Cookie: expense_token=<token>
    3. Query Param: ?token=<token> (用于浏览器下载文件场景)
    """
    token = None
    if auth and auth.credentials:
        token = auth.credentials
    elif "expense_token" in request.cookies:
        token = request.cookies.get("expense_token")
    elif "token" in request.query_params:
        token = request.query_params.get("token")

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

    return user

def init_default_admin(db: Session):
    """系统初始化默认超级管理员账号"""
    admin_user = db.query(User).first()
    if not admin_user:
        init_username = os.environ.get("ADMIN_USERNAME", "admin")
        init_password = os.environ.get("ADMIN_PASSWORD", "Admin@2026!")
        hashed, salt = hash_password(init_password)
        new_user = User(
            username=init_username,
            password_hash=hashed,
            salt=salt,
            created_at=datetime.now()
        )
        db.add(new_user)
        db.commit()
        print(f"[AUTH INIT] 已初始化管理员账号: {init_username} / 初始密码: {init_password}")
