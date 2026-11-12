"""薪资计划：按月补齐待提交工资应收，避免同一公司同一归属月重复生成。"""

from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models import Category, PayrollPlan, Receivable
from .money import (
    cents_to_text,
    cents_to_yuan,
    current_month,
    month_payday,
    months_between,
    normalize_date,
    normalize_period,
    parse_date,
    period_from_date,
    yuan_to_cents,
)


def _payroll_category(db: Session, user_id: int, category_id: int | None) -> Category | None:
    if category_id:
        category = (
            db.query(Category)
            .filter(Category.id == category_id, Category.user_id == user_id)
            .first()
        )
        if not category:
            raise HTTPException(status_code=404, detail="类别不存在")
        return category
    return (
        db.query(Category)
        .filter(Category.user_id == user_id, Category.kind == "payroll", Category.is_active == True, Category.name == "工资")
        .first()
    ) or (
        db.query(Category)
        .filter(Category.user_id == user_id, Category.kind == "payroll", Category.is_active == True)
        .order_by(Category.sort_order.asc(), Category.id.asc())
        .first()
    )


def serialize_plan(plan: PayrollPlan) -> dict:
    return {
        "id": plan.id,
        "company_id": plan.company_id,
        "company_name": plan.company.name if plan.company else "",
        "company_color": plan.company.color if plan.company else "#2563eb",
        "category_id": plan.category_id,
        "category_name": plan.category.name if plan.category else "",
        "amount": cents_to_yuan(plan.amount_cents),
        "amount_cents": plan.amount_cents or 0,
        "payday": plan.payday or 15,
        "effective_from": plan.effective_from or "",
        "effective_to": plan.effective_to or "",
        "note": plan.note or "",
        "is_active": bool(plan.is_active),
        "created_at": plan.created_at.strftime("%Y-%m-%d %H:%M:%S") if plan.created_at else "",
    }


def _period_overlap(a_from: str, a_to: str, b_from: str, b_to: str) -> bool:
    start_a = period_from_date(a_from) if a_from else ""
    start_b = period_from_date(b_from) if b_from else ""
    end_a = period_from_date(a_to) if a_to else "9999-12"
    end_b = period_from_date(b_to) if b_to else "9999-12"
    if not start_a or not start_b:
        return False
    return start_a <= end_b and start_b <= end_a


def assert_no_overlap(db: Session, user_id: int, company_id: int, effective_from: str, effective_to: str, exclude_id: int | None = None) -> None:
    query = db.query(PayrollPlan).filter(
        PayrollPlan.user_id == user_id,
        PayrollPlan.company_id == company_id,
        PayrollPlan.is_active == True,
    )
    if exclude_id:
        query = query.filter(PayrollPlan.id != exclude_id)
    for other in query.all():
        if _period_overlap(effective_from, effective_to, other.effective_from, other.effective_to):
            raise HTTPException(
                status_code=400,
                detail=f"与已有薪资计划（{other.effective_from} 起）的有效区间重叠，请先结束旧计划或调整日期",
            )


def plan_months(plan: PayrollPlan, until: str | None = None) -> list[str]:
    """该计划应生成的归属月（不超过截止月，默认本月）。"""
    if not plan.is_active:
        return []
    start = normalize_period(plan.effective_from) or period_from_date(plan.effective_from)
    if not start:
        return []
    end_limit = normalize_period(until) or current_month()
    if plan.effective_to:
        end = min(period_from_date(plan.effective_to), end_limit)
    else:
        end = end_limit
    return months_between(start, end)


def existing_payroll_periods(db: Session, user_id: int, company_id: int) -> set[str]:
    rows = (
        db.query(Receivable.period)
        .filter(
            Receivable.user_id == user_id,
            Receivable.company_id == company_id,
            Receivable.kind == "payroll",
            Receivable.deleted_at.is_(None),
            Receivable.period != "",
        )
        .all()
    )
    return {row[0] for row in rows if row[0]}


def missing_months_for_plan(db: Session, plan: PayrollPlan, until: str | None = None) -> list[str]:
    existing = existing_payroll_periods(db, plan.user_id, plan.company_id)
    return [month for month in plan_months(plan, until) if month not in existing]


