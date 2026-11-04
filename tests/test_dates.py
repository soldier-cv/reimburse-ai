from app.main import _next_pay_date


class CompanyStub:
    payday = 31
    payment_days = 30


def test_next_pay_date_handles_month_end():
    assert _next_pay_date(CompanyStub(), "2026-02-10") == "2026-02-28"
    assert _next_pay_date(CompanyStub(), "2026-04-10") == "2026-04-30"
    assert _next_pay_date(CompanyStub(), "2026-02-28") == "2026-02-28"
