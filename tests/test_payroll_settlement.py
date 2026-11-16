"""工资按实发结清：税前挂账、实发入账、差额记代扣。"""

from app.models import Receivable, User
from app.services.payroll_match import propose_allocation


def _company(client, auth, name="实发工资公司"):
    response = client.post("/api/companies", json={"name": name, "payday": 15}, headers=auth)
    assert response.status_code == 200, response.text
    return response.json()


def _payroll(db, company_id, period, amount_cents, status="pending"):
    user = db.query(User).first()
    item = Receivable(
        user_id=user.id,
        company_id=company_id,
        kind="payroll",
        title=f"{period} 工资",
        period=period,
        occurred_on=f"{period}-15",
        amount_cents=amount_cents,
        status=status,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _cash(client, auth, company_id, title, amount, period="2026-11"):
    response = client.post(
        "/api/receivables",
        json={
            "company_id": company_id,
            "title": title,
            "amount": amount,
            "occurred_on": f"{period}-03",
            "period": period,
        },
        headers=auth,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_salary_settles_by_net_and_records_deduction(client, auth, test_db):
    company = _company(client, auth)
    salary = _payroll(test_db, company["id"], "2026-10", 2000000, status="submitted")

    response = client.post(
        "/api/payments",
        json={
            "company_id": company["id"],
            "amount": 15000.0,
            "paid_on": "2026-11-10",
            "allocations": [{"receivable_id": salary.id, "amount": 15000.0, "waive": True}],
        },
        headers=auth,
    )
    assert response.status_code == 200, response.text

    detail = client.get(f"/api/receivables/{salary.id}", headers=auth).json()
    assert detail["status"] == "paid"
    assert detail["amount_cents"] == 2000000
    assert detail["received_cents"] == 1500000
    assert detail["waived_cents"] == 500000
    assert detail["remaining_cents"] == 0

    dashboard = client.get("/api/dashboard", headers=auth).json()
    assert dashboard["kpis"]["outstanding_cents"] == 0
    assert dashboard["todos"]["payroll_outstanding_cents"] == 0


def test_salary_without_waive_stays_partial(client, auth, test_db):
    company = _company(client, auth)
    salary = _payroll(test_db, company["id"], "2026-10", 2000000, status="submitted")

    response = client.post(
        "/api/payments",
        json={
            "company_id": company["id"],
            "amount": 15000.0,
            "paid_on": "2026-11-10",
            "allocations": [{"receivable_id": salary.id, "amount": 15000.0}],
        },
        headers=auth,
    )
    assert response.status_code == 200, response.text

    detail = client.get(f"/api/receivables/{salary.id}", headers=auth).json()
    assert detail["status"] == "partial"
    assert detail["waived_cents"] == 0
    assert detail["remaining_cents"] == 500000


def test_delete_payment_reverts_deduction(client, auth, test_db):
    company = _company(client, auth)
    salary = _payroll(test_db, company["id"], "2026-10", 2000000, status="submitted")
    created = client.post(
        "/api/payments",
        json={
            "company_id": company["id"],
            "amount": 15000.0,
            "paid_on": "2026-11-10",
            "allocations": [{"receivable_id": salary.id, "amount": 15000.0, "waive": True}],
        },
        headers=auth,
    )
    assert created.status_code == 200, created.text
    payment_id = created.json()["payment_id"]

    deleted = client.delete(f"/api/payments/{payment_id}", headers=auth)
    assert deleted.status_code == 200, deleted.text

    detail = client.get(f"/api/receivables/{salary.id}", headers=auth).json()
    assert detail["status"] == "pending"
    assert detail["received_cents"] == 0
    assert detail["waived_cents"] == 0
    assert detail["remaining_cents"] == 2000000


def test_batch_payment_skips_payroll(client, auth, test_db):
    company = _company(client, auth)
    salary = _payroll(test_db, company["id"], "2026-11", 2000000)
    cash = _cash(client, auth, company["id"], "打车费", 500.0)

    created = client.post(
        "/api/batches",
        json={
            "company_id": company["id"],
            "period": "2026-11",
            "title": "11 月工资与报销",
            "receivable_ids": [salary.id, cash["id"]],
            "submit": True,
            "submitted_on": "2026-11-05",
        },
        headers=auth,
    )
    assert created.status_code == 200, created.text
    batch_id = created.json()["batch_id"]

    paid = client.post(f"/api/batches/{batch_id}/payment", json={"note": "批次到账"}, headers=auth)
    assert paid.status_code == 200, paid.text
    body = paid.json()
    assert len(body["skipped_payroll"]) == 1
    assert body["skipped_payroll"][0]["id"] == salary.id

    salary_detail = client.get(f"/api/receivables/{salary.id}", headers=auth).json()
    assert salary_detail["status"] == "submitted"
    assert salary_detail["received_cents"] == 0
    assert salary_detail["remaining_cents"] == 2000000

    cash_detail = client.get(f"/api/receivables/{cash['id']}", headers=auth).json()
    assert cash_detail["status"] == "paid"
    assert cash_detail["received_cents"] == 50000

    # 报销结清、工资未结清，批次仍算已提交
    batch = client.get(f"/api/batches/{batch_id}", headers=auth).json()
    assert batch["status"] == "submitted"


def test_batch_payment_refuses_payroll_only_batch(client, auth, test_db):
    company = _company(client, auth)
    salary = _payroll(test_db, company["id"], "2026-11", 2000000)
    created = client.post(
        "/api/batches",
        json={
            "company_id": company["id"],
            "period": "2026-11",
            "title": "只有工资",
            "receivable_ids": [salary.id],
            "submit": True,
            "submitted_on": "2026-11-05",
        },
        headers=auth,
    )
    assert created.status_code == 200, created.text
    batch_id = created.json()["batch_id"]

    paid = client.post(f"/api/batches/{batch_id}/payment", json={}, headers=auth)
    assert paid.status_code == 400
    assert "登记实发" in paid.json()["detail"]


def test_payroll_match_settles_with_net_amount(test_db, client, auth):
    company = _company(client, auth)
    _payroll(test_db, company["id"], "2026-09", 2000000)
    _payroll(test_db, company["id"], "2026-10", 2000000)

    proposal = propose_allocation(
        test_db,
        test_db.query(User).first().id,
        company["id"],
        1500000,
        "2026-11-10",
        "10月工资",
    )
    assert proposal["remaining_cents"] == 0
    assert len(proposal["allocations"]) == 1
    allocation = proposal["allocations"][0]
    assert allocation["period"] == "2026-10"
    assert allocation["amount_cents"] == 1500000
    assert allocation["waived_cents"] == 500000
    assert allocation["waive"] is True
