"""金额与日期工具：全库金额使用整数分，避免浮点误差。"""

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo
import re

# 业务「今天」统一按上海时区，避免服务器 UTC 在夜间把日期算成昨天。
APP_TZ = ZoneInfo("Asia/Shanghai")


def yuan_to_cents(value) -> int:
    """把元金额转换为整数分，兼容字符串、千分位、货币符号。"""
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        raise ValueError("金额格式不正确")
    if isinstance(value, int):
        return value * 100
    if isinstance(value, float):
        return int(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)

    text = str(value).strip().replace(",", "").replace("￥", "").replace("¥", "")
    text = re.sub(r"[^\d.\-]", "", text)
    if not text or text in ("-", ".", "-."):
        raise ValueError("金额格式不正确")
    try:
        dec = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("金额格式不正确") from exc
    if dec != dec.quantize(Decimal("0.01")):
        dec = dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(dec * 100)


def cents_to_yuan(cents) -> float:
    if cents is None:
        return 0.0
    return round(int(cents) / 100, 2)


def cents_to_text(cents) -> str:
    if cents is None:
        return "0.00"
    return f"{int(cents) / 100:.2f}"


def today_local() -> date:
    """返回 Asia/Shanghai 的今天。"""
    return datetime.now(APP_TZ).date()


def today_str() -> str:
    return today_local().isoformat()


def now_str() -> str:
    return datetime.now(APP_TZ).strftime("%Y-%m-%d %H:%M:%S")


def current_month() -> str:
    return today_local().strftime("%Y-%m")


def normalize_period(value: str, fallback: str = "") -> str:
    """把各种月份写法规范为 YYYY-MM；无法解析时返回 fallback。"""
    if value is None or str(value).strip() == "":
        return fallback
    text = str(value).strip()
    for fmt in ("%Y-%m", "%Y/%m", "%Y.%m", "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m")
        except ValueError:
            continue
    m = re.search(r"(20\d{2})[-/.年]?\s*(\d{1,2})", text)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12:
            return f"{year:04d}-{month:02d}"
    return fallback


def parse_date(value: str) -> date | None:
    """解析常见日期格式，失败返回 None。"""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    m = re.search(r"(20\d{2})[^\d]?(\d{1,2})[^\d]?(\d{1,2})?", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3) or 1))
        except ValueError:
            return None
    return None


def normalize_date(value: str, fallback: str = "") -> str:
    parsed = parse_date(value)
    if parsed:
        return parsed.isoformat()
    return fallback or today_str()


def period_from_date(value: str) -> str:
    parsed = parse_date(value)
    if parsed:
        return parsed.strftime("%Y-%m")
    return current_month()


def add_days(value: str, days: int) -> str:
    parsed = parse_date(value) or today_local()
    return (parsed + timedelta(days=int(days or 0))).isoformat()


def days_overdue(expected_on: str, today: date | None = None) -> int:
    """逾期天数，未逾期返回 0。"""
    if not expected_on:
        return 0
    parsed = parse_date(expected_on)
    if not parsed:
        return 0
    current = today or today_local()
    return max(0, (current - parsed).days)


def months_between(start: str, end: str) -> list[str]:
    """含起止的 YYYY-MM 列表；非法输入返回空列表。"""
    start_p = normalize_period(start)
    end_p = normalize_period(end)
    if not start_p or not end_p or start_p > end_p:
        return []
    year, month = [int(part) for part in start_p.split("-")]
    end_year, end_month = [int(part) for part in end_p.split("-")]
    result = []
    while (year, month) <= (end_year, end_month):
        result.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            month = 1
            year += 1
    return result


def month_payday(period: str, payday: int) -> str:
    """某归属月对应的发薪日（超出当月天数则取月末）。"""
    import calendar as _cal
    normalized = normalize_period(period)
    if not normalized:
        return today_str()
    year, month = [int(part) for part in normalized.split("-")]
    last = _cal.monthrange(year, month)[1]
    day = min(max(int(payday or 1), 1), last)
    return date(year, month, day).isoformat()


def month_range(months: int = 6) -> list[str]:
    """返回最近 N 个月的 YYYY-MM 列表（含当月，升序）。"""
    today = today_local()
    result = []
    year, month = today.year, today.month
    for _ in range(months):
        result.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return list(reversed(result))