def generate_receivable(db: Session, plan: PayrollPlan, period: str) -> Receivable:
    category = plan.category or _payroll_category(db, plan.user_id, plan.category_id)
    occurred_on = month_payday(period, plan.payday or 15)
    title = f"{period} 工资"
    if plan.company:
        title = f"{plan.company.short_name or plan.company.name} {period} 工资"
    receivable = Receivable(
        user_id=plan.user_id,
        company_id=plan.company_id,
        category_id=category.id if category else None,
        kind="payroll",
        title=title[:150],
        period=period,
        occurred_on=occurred_on,
        amount_cents=plan.amount_cents,
        status="pending",
        expected_on=occurred_on,
        source="payroll_plan",
        note=(plan.note or "由薪资计划自动生成")[:300] if not (plan.note or "").strip() else (plan.note or "")[:300],
    )
    if not receivable.note:
        receivable.note = "由薪资计划自动生成"
    db.add(receivable)
    return receivable


def sync_user_plans(db: Session, user_id: int, until: str | None = None) -> dict:
    """补齐该用户所有有效计划的缺失月份，返回创建结果。"""
    plans = (
        db.query(PayrollPlan)
        .filter(PayrollPlan.user_id == user_id, PayrollPlan.is_active == True)
        .order_by(PayrollPlan.id.asc())
        .all()
    )
    created: list[Receivable] = []
    skipped = 0
    for plan in plans:
        missing = missing_months_for_plan(db, plan, until)
        skipped += max(0, len(plan_months(plan, until)) - len(missing))
        for period in missing:
            # 生成过程中同一请求内也要避免重复（例如两份计划理论上不应重叠）
            existing = existing_payroll_periods(db, plan.user_id, plan.company_id)
            if period in existing:
                skipped += 1
                continue
            created.append(generate_receivable(db, plan, period))
            db.flush()
    if created:
        db.commit()
        for item in created:
            db.refresh(item)
    return {
        "created_count": len(created),
        "skipped_count": skipped,
        "created_ids": [item.id for item in created],
        "created_periods": [item.period for item in created],
        "message": (
            f"已补齐 {len(created)} 笔工资应收" if created else "没有需要补齐的月份"
        ),
    }


def missing_summary(db: Session, user_id: int) -> dict:
    """尚未生成的计划月份汇总，用于看板待办。"""
    plans = (
        db.query(PayrollPlan)
        .filter(PayrollPlan.user_id == user_id, PayrollPlan.is_active == True)
        .all()
    )
    missing = []
    for plan in plans:
        for period in missing_months_for_plan(db, plan):
            missing.append(
                {
                    "plan_id": plan.id,
                    "company_id": plan.company_id,
                    "company_name": plan.company.name if plan.company else "",
                    "period": period,
                    "amount_cents": plan.amount_cents or 0,
                    "amount": cents_to_yuan(plan.amount_cents or 0),
                }
            )
    return {
        "count": len(missing),
        "amount_cents": sum(item["amount_cents"] for item in missing),
        "amount": cents_to_yuan(sum(item["amount_cents"] for item in missing)),
        "items": missing,
    }


def parse_plan_dates(effective_from: str, effective_to: str | None) -> tuple[str, str]:
    start = parse_date(effective_from)
    if not start:
        raise HTTPException(status_code=400, detail="请填写有效的生效日期")
    if not (effective_to or "").strip():
        return start.isoformat(), ""
    end = parse_date(effective_to)
    if not end:
        raise HTTPException(status_code=400, detail="停发日期格式不正确")
    if end < start:
        raise HTTPException(status_code=400, detail="停发日期不能早于生效日期")
    return start.isoformat(), end.isoformat()


def parse_salary(amount) -> int:
    cents = yuan_to_cents(amount)
    if cents <= 0:
        raise HTTPException(status_code=400, detail="月薪必须大于 0")
    return cents


def clamp_payday(value) -> int:
    try:
        payday = int(value or 15)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="发薪日必须是 1 到 31 的整数") from exc
    if payday < 1 or payday > 31:
        raise HTTPException(status_code=400, detail="发薪日必须是 1 到 31 的整数")
    return payday
