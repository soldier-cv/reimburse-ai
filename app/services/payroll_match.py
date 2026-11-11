"""工资到账：推断归属月并给出核销建议。

只产出建议，不自动入账。规则按优先级：
1. 备注里写明月份（6月工资、6、7月工资、2025年6-8月工资），以备注为准；
2. 备注没写月份，到账金额正好等于最早 N 笔未结清工资合计；
3. 备注没写月份，到账金额约为 N × 参考月薪（该公司历史工资中位数）；
4. 都对不上，按最早欠发顺序拆出候选并降低把握，交人工核对。
"""

import re
from datetime import date
from statistics import median

from sqlalchemy.orm import Session

from ..models import Receivable
from .money import cents_to_text, parse_date

SALARY_KEYWORDS = ("工资", "薪资", "薪酬", "月薪", "发薪", "代发", "补发", "奖金", "绩效", "年终奖", "十三薪")
OPEN_STATUSES = ("pending", "submitted", "partial")
CN_MONTHS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
    "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
}
FULLWIDTH = str.maketrans({"，": ",", "、": ",", "。": ".", "；": ";", "：": ":",
                           "（": "(", "）": ")", "～": "~", "－": "-", "—": "-", "–": "-", "至": "-", "到": "-"})


def normalize_remark(text: str) -> str:
    return (text or "").translate(FULLWIDTH)


def _month_number(token: str) -> int:
    token = (token or "").strip()
    if token.isdigit():
        return int(token)
    return CN_MONTHS.get(token, 0)


def parse_month_mentions(remark: str, paid_on: str = "") -> list[str]:
    """从备注里解析出归属月，返回升序的 YYYY-MM 列表。"""
    text = normalize_remark(remark)
    if not text:
        return []
    paid = parse_date(paid_on) or date.today()
    years = [(match.start(), int(match.group(1))) for match in re.finditer(r"(20\d{2})\s*年", text)]

    tokens: list[tuple[int, int]] = []  # (位置, 月份)
    consumed: list[tuple[int, int]] = []

    # 区间：6-8月 / 6月-8月
    for match in re.finditer(
        r"([0-9]{1,2}|[一二三四五六七八九十]{1,3})\s*月?\s*-\s*([0-9]{1,2}|[一二三四五六七八九十]{1,3})\s*月", text
    ):
        start, end = _month_number(match.group(1)), _month_number(match.group(2))
        if start and end and 1 <= start <= end <= 12:
            for month in range(start, end + 1):
                tokens.append((match.start(), month))
            consumed.append(match.span())

    # 并列：6,7月 / 6、7、8月 / 6和7月（也覆盖单个 6月）
    for match in re.finditer(
        r"((?:[0-9]{1,2}|[一二三四五六七八九十]{1,3})(?:\s*[,、和及]\s*(?:[0-9]{1,2}|[一二三四五六七八九十]{1,3}))*)\s*月", text
    ):
        if any(begin <= match.start() < finish for begin, finish in consumed):
            continue
        months = [_month_number(part) for part in re.split(r"\s*[,、和及]\s*", match.group(1))]
        if not all(1 <= month <= 12 for month in months):
            continue
        # 6月30日 这类是日期，不是归属月
        if re.match(r"\s*[0-9]{1,2}\s*日", text[match.end():match.end() + 6]):
            continue
        for month in months:
            tokens.append((match.start(), month))
        consumed.append(match.span())

    if not tokens:
        return []

    # 有工资相关关键词时，只保留邻近关键词的月份，避免把其它月份误当工资月
    keyword_positions = [match.start() for keyword in SALARY_KEYWORDS for match in re.finditer(keyword, text)]
    if keyword_positions:
        nearby = [
            (pos, month) for pos, month in tokens
            if any(abs(kpos - pos) <= 12 for kpos in keyword_positions)
        ]
        if nearby:
            tokens = nearby

    periods = set()
    for pos, month in tokens:
        year = None
        for ypos, yval in years:
            if ypos < pos and pos - ypos <= 16:
                year = yval
        if year is None:
            year = paid.year if month <= paid.month else paid.year - 1
        periods.add(f"{year:04d}-{month:02d}")
    return sorted(periods)


def _outstanding(receivable: Receivable) -> int:
    return max(0, (receivable.amount_cents or 0) - (receivable.received_cents or 0))


def unpaid_payroll(db: Session, user_id: int, company_id: int) -> list[Receivable]:
    rows = (
        db.query(Receivable)
        .filter(
            Receivable.user_id == user_id,
            Receivable.company_id == company_id,
            Receivable.kind == "payroll",
            Receivable.deleted_at.is_(None),
            Receivable.status.in_(OPEN_STATUSES),
        )
        .order_by(Receivable.period.asc(), Receivable.id.asc())
        .all()
    )
    return [item for item in rows if _outstanding(item) > 0]


def reference_monthly_cents(db: Session, user_id: int, company_id: int) -> int:
    """参考月薪：该公司最近 6 笔工资应收的中位数。"""
    rows = (
        db.query(Receivable)
        .filter(
            Receivable.user_id == user_id,
            Receivable.company_id == company_id,
            Receivable.kind == "payroll",
            Receivable.deleted_at.is_(None),
            Receivable.amount_cents > 0,
        )
        .order_by(Receivable.period.desc(), Receivable.id.desc())
        .limit(6)
        .all()
    )
    values = [item.amount_cents for item in rows if item.amount_cents]
    return int(median(values)) if values else 0


