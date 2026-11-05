import pytest


def create_company(client, auth, name="部分到账公司"):
    response = client.post("/api/companies", json={"name": name, "payday": 15}, headers=auth)
    assert response.status_code == 200, response.text
    return response.json()


def create_receivable(client, auth, company_id, title, amount):
    response = client.post(
        "/api/receivables",
        json={"company_id": company_id, "title": title, "amount": amount, "occurred_on": "2026-11-03"},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_partial_payment_and_second_payment(client, auth):
    company = create_company(client, auth)
    item = create_receivable(client, auth, company["id"], "分期报销", 1000.0)

    first = client.post(
        "/api/payments",
        json={"company_id": company["id"], "amount": 500.0, "paid_on": "2026-11-10", "receivable_ids": [item["id"]]},
        headers=auth,
    )
    assert first.status_code == 200, first.text

    detail = client.get(f"/api/receivables/{item['id']}", headers=auth).json()
    assert detail["status"] == "partial"
    assert detail["received_cents"] == 50000
    assert detail["remaining_cents"] == 50000
    assert len(detail["payments"]) == 1
    assert detail["payments"][0]["amount"] == 500.0

    dashboard = client.get("/api/dashboard", headers=auth).json()
    assert dashboard["kpis"]["outstanding_cents"] == 50000
    assert dashboard["kpis"]["partial_count"] == 1

    second = client.post(
        "/api/payments",
        json={"company_id": company["id"], "amount": 500.0, "paid_on": "2026-11-20", "receivable_ids": [item["id"]]},
        headers=auth,
    )
    assert second.status_code == 200, second.text

    detail = client.get(f"/api/receivables/{item['id']}", headers=auth).json()
    assert detail["status"] == "paid"
    assert detail["received_cents"] == 100000
    assert detail["remaining_cents"] == 0
    assert len(detail["payments"]) == 2

    dashboard = client.get("/api/dashboard", headers=auth).json()
    assert dashboard["kpis"]["outstanding_cents"] == 0
    assert dashboard["kpis"]["paid_this_month"] == 1000.0


def test_delete_payment_returns_to_partial(client, auth):
    company = create_company(client, auth, "回滚公司")
    item = create_receivable(client, auth, company["id"], "分期工资", 1000.0)

    for amount in (500.0, 500.0):
        resp = client.post(
            "/api/payments",
            json={"company_id": company["id"], "amount": amount, "paid_on": "2026-11-10", "receivable_ids": [item["id"]]},
            headers=auth,
        )
        assert resp.status_code == 200, resp.text

    payments = client.get("/api/payments", headers=auth).json()
    assert len(payments) == 2
    deleted = client.delete(f"/api/payments/{payments[0]['id']}", headers=auth)
    assert deleted.status_code == 200, deleted.text

    detail = client.get(f"/api/receivables/{item['id']}", headers=auth).json()
    assert detail["status"] == "partial"
    assert detail["received_cents"] == 50000
    assert detail["remaining_cents"] == 50000
    assert len(detail["payments"]) == 1


def test_one_payment_covers_multiple_receivables_with_partial(client, auth):
    company = create_company(client, auth, "多笔公司")
    first = create_receivable(client, auth, company["id"], "打车", 100.0)
    second = create_receivable(client, auth, company["id"], "住宿", 200.0)

    resp = client.post(
        "/api/payments",
        json={"company_id": company["id"], "amount": 250.0, "paid_on": "2026-11-10", "receivable_ids": [first["id"], second["id"]]},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text

    first_detail = client.get(f"/api/receivables/{first['id']}", headers=auth).json()
    second_detail = client.get(f"/api/receivables/{second['id']}", headers=auth).json()
    assert first_detail["status"] == "paid"
    assert first_detail["received_cents"] == 10000
    assert second_detail["status"] == "partial"
    assert second_detail["received_cents"] == 15000
    assert second_detail["remaining_cents"] == 5000

    payment = client.get(f"/api/payments/{resp.json()['payment_id']}", headers=auth).json()
    assert len(payment["items"]) == 2


def test_explicit_allocation_over_outstanding_rejected(client, auth):
    company = create_company(client, auth, "校验公司")
    item = create_receivable(client, auth, company["id"], "校验应收", 1000.0)

    resp = client.post(
        "/api/payments",
        json={
            "company_id": company["id"],
            "amount": 1200.0,
            "paid_on": "2026-11-10",
            "allocations": [{"receivable_id": item["id"], "amount": 1200.0}],
        },
        headers=auth,
    )
    assert resp.status_code == 400, resp.text
    assert "超过待收余额" in resp.json()["detail"]["message"]
