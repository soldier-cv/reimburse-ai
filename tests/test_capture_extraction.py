import io
import zipfile

from PIL import Image


def make_png() -> bytes:
    image = Image.new("RGB", (500, 700), (245, 245, 245))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def create_company(client, auth, name="AI 公司"):
    response = client.post("/api/companies", json={"name": name, "payday": 10}, headers=auth)
    assert response.status_code == 200, response.text
    return response.json()


def test_ai_extraction_maps_draft_fields(client, auth, monkeypatch):
    company = create_company(client, auth)
    # 启用 AI，让采集流程走视觉识别分支
    saved = client.post(
        "/api/settings/ai",
        json={
            "enabled": True,
            "base_url": "https://example.invalid/v1",
            "api_key": "test-key",
            "model_name": "test-vl",
            "system_prompt": "",
        },
        headers=auth,
    )
    assert saved.status_code == 200, saved.text

    async def fake_extract(**kwargs):
        return {
            "doc_type": "payment",
            "amount": 128.0,
            "amount_candidates": [128.0, 1280.0],
            "date": "2026-11-03",
            "merchant": "滴滴出行",
            "item": "网约车",
            "title": "滴滴打车",
            "invoice_no": "",
            "confidence": {"amount": 0.98},
            "evidence": "支付金额 -128.00",
            "entries": [],
        }

    monkeypatch.setattr("app.services.capture.extract_document_with_ai", fake_extract)

    upload = client.post(
        "/api/capture",
        files=[("files", ("shot.png", make_png(), "image/png"))],
        data={"company_id": str(company["id"])},
        headers=auth,
    )
    assert upload.status_code == 200, upload.text
    draft = upload.json()["drafts"][0]
    assert draft["status"] == "ready"
    assert draft["amount_cents"] == 12800
    assert draft["occurred_on"] == "2026-11-03"
    assert draft["title"] == "滴滴打车"
    assert draft["merchant"] == "滴滴出行"

    confirm = client.post(
        "/api/capture/confirm",
        json={"draft_ids": [draft["id"]]},
        headers=auth,
    )
    assert confirm.status_code == 200, confirm.text
    created = confirm.json()["created"][0]
    assert created["amount_cents"] == 12800
    assert created["company_id"] == company["id"]


def test_zip_upload_creates_one_draft_per_file(client, auth):
    company = create_company(client, auth, "压缩包公司")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("2026-11/打车.png", make_png())
        archive.writestr("2026-11/说明.txt", "ignore me")
    buffer.seek(0)

    upload = client.post(
        "/api/capture",
        files=[("files", ("invoices.zip", buffer.getvalue(), "application/zip"))],
        data={"company_id": str(company["id"])},
        headers=auth,
    )
    assert upload.status_code == 200, upload.text
    drafts = upload.json()["drafts"]
    assert len(drafts) == 1
    assert drafts[0]["original_filename"].endswith(".png")
    assert drafts[0]["company_id"] == company["id"]
