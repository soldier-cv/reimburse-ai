"""上传反馈、压缩包跳过说明与重复票据提示。"""

import io
import zipfile

from PIL import Image


def company(client, auth, name="采集公司"):
    response = client.post("/api/companies", json={"name": name, "invoice_title": name, "tax_id": "91110000TEST"}, headers=auth)
    assert response.status_code == 200, response.text
    return response.json()


def png(color=(120, 120, 120)) -> bytes:
    image = Image.new("RGB", (60, 80), color)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def test_zip_reports_skipped_nested_and_unsupported(client, auth):
    firm = company(client, auth)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("ok.png", png())
        archive.writestr("nested/inner.zip", b"PK\x05\x06" + b"\x00" * 18)
        archive.writestr("notes.docx", b"not-an-image")
        archive.writestr("__MACOSX/._ok.png", b"junk")
    buffer.seek(0)
    upload = client.post(
        "/api/capture",
        files=[("files", ("batch.zip", buffer.getvalue(), "application/zip"))],
        data={"company_id": str(firm["id"])},
        headers=auth,
    )
    assert upload.status_code == 200, upload.text
    body = upload.json()
    assert body["count"] == 1
    assert body["skipped_count"] >= 2
    reasons = " ".join(item["reason"] + item["filename"] for item in body["skipped"])
    assert "嵌套" in reasons or "zip" in reasons.lower()
    assert "docx" in reasons.lower() or "不支持" in reasons
    assert "已生成 1 条草稿" in body["message"]
    assert "跳过" in body["message"]


def test_upload_and_confirm_surface_duplicate_warnings(client, auth):
    firm = company(client, auth, name="重复公司")
    data = png((90, 10, 10))
    first = client.post(
        "/api/capture",
        files=[("files", ("receipt.png", data, "image/png"))],
        data={"company_id": str(firm["id"])},
        headers=auth,
    )
    draft_id = first.json()["drafts"][0]["id"]
    client.patch(
        f"/api/capture/drafts/{draft_id}",
        json={"amount": "25.00", "occurred_on": "2026-11-04", "invoice_no": "12345678", "invoice_code": "044001800111", "title": "餐饮"},
        headers=auth,
    )
    confirm = client.post(
        "/api/capture/confirm",
        json={"draft_ids": [draft_id], "company_id": firm["id"]},
        headers=auth,
    )
    assert confirm.status_code == 200, confirm.text

    again = client.post(
        "/api/capture",
        files=[("files", ("receipt-copy.png", data, "image/png"))],
        data={"company_id": str(firm["id"])},
        headers=auth,
    )
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["duplicate_count"] >= 1
    assert body["drafts"][0]["duplicates"]
    assert any("已经" in (item.get("message") or "") or "相同" in (item.get("message") or "") for item in body["duplicates"])

    draft2 = body["drafts"][0]["id"]
    client.patch(
        f"/api/capture/drafts/{draft2}",
        json={"amount": "25.00", "occurred_on": "2026-11-04", "invoice_no": "12345678", "invoice_code": "044001800111"},
        headers=auth,
    )
    blocked = client.post(
        "/api/capture/confirm",
        json={"draft_ids": [draft2], "company_id": firm["id"]},
        headers=auth,
    )
    assert blocked.status_code == 409, blocked.text
    warnings = blocked.json()["detail"]["warnings"]
    assert any(item["type"] in ("file_hash", "invoice_no") for item in warnings)
    forced = client.post(
        "/api/capture/confirm",
        json={"draft_ids": [draft2], "company_id": firm["id"], "force": True},
        headers=auth,
    )
    assert forced.status_code == 200, forced.text
