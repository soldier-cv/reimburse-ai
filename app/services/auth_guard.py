from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter
from limits.limits import RateLimitItemPerHour
from fastapi import HTTPException, status

# 模块级常量
AUTH_FAIL_THRESHOLD = 5
AUTH_FAIL_WINDOW = "1/hour"
BAN_DURATION_SECONDS = 900 # 15 分钟

# limits 内存存储与滑动窗口限流器（进程内状态，多副本/重启后清零）
guard_storage = MemoryStorage()
guard_limiter = MovingWindowRateLimiter(guard_storage)
auth_fail_limit = RateLimitItemPerHour(AUTH_FAIL_THRESHOLD)

def is_banned(ip: str) -> bool:
    """判定指定 IP 是否处于封禁期"""
    val = guard_storage.get(f"ban:{ip}")
    return bool(val and val > 0)

def check_auth_banned(ip: str) -> None:
    """封禁判定在口令校验之前执行，封禁期内一律 429 且不泄露机制细节"""
    if is_banned(ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="尝试过于频繁，请稍后再试"
        )

def record_auth_failure(ip: str) -> None:
    """
    记录一次口令失败：
    若在滑动 1 小时窗口内失败达 5 次（hit 为 False 或 remaining==0），立即建立 15 分钟封禁。
    """
    allowed = guard_limiter.hit(auth_fail_limit, "authfail", ip)
    stats = guard_limiter.get_window_stats(auth_fail_limit, "authfail", ip)
    if (not allowed) or (stats.remaining == 0):
        guard_storage.incr(f"ban:{ip}", expiry=BAN_DURATION_SECONDS)

def clear_auth_failures(ip: str) -> None:
    """成功登录时清除该 IP 的失败计数（不解除封禁状态）"""
    key = auth_fail_limit.key_for("authfail", ip)
    guard_storage.clear(key)

def unban(ip: str) -> None:
    """用于测试或人工解封指定 IP"""
    guard_storage.clear(f"ban:{ip}")
