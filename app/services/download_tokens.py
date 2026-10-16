from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature

DOWNLOAD_TOKEN_TTL_SECONDS = 300

def get_serializer() -> URLSafeTimedSerializer:
    from .auth import JWT_SECRET
    return URLSafeTimedSerializer(secret_key=JWT_SECRET, salt="download-link-v1")

def make_download_token(kind: str, obj_id: int, user_id: int) -> str:
    """使用 itsdangerous 为特定文件/导出签发一次性限时下载凭证"""
    s = get_serializer()
    return s.dumps({"k": kind, "i": obj_id, "u": user_id})

def verify_download_token(token: str, max_age: int = DOWNLOAD_TOKEN_TTL_SECONDS) -> dict | None:
    """校验下载凭证并返回载荷，若过期或签名非法返回 None"""
    s = get_serializer()
    try:
        data = s.loads(token, max_age=max_age)
        return data
    except (SignatureExpired, BadSignature, Exception):
        return None
