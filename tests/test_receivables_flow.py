import io
import json
import zipfile

from PIL import Image


def make_png(color=(240, 240, 240)) -> bytes:
    image = Image.new("RGB", (640, 900), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def create_company(client, auth, name="测试公司", payday=15):
    response = client.post(
        "/api/companies",
        json={
            "name": name,
            "short_name": name[:4],
            "invoice_title": name,
            "tax_id": "91320000TEST",
            "payday": payday,
            "payment_days": 30,
        },
        headers=auth,
    )
    assert response.status_code == 200, response.text
    return response.json()


def create_receivable(client, auth, company_id, title="打车费", amount=128.0, occurred_on="2026-11-03"):
    response = client.post(
        "/api/receivables",
        json={
            "company_id": company_id,
            "title": title,
            "amount": amount,
            "occurred_on": occurred_on,
        },
        headers=auth,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_full_receivable_batch_payment_export_flow(client, auth):
    company = create_company(client, auth)
    first = create_receivable(client, auth, company["id"], "打车费", 128.0)
    second = create_receivable(client, auth, company["id"], "酒店住宿", 480.0)

    assert first["amount_cents"] == 12800
    assert first["status"] == "pending"

    listing = client.get("/api/receivables", headers=auth)
    assert listing.status_code == 200
    assert listing.json()["total"] == 2
    assert listing.json()["total_amount"] == 608.0

    batch_response = client.post(
        "/api/batches",
        json={
            "company_id": company["id"],
            "period": "2026-11",
            "title": "11 月报销",
            "receivable_ids": [first["id"], second["id"]],
        },
        headers=auth,
    )
    assert batch_response.status_code == 200, batch_response.text
    batch_id = batch_response.json()["batch_id"]

    submit_response = client.post(
        f"/api/batches/{batch_id}/submit",
        json={"submitted_on": "2026-11-05"},
        headers=auth,
    )
    assert submit_response.status_code == 200, submit_response.text
    # 每月 15 日打款，11-05 提交后预计 11-15 到账
    assert submit_response.json()["expected_on"] == "2026-11-15"

    detail = client.get(f"/api/batches/{batch_id}", headers=auth).json()
    assert detail["amount"] == 608.0
    assert all(item["status"] == "submitted" for item in detail["items"])

    # 金额不一致必须拒绝
    mismatch = client.post(
        "/api/payments",
        json={
            "company_id": company["id"],
            "amount": 600.0,
            "paid_on": "2026-11-15",
            "receivable_ids": [first["id"], second["id"]],
        },
        headers=auth,
    )
    assert mismatch.status_code == 400, mismatch.text

    payment = client.post(
        f"/api/batches/{batch_id}/payment",
        json={"paid_on": "2026-11-15", "note": "一批到账"},
        headers=auth,
    )
    assert payment.status_code == 200, payment.text

    paid_detail = client.get(f"/api/batches/{batch_id}", headers=auth).json()
    assert paid_detail["status"] == "paid"
    assert all(item["status"] == "paid" for item in paid_detail["items"])

    dashboard = client.get("/api/dashboard", headers=auth).json()
    assert dashboard["kpis"]["outstanding_cents"] == 0
    assert dashboard["kpis"]["paid_this_month"] == 608.0
    assert dashboard["kpis"]["avg_days"] == 10.0

    excel = client.get("/api/export/excel", headers=auth)
    assert excel.status_code == 200
    assert "spreadsheetml" in excel.headers["content-type"]
    assert len(excel.content) > 1000

    pdf = client.get(f"/api/export/pdf?ids={first['id']},{second['id']}", headers=auth)
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF")


def test_capture_confirm_and_duplicate_guard(client, auth):
    company = create_company(client, auth, name="截图公司")
    png = make_png()

    upload = client.post(
        "/api/capture",
        files=[("files", ("wechat_shot.png", png, "image/png"))],
        data={"company_id": str(company["id"])},
        headers=auth,
    )
    assert upload.status_code == 200, upload.text
    draft = upload.json()["drafts"][0]
    assert draft["status"] == "ready"
    assert draft["amount_cents"] is None

    patch = client.patch(
        f"/api/capture/drafts/{draft['id']}",
        json={"amount": "128.00", "occurred_on": "2026-11-03", "title": "微信打车"},
        headers=auth,
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["amount_cents"] == 12800

    confirm = client.post(
        "/api/capture/confirm",
        json={"draft_ids": [draft["id"]], "company_id": company["id"]},
        headers=auth,
    )
    assert confirm.status_code == 200, confirm.text
    receivable_id = confirm.json()["created"][0]["id"]

    detail = client.get(f"/api/receivables/{receivable_id}", headers=auth).json()
    assert detail["amount_cents"] == 12800
    assert detail["attachment_count"] == 1
    assert detail["attachments"][0]["filename"] == "wechat_shot.png"

    attachment_id = detail["attachments"][0]["id"]
    assert client.get(f"/api/attachments/{attachment_id}/file", headers=auth).status_code == 200
    assert client.get(f"/api/attachments/{attachment_id}/thumb", headers=auth).status_code == 200
    sign = client.get(f"/api/files/sign?kind=attachment&obj_id={attachment_id}", headers=auth)
    assert sign.status_code == 200, sign.text
    assert client.get(sign.json()["url"]).status_code == 200

    # 同一张图再次上传并入账，应触发重复保护
    upload_again = client.post(
        "/api/capture",
        files=[("files", ("wechat_shot.png", png, "image/png"))],
        data={"company_id": str(company["id"])},
        headers=auth,
    )
    draft_again = upload_again.json()["drafts"][0]
    client.patch(
        f"/api/capture/drafts/{draft_again['id']}",
        json={"amount": "128.00", "occurred_on": "2026-11-03"},
        headers=auth,
    )
    duplicate = client.post(
        "/api/capture/confirm",
        json={"draft_ids": [draft_again["id"]], "company_id": company["id"]},
        headers=auth,
    )
    assert duplicate.status_code == 409, duplicate.text

    forced = client.post(
        "/api/capture/confirm",
        json={"draft_ids": [draft_again["id"]], "company_id": company["id"], "force": True},
        headers=auth,
    )
    assert forced.status_code == 200, forced.text


def test_finance_pack_contains_excel_pdf_and_originals(client, auth):
    company = create_company(client, auth, name="报销包公司")
    png = make_png((250, 250, 250))

    upload = client.post(
        "/api/capture",
        files=[("files", ("shot.png", png, "image/png"))],
        data={"company_id": str(company["id"])},
        headers=auth,
    )
    draft = upload.json()["drafts"][0]
    client.patch(
        f"/api/capture/drafts/{draft['id']}",
        json={"amount": "66.50", "occurred_on": "2026-11-04", "title": "办公用品"},
        headers=auth,
    )
    confirm = client.post(
        "/api/capture/confirm",
        json={"draft_ids": [draft["id"]], "company_id": company["id"]},
        headers=auth,
    ).json()
    receivable_id = confirm["created"][0]["id"]

    pack = client.post(
        "/api/export/finance-pack",
        json={"receivable_ids": [receivable_id]},
        headers=auth,
    )
    assert pack.status_code == 200, pack.text
    assert pack.headers["content-type"] == "application/zip"

    with zipfile.ZipFile(io.BytesIO(pack.content)) as archive:
        names = archive.namelist()
        assert "00_封面.pdf" in names
        assert "01_报销明细.xlsx" in names
        assert "02_合并票据.pdf" in names
        assert any(name.startswith("04_截图凭证/") for name in names)
        assert archive.read("00_封面.pdf").startswith(b"%PDF")
        assert archive.read("02_合并票据.pdf").startswith(b"%PDF")

    records = client.get("/api/export/records", headers=auth).json()
    assert len(records) == 1
    assert records[0]["amount"] == 66.5


def test_user_isolation_and_admin_boundary(client, auth):
    company = create_company(client, auth, name="隔离公司")
    receivable = create_receivable(client, auth, company["id"], "隔离测试", 10.0)

    # 第二个用户
    response = client.post(
        "/api/auth/register",
        json={"username": "other_user", "password": "{{SECRET_fv3x91qq}}"},
    )
    assert response.status_code == 200
    other_headers = {"Authorization": f"Bearer {response.json()['token']}"}

    assert client.get(f"/api/receivables/{receivable['id']}", headers=other_headers).status_code == 404
    assert client.get("/api/receivables", headers=other_headers).json()["total"] == 0
    assert client.get("/api/companies", headers=other_headers).json() == []
    assert client.delete(f"/api/companies/{company['id']}", headers=other_headers).status_code == 404

    # 非管理员不能维护用户
    assert client.get("/api/admin/users", headers=other_headers).status_code == 403
    assert client.post(
        "/api/admin/users",
        json={"username": "hacker", "password": "{{SECRET_9km2v4aa}}"},
        headers=other_headers,
    ).status_code == 403
    assert client.delete("/api/admin/users/1", headers=other_headers).status_code == 403
