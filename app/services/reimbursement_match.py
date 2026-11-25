"""到账附言拆单：把一笔到账按 AI 拆出的明细核销到已提交的报销应收。

只产出建议与校验结果，不写库。匹配口径：同公司 + 同归属月 + 费用项/类别，
目标必须是已提交且未结清（submitted / partial）的应收。工资不参与拆单。
"""

from sqlalchemy.orm import Session

from ..models import Category, Receivable
from .ledger import receivable_period, remaining_cents
from .money import yuan_to_cents
from .payroll_match import SALARY_KEYWORDS, normalize_remark

# 费用项关键词，只用于报销类应收；工资关键词由 payroll_match 负责
ITEM_KEYWORDS = {
    "差旅住宿": ("酒店", "住宿", "宾馆", "民宿", "差旅", "水单"),
    "城市交通": ("滴滴", "打车", "地铁", "公交", "出租", "网约车", "高铁", "火车", "铁路", "机票", "航空", "通行费", "停车", "加油", "汽油", "柴油"),
    "餐饮招待": ("餐饮", "餐费", "饭店", "餐厅", "外卖", "咖啡", "招待", "美团", "饿了么"),
    "办公采购": ("办公", "文具", "打印", "耗材", "电脑", "数码", "京东", "淘宝"),
    "通讯补贴": ("话费", "通讯", "流量", "宽带"),
}

TARGET_STATUSES = ("submitted", "partial")


def is_salary_remark(remark: str) -> bool:
    text = normalize_remark(remark)
    return any(keyword in text for keyword in SALARY_KEYWORDS)


def is_salary_inflow(extracted: dict) -> bool:
    """AI 判定：工资条类型或附言含工资语义才走工资核销，其余到账走附言拆单。"""
    doc_type = str((extracted or {}).get("doc_type") or "").strip().lower()
    if doc_type == "salary":
        return True
    return is_salary_remark(str((extracted or {}).get("remark") or ""))


def _categories(db: Session, user_id: int) -> list[Category]:
    return (
        db.query(Category)
        .filter(Category.user_id == user_id, Category.is_active == True)
        .order_by(Category.sort_order.asc(), Category.id.asc())
        .all()
    )


def _category_for_item(categories: list[Category], item: str) -> Category | None:
    text = (item or "").strip()
    if not text:
        return None
    for category in categories:
        if category.kind == "payroll":
            continue
        if text == category.name or category.name in text or text in category.name:
            return category
    for category in categories:
        if category.kind == "payroll":
            continue
        for keyword in ITEM_KEYWORDS.get(category.name, ()):
            if keyword in text:
                return category
    return None


def _item_specific_keywords(item: str) -> list[str]:
    """返回明细文字命中的具体费用关键词（如 通行费 / 汽油），用于区分同类别下的不同事项。"""
    text = (item or "").strip()
    hits = []
    for keywords in ITEM_KEYWORDS.values():
        for keyword in keywords:
            if keyword in text and keyword not in hits:
                hits.append(keyword)
    return hits


def _receivable_matches_entry(receivable: Receivable, entry: dict, categories: list[Category]) -> bool:
    period = str(entry.get("period") or "").strip()
    if period and receivable_period(receivable) != period:
        return False
    item = str(entry.get("item") or "").strip()
    if not item:
        return True
    title = (receivable.title or "").strip()
    # 最精确：明细原文出现在应收事项里
    if title and item in title:
        return True
    specific = _item_specific_keywords(item)
    # 明细含具体关键词（如通行费/汽油）：应收事项也须含其一，避免同类别下互串
    if specific:
        return bool(title and any(k in title for k in specific))
    # 无具体关键词：退到类别匹配
    category = _category_for_item(categories, item)
    return bool(category and receivable.category_id == category.id)