def _month_label(period: str) -> str:
    return f"{int(period[5:7])}月" if len(period) == 7 and period[5:7].isdigit() else period


def _allocation(receivable: Receivable, cents: int) -> dict:
    return {
        "receivable_id": receivable.id,
        "period": receivable.period or "",
        "title": receivable.title or "",
        "amount_cents": cents,
        "amount": round(cents / 100, 2),
        "outstanding_cents": _outstanding(receivable),
    }


def _greedy(unpaid: list[Receivable], amount_cents: int) -> tuple[list[dict], int]:
    remaining = amount_cents
    allocations = []
    for receivable in unpaid:
        if remaining <= 0:
            break
        take = min(_outstanding(receivable), remaining)
        if take > 0:
            allocations.append(_allocation(receivable, take))
            remaining -= take
    return allocations, remaining


def propose_allocation(
    db: Session, user_id: int, company_id: int, amount_cents: int, paid_on: str = "", remark: str = ""
) -> dict:
    amount_cents = int(amount_cents or 0)
    result = {
        "months": [],
        "month_labels": [],
        "allocations": [],
        "remaining_cents": amount_cents,
        "remaining": round(amount_cents / 100, 2),
        "missing_periods": [],
        "confidence": 0.0,
        "source": "none",
        "reason": "",
    }
    if amount_cents <= 0 or not company_id:
        result["reason"] = "缺少到账金额或公司，无法推断"
        return result

    unpaid = unpaid_payroll(db, user_id, company_id)
    months = parse_month_mentions(remark, paid_on)

    if months:
        remaining = amount_cents
        allocations: list[dict] = []
        missing: list[str] = []
        for period in months:
            matched = [item for item in unpaid if (item.period or "") == period]
            if not matched:
                missing.append(period)
                continue
            for receivable in matched:
                if remaining <= 0:
                    break
                take = min(_outstanding(receivable), remaining)
                if take > 0:
                    allocations.append(_allocation(receivable, take))
                    remaining -= take
        result.update(
            months=months,
            month_labels=[_month_label(item) for item in months],
            allocations=allocations,
            remaining_cents=remaining,
            remaining=round(remaining / 100, 2),
            missing_periods=missing,
            source="remark",
        )
        parts = [f"备注写明 {'、'.join(_month_label(item) for item in months)}"]
        if missing:
            if allocations:
                parts.append(f"{'、'.join(_month_label(item) for item in missing)}没有对应的工资应收")
            else:
                parts[0] += "，但没有对应的工资应收"
        if remaining > 0:
            parts.append(f"还有 ¥{cents_to_text(remaining)} 没有可分到")
        if allocations and not missing and remaining == 0:
            result["confidence"] = 0.9
        elif allocations:
            result["confidence"] = 0.7
        result["reason"] = "；".join(parts)
        return result

    if not unpaid:
        result["reason"] = "该公司没有待结清的工资应收，无法推断归属月"
        return result

    # 金额正好等于最早 N 笔未结清工资合计
    running = 0
    exact_count = 0
    for index, receivable in enumerate(unpaid, start=1):
        running += _outstanding(receivable)
        if running == amount_cents:
            exact_count = index
            break
        if running > amount_cents:
            break
    if exact_count:
        allocations, remaining = _greedy(unpaid[:exact_count], amount_cents)
        periods = [item["period"] for item in allocations]
        result.update(
            months=periods,
            month_labels=[_month_label(item) for item in periods],
            allocations=allocations,
            remaining_cents=remaining,
            remaining=round(remaining / 100, 2),
            confidence=0.85,
            source="order",
            reason=f"备注没有月份；到账金额正好等于最早 {exact_count} 笔未结清工资合计",
        )
        return result

    # 金额约为 N 个月工资
    reference = reference_monthly_cents(db, user_id, company_id)
    if reference > 0:
        months_count = max(1, round(amount_cents / reference))
        if months_count <= len(unpaid):
            target_sum = sum(_outstanding(item) for item in unpaid[:months_count])
            tolerance = max(int(reference * 0.05), 100)
            if abs(target_sum - amount_cents) <= tolerance:
                allocations, remaining = _greedy(unpaid[:months_count], amount_cents)
                periods = [item["period"] for item in allocations]
                result.update(
                    months=periods,
                    month_labels=[_month_label(item) for item in periods],
                    allocations=allocations,
                    remaining_cents=remaining,
                    remaining=round(remaining / 100, 2),
                    confidence=0.8,
                    source="amount",
                    reason=(
                        f"备注没有月份；到账金额约为 {months_count} 个月工资"
                        f"（参考月薪 ¥{cents_to_text(reference)}），按最早欠发顺序核销"
                    ),
                )
                return result

    allocations, remaining = _greedy(unpaid, amount_cents)
    periods = [item["period"] for item in allocations]
    result.update(
        months=periods,
        month_labels=[_month_label(item) for item in periods],
        allocations=allocations,
        remaining_cents=remaining,
        remaining=round(remaining / 100, 2),
        confidence=0.5 if allocations and remaining == 0 else 0.3,
        source="order",
        reason="备注没有月份，按最早欠发顺序拆出候选，请核对",
    )
    if remaining > 0:
        result["reason"] += f"；还有 ¥{cents_to_text(remaining)} 没有可分到"
    return result
