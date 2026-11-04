import io
import zipfile


def create_company(client, auth, name="通行费公司"):
    response = client.post("/api/companies", json={"name": name, "payday": 8}, headers=auth)
    assert response.status_code == 200, response.text
    return response.json()


def test_toll_zip_groups_trip_and_invoice_package(client, auth):
    company = create_company(client, auth)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("通行费电子票据汇总单(票据).pdf", b"%PDF-1.4\n%fake\n")
        archive.writestr("通行费电子票据汇总单(行程).pdf", b"%PDF-1.4\n%fake\n")
        archive.writestr("通行费电子发票.zip", b"PK\x05\x06" + b"\x00" * 18)
    buffer.seek(0)

    upload = client.post(
        "/api/capture",
        files=[("files", ("2026-11.zip", buffer.getvalue(), "application/zip"))],
        data={"company_id": str(company["id"])},
        headers=auth,
    )
    assert upload.status_code == 200, upload.text
    drafts = upload.json()["drafts"]
    assert len(drafts) == 1, [d["original_filename"] for d in drafts]
    main = drafts[0]
    assert "票据" in main["original_filename"]
    assert main["extra_file_count"] == 2

    # 主汇总单的附件在确认时应一并挂载，且不产生第二笔应收
    client.patch(
        f"/api/capture/drafts/{main['id']}",
        json={"amount": "100.00", "occurred_on": "2026-11-03", "title": "通行费汇总"},
        headers=auth,
    )
    confirm = client.post(
        "/api/capture/confirm",
        json={"draft_ids": [main["id"]], "company_id": company["id"]},
        headers=auth,
    )
    assert confirm.status_code == 200, confirm.text
    receivable_id = confirm.json()["created"][0]["id"]
    detail = client.get(f"/api/receivables/{receivable_id}", headers=auth).json()
    assert detail["attachment_count"] == 3
    assert client.get("/api/receivables", headers=auth).json()["total"] == 1
