"""票据预览接口与缓存。"""

import io
from pathlib import Path

from PIL import Image
import pymupdf


def create_company(client, auth, name="预览公司"):
    response = client.post("/api/companies", json={"name": name, "payday": 10}, headers=auth)
    assert response.status_code == 200, response.text
    return response.json()


def _png_bytes(size=(800, 1200), color=(30, 80, 160)) -> bytes:
    image = Image.new("RGB", size, color)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _pdf_bytes(pages=3) -> bytes:
    document = pymupdf.open()
    for index in range(pages):
        page = document.new_page(width=595, height=842)
        page.insert_text((72, 72), f"Page {index + 1}", fontsize=28)
    data = document.tobytes()
    document.close()
    return data


def test_image_preview_and_cache(client, auth, tmp_path, monkeypatch):
    company = create_company(client, auth)
    upload = client.post(
        "/api/capture",
        files=[("files", ("shot.png", _png_bytes(), "image/png"))],
        data={"company_id": str(company["id"])},
        headers=auth,
    )
    draft_id = upload.json()["drafts"][0]["id"]
    first = client.get(f"/api/capture/drafts/{draft_id}/preview?page=1", headers=auth)
    assert first.status_code == 200, first.text
    assert first.headers["content-type"].startswith("image/jpeg")
    assert first.headers["x-preview-pages"] == "1"
    assert first.headers["x-preview-page"] == "1"
    assert first.headers["x-preview-kind"] == "image"
    body = first.content
    assert body[:2] == b"\xff\xd8"  # JPEG
    # 第二次应命中缓存，内容相同
    second = client.get(f"/api/capture/drafts/{draft_id}/preview?page=1", headers=auth)
    assert second.status_code == 200
    assert second.content == body
    # 图片只有一页
    bad = client.get(f"/api/capture/drafts/{draft_id}/preview?page=2", headers=auth)
    assert bad.status_code == 400


def test_pdf_preview_pages(client, auth):
    company = create_company(client, auth, name="PDF预览")
    upload = client.post(
        "/api/capture",
        files=[("files", ("invoice.pdf", _pdf_bytes(3), "application/pdf"))],
        data={"company_id": str(company["id"])},
        headers=auth,
    )
    draft_id = upload.json()["drafts"][0]["id"]
    page1 = client.get(f"/api/capture/drafts/{draft_id}/preview?page=1", headers=auth)
    assert page1.status_code == 200, page1.text
    assert page1.headers["x-preview-pages"] == "3"
    assert page1.headers["x-preview-kind"] == "pdf"
    page3 = client.get(f"/api/capture/drafts/{draft_id}/preview?page=3", headers=auth)
    assert page3.status_code == 200
    assert page3.headers["x-preview-page"] == "3"
    # 不同页内容不同
    assert page1.content != page3.content
    # 再取第一页应走缓存（内容不变）
    again = client.get(f"/api/capture/drafts/{draft_id}/preview?page=1", headers=auth)
    assert again.content == page1.content
    overflow = client.get(f"/api/capture/drafts/{draft_id}/preview?page=4", headers=auth)
    assert overflow.status_code == 400


def test_ofd_preview_is_unsupported(client, auth):
    company = create_company(client, auth, name="OFD预览")
    upload = client.post(
        "/api/capture",
        files=[("files", ("invoice.ofd", b"%OFD fake", "application/ofd"))],
        data={"company_id": str(company["id"])},
        headers=auth,
    )
    assert upload.status_code == 200, upload.text
    draft_id = upload.json()["drafts"][0]["id"]
    preview = client.get(f"/api/capture/drafts/{draft_id}/preview", headers=auth)
    assert preview.status_code == 400
    assert "OFD" in preview.json()["detail"]


def test_attachment_preview_and_thumb_not_original(client, auth):
    company = create_company(client, auth, name="附件预览")
    original = _png_bytes((2000, 1500), (10, 10, 10))
    upload = client.post(
        "/api/capture",
        files=[("files", ("big.png", original, "image/png"))],
        data={"company_id": str(company["id"])},
        headers=auth,
    )
    draft_id = upload.json()["drafts"][0]["id"]
    client.patch(f"/api/capture/drafts/{draft_id}", json={"amount": "12.00", "occurred_on": "2026-11-01"}, headers=auth)
    confirm = client.post(
        "/api/capture/confirm",
        json={"draft_ids": [draft_id], "company_id": company["id"]},
        headers=auth,
    )
    assert confirm.status_code == 200, confirm.text
    receivable_id = confirm.json()["created"][0]["id"]
    detail = client.get(f"/api/receivables/{receivable_id}", headers=auth).json()
    attachment_id = detail["attachments"][0]["id"]

    thumb = client.get(f"/api/attachments/{attachment_id}/thumb", headers=auth)
    assert thumb.status_code == 200
    assert thumb.headers["content-type"].startswith("image/jpeg")
    assert thumb.content != original
    assert len(thumb.content) < len(original)

    preview = client.get(f"/api/attachments/{attachment_id}/preview?page=1", headers=auth)
    assert preview.status_code == 200
    assert preview.headers["x-preview-pages"] == "1"
    assert preview.content[:2] == b"\xff\xd8"