def _candidates(db: Session, user_id: int, company_id: int) -> list[Receivable]:
    return (
        db.query(Receivable)
        .filter(
            Receivable.user_id == user_id,
            Receivable.company_id == company_id,
            Receivable.deleted_at.is_(None),
            Receivable.status.in_(TARGET_STATUSES),
            Receivable.kind != "payroll",
        )
        .order_by(Receivable.id.asc())
        .all()
    )


def propose(db: Session, user_id: int, company_id: int, entries: list[dict]) -> dict:
    """按归属月 + 费用项/类别给出分摊建议；匹配不到或超待收记为 issues。"""
    result = {
        "allocations": [],
        "issues": [],
        "total_cents": 0,
        "entry_count": len(entries or []),
        "source": "entries",
    }
    if not company_id:
        result["issues"].append("请先为草稿选择公司")
        return result
    if not entries:
        result["issues"].append("附言里没有识别到可拆分的费用明细")
        return result

    categories = _categories(db, user_id)
    candidates = _candidates(db, user_id, company_id)
    used: dict[int, int] = {}
    for index, entry in enumerate(entries, start=1):
        label = str(entry.get("item") or "").strip() or f"第 {index} 条明细"
        period = str(entry.get("period") or "").strip()
        if not period:
            result["issues"].append(f"{label}：归属月无法识别，请人工填写后再核销")
        try:
            cents = yuan_to_cents(entry.get("amount"))
        except Exception:
            cents = 0
        if not cents or cents <= 0:
            result["issues"].append(f"{label}：金额无效")
            continue
        matched = [item for item in candidates if _receivable_matches_entry(item, entry, categories)]
        if not matched:
            result["issues"].append(f"{period or '未知月份'} {label}：没有对应的已提交应收")
            continue
        left = cents
        for receivable in matched:
            if left <= 0:
                break
            free = remaining_cents(receivable) - used.get(receivable.id, 0)
            if free <= 0:
                continue
            take = min(left, free)
            used[receivable.id] = used.get(receivable.id, 0) + take
            left -= take
            result["allocations"].append(
                {
                    "receivable_id": receivable.id,
                    "period": receivable_period(receivable),
                    "title": receivable.title or "",
                    "item": label,
                    "confidence": float(entry.get("confidence") or 0),
                    "amount_cents": take,
                    "amount": round(take / 100, 2),
                    "remaining_cents": free - take,
                }
            )
        if left > 0:
            result["issues"].append(
                f"{period or '未知月份'} {label}：金额超过对应应收待收余额 ¥{left / 100:.2f}"
            )
    result["total_cents"] = sum(item["amount_cents"] for item in result["allocations"])
    return result


def validate_apply(
    db: Session, user_id: int, company_id: int, entries: list[dict], allocations
) -> list[str]:
    """复核人工改过的分摊：状态、归属月/费用项、待收余额与重复项。"""
    issues: list[str] = []
    categories = _categories(db, user_id)
    candidates = {item.id: item for item in _candidates(db, user_id, company_id)}
    entries = entries or []
    seen: set[int] = set()
    for alloc in allocations or []:
        receivable_id = int(getattr(alloc, "receivable_id", 0) or 0)
        if receivable_id in seen:
            issues.append(f"应收 {receivable_id} 重复分摊")
            continue
        seen.add(receivable_id)
        receivable = candidates.get(receivable_id)
        if not receivable:
            issues.append(f"应收 {receivable_id} 不是该公司已提交且未结清的应收")
            continue
        try:
            cents = yuan_to_cents(getattr(alloc, "amount", 0))
        except Exception:
            cents = 0
        if cents <= 0:
            issues.append(f"「{receivable.title}」分摊金额无效")
            continue
        if cents > remaining_cents(receivable):
            issues.append(f"「{receivable.title}」分摊金额超过待收余额")
            continue
        if entries and not any(
            _receivable_matches_entry(receivable, entry, categories) for entry in entries
        ):
            issues.append(f"「{receivable.title}」的归属月或费用项与附言明细不一致")
    return issues
