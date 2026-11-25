"""收件箱一键入账建批次、批次部分到账。"""

import io

from PIL import Image


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (300, 400), (240, 240, 240)).save(buffer, format="PNG")
    return buffer.getvalue()


def _enable_ai(client, auth):
    saved = client.post(
        "/api/settings/ai",
        json={"enabled": True, "base_url": "https://example.invalid/v1", "api_key": "k", "model_name": "m", "system_prompt": ""},
        headers=auth,
    )
    assert saved.status_code == 200, saved.text


def _capture(client, auth, monkeypatch, period):
    async def fake_ai(**kwargs):
        return {"doc_type": "receipt", "amount": 100.0, "date": "2026-11-02", "entries": [], "method": "ai_vision"}

    monkeypatch.setattr("app.services.capture.extract_document_with_ai", fake_ai)
    resp = client.post("/api/capture", files=[("files", ("a.png", _png(), "image/png"))], headers=auth)
    assert resp.status_code == 200, resp.text
    draft = resp.json()["drafts"][0]
    patched = client.patch(
        f"/api/capture/drafts/{draft['id']}",
        json={"period": period},
        headers=auth,
    )
    assert patched.status_code == 200, patched.text
    return patched.json()


def test_confirm_and_submit_creates_one_batch(client, auth, monkeypatch):
    _enable_ai(client, auth)
    company = client.post("/api/companies", json={"name": "一键公司"}, headers=auth).json()
    d1 = _capture(client, auth, monkeypatch, "2026-10")
    d2 = _capture(client, auth, monkeypatch, "2026-10")
    for draft in (d1, d2):
        client.patch(f"/api/capture/drafts/{draft['id']}", json={"company_id": company["id"]}, headers=auth)

    result = client.post(
        "/api/capture/confirm-and-submit",
        json={"draft_ids": [d1["id"], d2["id"]], "force": True},
        headers=auth,
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["count"] == 2
    assert body["batch"]["status"] == "submitted"
    assert len(body["created_ids"]) == 2

    batches = client.get("/api/batches", headers=auth).json()
    assert len(batches) == 1
    assert batches[0]["period"] == "2026-10"
    assert batches[0]["count"] == 2

    items = client.get("/api/receivables?page_size=200", headers=auth).json()["items"]
    assert all(item["status"] == "submitted" for item in items)
    assert all(item["batch_id"] for item in items)


def test_confirm_and_submit_rejects_mixed_periods(client, auth, monkeypatch):
    _enable_ai(client, auth)
    company = client.post("/api/companies", json={"name": "跨月公司"}, headers=auth).json()
    d1 = _capture(client, auth, monkeypatch, "2026-09")
    d2 = _capture(client, auth, monkeypatch, "2026-10")
    for draft in (d1, d2):
        client.patch(f"/api/capture/drafts/{draft['id']}", json={"company_id": company["id"]}, headers=auth)

    result = client.post(
        "/api/capture/confirm-and-submit",
        json={"draft_ids": [d1["id"], d2["id"]]},
        headers=auth,
    )
    assert result.status_code == 400
    assert set(result.json()["detail"]["periods"]) == {"2026-09", "2026-10"}
    assert client.get("/api/batches", headers=auth).json() == []


def test_batch_partial_payment(client, auth):
    company = client.post("/api/companies", json={"name": "部分到账公司"}, headers=auth).json()
    ids = []
    for title in ("报销A", "报销B", "报销C"):
        resp = client.post(
            "/api/receivables",
            json={"company_id": company["id"], "title": title, "amount": 100.0, "occurred_on": "2026-11-03"},
            headers=auth,
        )
        ids.append(resp.json()["id"])
    batch = client.post(
        "/api/batches",
        json={"company_id": company["id"], "receivable_ids": ids, "submit": True},
        headers=auth,
    ).json()

    result = client.post(
        f"/api/batches/{batch['batch_id']}/payment",
        json={
            "paid_on": "2026-11-10",
            "note": "只到了一笔",
            "amount": 100.0,
            "allocations": [{"receivable_id": ids[0], "amount": 100.0}],
        },
        headers=auth,
    )
    assert result.status_code == 200, result.text
    detail = client.get(f"/api/receivables/{ids[0]}", headers=auth).json()
    other = client.get(f"/api/receivables/{ids[1]}", headers=auth).json()
    assert detail["status"] == "paid"
    assert other["status"] == "submitted"
