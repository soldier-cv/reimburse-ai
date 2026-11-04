"""金额与日期工具：全库金额使用整数分，避免浮点误差。"""

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re


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


def today_str() -> str:
    return date.today().isoformat()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
    return datetime.now().strftime("%Y-%m")


def add_days(value: str, days: int) -> str:
    parsed = parse_date(value) or date.today()
    return (parsed + timedelta(days=int(days or 0))).isoformat()


def days_overdue(expected_on: str, today: date | None = None) -> int:
    """逾期天数，未逾期返回 0。"""
    if not expected_on:
        return 0
    parsed = parse_date(expected_on)
    if not parsed:
        return 0
    current = today or date.today()
    return max(0, (current - parsed).days)


def month_range(months: int = 6) -> list[str]:
    """返回最近 N 个月的 YYYY-MM 列表（含当月，升序）。"""
    today = date.today()
    result = []
    year, month = today.year, today.month
    for _ in range(months):
        result.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return list(reversed(result))
