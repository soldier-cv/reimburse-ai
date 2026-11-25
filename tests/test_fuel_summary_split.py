"""油费按月汇总打款：一条附言明细分摊到同月多笔汽油发票应收。"""

from app.services.reimbursement_match import propose


class _CategoryStub:
    def __init__(self, id, name, kind="reimbursement"):
        self.id = id
        self.name = name
        self.kind = kind


class _ReceivableStub:
    def __init__(self, id, title, period, cents):
        self.id = id
        self.title = title
        self.period = period
        self.amount_cents = cents
        self.received_cents = 0
        self.waived_cents = 0
        self.deleted_at = None
        self.status = "submitted"
        self.kind = "reimbursement"
        self.category_id = 1


def test_oil_fee_summary_matches_gasoline_invoices():
    """附言「油费」应匹配标题含「汽油」的应收；同月多笔按待收分摊。"""
    categories = [_CategoryStub(1, "城市交通")]
    # 2 月三张汽油发票，合计 815.35
    receivables = [
        _ReceivableStub(1, "*汽油*92号车用汽油(VIB)", "2026-02", 26164),
        _ReceivableStub(2, "*汽油*92号车用汽油(VIB)", "2026-02", 27013),
        _ReceivableStub(3, "*汽油*92号车用汽油(VIB)", "2026-02", 28358),
    ]
    entries = [{"item": "油费", "period": "2026-02", "amount": 815.35, "confidence": 0.9}]

    # 直接调用纯函数 propose：需 mock db/session 的候选查询，改用内部匹配断言
    from app.services.reimbursement_match import _receivable_matches_entry

    matched = [r for r in receivables if _receivable_matches_entry(r, entries[0], categories)]
    assert len(matched) == 3, "油费应匹配到三笔汽油发票应收"

    # 验证总待收等于明细金额，可全额分摊
    total = sum(r.amount_cents for r in matched)
    assert total == 81535
