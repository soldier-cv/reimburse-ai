"""批次筛选、收件箱批量操作、台账批量删除。"""

import io

from PIL import Image


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (300, 400), (240, 240, 240)).save(buffer, format="PNG")
    return buffer.getvalue()


def _company(client, auth, name="批量公司"):
    resp = client.post("/api/companies", json={"name": name, "payday": 15}, headers=auth)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _receivable(client, auth, company_id, title, amount, period):
    resp = client.post(
        "/api/receivables",
        json={"company_id": company_id, "title": title, "amount": amount, "occurred_on": f"{period}-05", "period": period},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_batch_filters_and_kind_label(client, auth):
    company = _company(client, auth)
    may = _receivable(client, auth, company["id"], "5月报销", 100.0, "2026-05")
    june = _receivable(client, auth, company["id"], "6月报销", 200.0, "2026-06")
    first = client.post(
        "/api/batches",
        json={"company_id": company["id"], "period": "2026-05", "receivable_ids": [may["id"]], "submit": True},
        headers=auth,
    )
    assert first.status_code == 200, first.text
    second = client.post(
        "/api/batches",
        json={"company_id": company["id"], "period": "2026-06", "receivable_ids": [june["id"]]},
        headers=auth,
    )
    assert second.status_code == 200, second.text

    all_batches = client.get("/api/batches", headers=auth).json()
    assert len(all_batches) == 2
    assert all(batch["kind_label"] == "报销" for batch in all_batches)

    may_batches = client.get("/api/batches", params={"period_from": "2026-05", "period_to": "2026-05"}, headers=auth).json()
    assert [b["period"] for b in may_batches] == ["2026-05"]

    draft_batches = client.get("/api/batches", params={"status_value": "draft"}, headers=auth).json()
    assert [b["period"] for b in draft_batches] == ["2026-06"]

    searched = client.get("/api/batches", params={"q": "2026-06"}, headers=auth).json()
    assert [b["period"] for b in searched] == ["2026-06"]


def test_batch_draft_retry_and_delete(client, auth, monkeypatch):
    company = _company(client, auth)
    calls = []

    async def fake_ai(**kwargs):
        calls.append(kwargs)
        return {"doc_type": "receipt", "amount": 12.5, "date": "2026-11-01", "entries": [], "method": "ai_vision"}

    monkeypatch.setattr("app.services.capture.extract_document_with_ai", fake_ai)
    saved = client.post(
        "/api/settings/ai",
        json={"enabled": True, "base_url": "https://example.invalid/v1", "api_key": "k", "model_name": "m", "system_prompt": ""},
        headers=auth,
    )
    assert saved.status_code == 200, saved.text
    uploaded = client.post(
        "/api/capture",
        files=[("files", ("a.png", _png(), "image/png")), ("files", ("b.png", _png(), "image/png"))],
        data={"company_id": company["id"]},
        headers=auth,
    ).json()
    ids = [d["id"] for d in uploaded["drafts"]]
    assert len(ids) == 2

    retried = client.post("/api/capture/drafts/batch-retry", json={"draft_ids": ids}, headers=auth)
    assert retried.status_code == 200, retried.text
    assert retried.json()["retried"] == 2

    deleted = client.post("/api/capture/drafts/batch-delete", json={"draft_ids": ids}, headers=auth)
    assert deleted.status_code == 200, deleted.text
    assert sorted(deleted.json()["deleted"]) == sorted(ids)
    assert client.get("/api/capture/drafts", headers=auth).json() == []


def test_batch_delete_receivables_skips_submitted(client, auth):
    company = _company(client, auth)
    pending1 = _receivable(client, auth, company["id"], "待删1", 10.0, "2026-05")
    pending2 = _receivable(client, auth, company["id"], "待删2", 20.0, "2026-06")
    submitted = _receivable(client, auth, company["id"], "保留", 30.0, "2026-07")
    batch = client.post(
        "/api/batches",
        json={"company_id": company["id"], "period": "2026-07", "receivable_ids": [submitted["id"]], "submit": True},
        headers=auth,
    )
    assert batch.status_code == 200, batch.text

    result = client.post(
        "/api/receivables/batch-delete",
        json={"receivable_ids": [pending1["id"], pending2["id"], submitted["id"]]},
        headers=auth,
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert sorted(body["deleted"]) == sorted([pending1["id"], pending2["id"]])
    assert [s["id"] for s in body["skipped"]] == [submitted["id"]]

    remaining = client.get("/api/receivables?page_size=200", headers=auth).json()["items"]
    assert [item["id"] for item in remaining] == [submitted["id"]]
