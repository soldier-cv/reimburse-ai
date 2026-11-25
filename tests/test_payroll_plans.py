"""薪资计划：补齐缺失月份且不重复生成。"""

from app.models import Receivable
from app.services.money import current_month, months_between, today_str


def create_company(client, auth, name="发薪公司", payday=15):
    response = client.post("/api/companies", json={"name": name, "payday": payday}, headers=auth)
    assert response.status_code == 200, response.text
    return response.json()


def test_sync_generates_missing_months_once(client, auth, test_db):
    company = create_company(client, auth)
    created = client.post(
        "/api/payroll-plans",
        json={
            "company_id": company["id"],
            "amount": "15000.00",
            "payday": 15,
            "effective_from": "2026-09-01",
        },
        headers=auth,
    )
    assert created.status_code == 200, created.text
    plan = created.json()
    assert plan["amount_cents"] == 1500000
    assert plan["payday"] == 15

    first = client.post("/api/payroll-plans/sync", headers=auth)
    assert first.status_code == 200, first.text
    expected = months_between("2026-09", current_month())
    assert first.json()["created_count"] == len(expected)
    assert first.json()["created_periods"] == expected

    items = client.get("/api/receivables?kind=payroll&page_size=200", headers=auth).json()["items"]
    periods = sorted(item["period"] for item in items)
    assert periods == expected
    assert all(item["status"] == "submitted" for item in items)
    assert all(item["kind"] == "payroll" for item in items)
    assert all(item["amount_cents"] == 1500000 for item in items)

    second = client.post("/api/payroll-plans/sync", headers=auth)
    assert second.status_code == 200, second.text
    assert second.json()["created_count"] == 0
    again = client.get("/api/receivables?kind=payroll&page_size=200", headers=auth).json()
    assert again["total"] == len(expected)


def test_generated_salary_uses_next_month_payday(client, auth, test_db):
    """工资次月发放：归属月 2026-01、发薪日 15 → 发生日与预计到账日 2026-02-15。"""
    company = create_company(client, auth, name="次月发薪公司")
    plan = client.post(
        "/api/payroll-plans",
        json={"company_id": company["id"], "amount": "10000", "payday": 15, "effective_from": "2026-01-01", "effective_to": "2026-01-31"},
        headers=auth,
    )
    assert plan.status_code == 200, plan.text
    assert client.post("/api/payroll-plans/sync", headers=auth).status_code == 200

    item = client.get("/api/receivables?kind=payroll", headers=auth).json()["items"][0]
    assert item["period"] == "2026-01"
    assert item["occurred_on"] == "2026-02-15"
    assert item["expected_on"] == "2026-02-15"


def test_next_month_payday_crosses_year_and_month_end():
    from app.services.money import next_month_payday

    assert next_month_payday("2026-12", 15) == "2027-01-15"
    assert next_month_payday("2026-01", 31) == "2026-02-28"


