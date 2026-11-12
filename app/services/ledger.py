"""应收状态机与批次调整：规则以后端校验为准。"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models import Batch, Receivable
from .money import normalize_period, period_from_date, today_str

COLLECTABLE_STATUSES = ("pending", "rejected")
LOCKED_STATUSES = ("submitted", "partial", "paid")
TERMINAL_STATUSES = ("abandoned",)
PAYMENT_STATUSES = ("partial", "paid")
LOCKED_FIELDS = ("amount", "title", "company_id", "category_id", "period")

LOCKED_MESSAGE = "已提交的应收已锁定金额、事项、公司、类别和归属月；如需修改，请先撤回提交"


def receivable_period(item: Receivable) -> str:
    return normalize_period(item.period) or period_from_date(item.occurred_on or "")


def has_payments(item: Receivable) -> bool:
    return (item.received_cents or 0) > 0 or item.status in PAYMENT_STATUSES


def is_locked(item: Receivable) -> bool:
    return item.status in LOCKED_STATUSES or item.status in TERMINAL_STATUSES


def collectable(item: Receivable) -> bool:
    return item.status in COLLECTABLE_STATUSES and item.deleted_at is None


def collectable_reason(item: Receivable) -> str:
    if item.status in COLLECTABLE_STATUSES:
        return ""
    labels = {
        "submitted": "已提交，不能再次归集",
        "partial": "已部分到账，不能归集",
        "paid": "已到账，不能归集",
        "abandoned": "已放弃，不能归集",
    }
    return labels.get(item.status, "当前状态不能归集")


def assert_unlocked_for_edit(item: Receivable, fields: list[str] | None = None) -> None:
    if item.status in TERMINAL_STATUSES:
        raise HTTPException(status_code=400, detail="已放弃的应收不能再修改")
    touching = set(fields or [])
    if item.status in LOCKED_STATUSES and touching.intersection(LOCKED_FIELDS):
        raise HTTPException(status_code=400, detail=LOCKED_MESSAGE)


def assert_can_delete(item: Receivable) -> None:
    if has_payments(item):
        raise HTTPException(status_code=400, detail="已有到账记录的应收不能删除，请先删除到账记录")
    if item.status in LOCKED_STATUSES:
        raise HTTPException(status_code=400, detail="已提交的应收不能删除，只能放弃并留下审计记录")
    if item.status == "abandoned":
        raise HTTPException(status_code=400, detail="已放弃的应收不能再删除")


def assert_status_change(item: Receivable, new_status: str, reason: str = "") -> None:
    if new_status == item.status:
        return
    if item.status in TERMINAL_STATUSES:
        raise HTTPException(status_code=400, detail="已放弃的应收不能再改状态")
    if has_payments(item):
        raise HTTPException(status_code=400, detail="已有到账记录的应收不能手工改状态")
    allowed = {
        "pending": {"rejected", "abandoned"},
        "rejected": {"pending", "abandoned"},
        "submitted": {"rejected", "abandoned"},
    }
    if item.status not in allowed or new_status not in allowed[item.status]:
        if new_status == "pending" and item.status == "submitted":
            raise HTTPException(status_code=400, detail="已提交的应收不能直接退回待提交，请通过批次「撤回提交」")
        if new_status == "submitted":
            raise HTTPException(status_code=400, detail="请通过提交批次将应收改为已提交")
        raise HTTPException(status_code=400, detail="不允许的状态变更")
    if new_status in ("abandoned", "rejected") and item.status == "submitted" and not (reason or "").strip():
        # 驳回和放弃建议填原因，但不强制驳回（公司口头通知也要能记下）；放弃必须填原因
        if new_status == "abandoned":
            raise HTTPException(status_code=400, detail="放弃已提交的应收必须填写原因")


def resolve_item_period(period: str | None, occurred_on: str | None, fallback: str = "") -> str:
    return normalize_period(period or "") or period_from_date(occurred_on or fallback or today_str())


def assert_same_period(receivables: list[Receivable], requested: str = "") -> str:
    periods = {receivable_period(item) for item in receivables}
    periods.discard("")
    if len(periods) > 1:
        raise HTTPException(status_code=400, detail="所选应收的归属月不一致，不能归入同一批次")
    auto = periods.pop() if periods else ""
    requested = normalize_period(requested)
    if requested and auto and requested != auto:
        raise HTTPException(status_code=400, detail="批次归属月与所选应收不一致")
    return requested or auto


def batch_has_payments(batch: Batch) -> bool:
    return any(
        (item.received_cents or 0) > 0
        for item in batch.receivables
        if item.deleted_at is None
    )


def assert_batch_adjustable(batch: Batch, action: str = "调整") -> None:
    if batch.status == "cancelled":
        raise HTTPException(status_code=400, detail="已取消的批次不能再" + action)
    if batch.status == "paid" or batch_has_payments(batch):
        raise HTTPException(status_code=400, detail="已到账或部分到账的批次不能再拆分或撤回，以免破坏已核销记录")


def reset_receivable_to_pending(item: Receivable, unbind: bool = False) -> None:
    if item.deleted_at is not None:
        return
    if (item.received_cents or 0) > 0:
        return
    item.status = "pending"
    item.submitted_on = ""
    item.expected_on = ""
    if unbind:
        item.batch_id = None