def test_amount_change_does_not_regenerate_existing_month(client, auth, test_db):
    """改了税前月薪再补齐，不会为同一个月再生成一笔。"""
    company = create_company(client, auth, name="改薪公司")
    created = client.post(
        "/api/payroll-plans",
        json={
            "company_id": company["id"],
            "amount": "10000.00",
            "payday": 15,
            "effective_from": "2026-10-01",
        },
        headers=auth,
    )
    assert created.status_code == 200, created.text
    plan_id = created.json()["id"]

    first = client.post("/api/payroll-plans/sync", headers=auth)
    assert first.status_code == 200, first.text
    expected = len(first.json()["created_periods"])
    assert expected > 0

    updated = client.put(
        f"/api/payroll-plans/{plan_id}",
        json={"amount": "20000.00"},
        headers=auth,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["amount_cents"] == 2000000

    second = client.post("/api/payroll-plans/sync", headers=auth)
    assert second.status_code == 200, second.text
    assert second.json()["created_count"] == 0

    items = client.get("/api/receivables?kind=payroll&page_size=200", headers=auth).json()
    assert items["total"] == expected
    assert all(item["amount_cents"] == 1000000 for item in items["items"])


def test_amount_change_can_update_unpaid_generated_receivables(client, auth, test_db):
    """勾选同步后，只改本计划生成、已提交但未发薪且未归集的工资应收。"""
    company = create_company(client, auth, name="同步改薪公司")
    created = client.post(
        "/api/payroll-plans",
        json={
            "company_id": company["id"],
            "amount": "10000.00",
            "payday": 15,
            "effective_from": "2026-10-01",
        },
        headers=auth,
    )
    assert created.status_code == 200, created.text
    plan_id = created.json()["id"]

    synced = client.post("/api/payroll-plans/sync", headers=auth)
    assert synced.status_code == 200, synced.text
    items = client.get("/api/receivables?kind=payroll&page_size=200", headers=auth).json()["items"]
    assert len(items) >= 2
    assert all(item["status"] == "submitted" for item in items)
    paid, unpaid = sorted(items, key=lambda item: item["period"])[:2]

    payment = client.post(
        "/api/payments",
        json={
            "company_id": company["id"],
            "amount": 10000.0,
            "paid_on": "2026-11-05",
            "allocations": [{"receivable_id": paid["id"], "amount": 10000.0}],
        },
        headers=auth,
    )
    assert payment.status_code == 200, payment.text

    updated = client.put(
        f"/api/payroll-plans/{plan_id}",
        json={"amount": "20000.00", "apply_amount_to_pending": True},
        headers=auth,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["updated_receivables"] == 1

    paid_after = client.get(f"/api/receivables/{paid['id']}", headers=auth).json()
    unpaid_after = client.get(f"/api/receivables/{unpaid['id']}", headers=auth).json()
    assert paid_after["status"] == "paid"
    assert paid_after["amount_cents"] == 1000000
    assert unpaid_after["status"] == "submitted"
    assert unpaid_after["amount_cents"] == 2000000

    synced_again = client.post("/api/payroll-plans/sync", headers=auth)
    assert synced_again.status_code == 200, synced_again.text
    assert synced_again.json()["created_count"] == 0


def test_stop_date_and_raise_via_new_plan(client, auth):
    company = create_company(client, auth, name="涨薪公司")
    old = client.post(
        "/api/payroll-plans",
        json={
            "company_id": company["id"],
            "amount": "10000",
            "payday": 10,
            "effective_from": "2026-01-01",
            "effective_to": "2026-06-30",
        },
        headers=auth,
    )
    assert old.status_code == 200, old.text
    new = client.post(
        "/api/payroll-plans",
        json={
            "company_id": company["id"],
            "amount": "12000",
            "payday": 10,
            "effective_from": "2026-07-01",
        },
        headers=auth,
    )
    assert new.status_code == 200, new.text
    # 重叠会被拒绝
    overlap = client.post(
        "/api/payroll-plans",
        json={
            "company_id": company["id"],
            "amount": "13000",
            "payday": 10,
            "effective_from": "2026-06-01",
        },
        headers=auth,
    )
    assert overlap.status_code == 400

    synced = client.post("/api/payroll-plans/sync", headers=auth)
    assert synced.status_code == 200, synced.text
    items = client.get("/api/receivables?kind=payroll&page_size=200", headers=auth).json()["items"]
    by_period = {item["period"]: item for item in items}
    assert by_period["2026-06"]["amount_cents"] == 1000000
    assert by_period["2026-07"]["amount_cents"] == 1200000
    assert "2026-05" in by_period
    # 不应生成停发月份之后、新计划生效之前的空洞（6 月结束后 7 月接上）


def test_stopped_plan_does_not_generate_future_months(client, auth):
    company = create_company(client, auth, name="离职公司")
    # 已结束的计划只生成到停发月
    created = client.post(
        "/api/payroll-plans",
        json={
            "company_id": company["id"],
            "amount": "8000",
            "payday": 5,
            "effective_from": "2026-08-01",
            "effective_to": "2026-09-15",
        },
        headers=auth,
    )
    assert created.status_code == 200, created.text
    synced = client.post("/api/payroll-plans/sync", headers=auth)
    assert synced.status_code == 200, synced.text
    items = client.get("/api/receivables?kind=payroll", headers=auth).json()["items"]
    periods = {item["period"] for item in items}
    assert periods == {"2026-08", "2026-09"}


def test_manual_payroll_blocks_auto_generation_for_same_period(client, auth):
    company = create_company(client, auth, name="已有工资")
    categories = client.get("/api/categories", headers=auth).json()
    payroll_cat = next(item for item in categories if item["name"] == "工资")
    manual = client.post(
        "/api/receivables",
        json={
            "company_id": company["id"],
            "category_id": payroll_cat["id"],
            "title": "2026-10 工资",
            "amount": "9000",
            "occurred_on": "2026-10-15",
            "period": "2026-10",
        },
        headers=auth,
    )
    assert manual.status_code == 200
    client.post(
        "/api/payroll-plans",
        json={
            "company_id": company["id"],
            "amount": "9000",
            "payday": 15,
            "effective_from": "2026-10-01",
            "effective_to": "2026-10-31",
        },
        headers=auth,
    )
    synced = client.post("/api/payroll-plans/sync", headers=auth)
    assert synced.json()["created_count"] == 0
    listing = client.get("/api/receivables?kind=payroll", headers=auth).json()
    assert listing["total"] == 1
    assert listing["items"][0]["id"] == manual.json()["id"]
