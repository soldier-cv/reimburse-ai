
from app.services.ai_service import sort_model_names


def test_sort_model_names_pins_selected_and_prefers_vision():
    models = [
        "text-embedding-3-small",
        "gpt-4o-mini",
        "free/agnes-3.0-flash",
        "qwen-plus",
        "qwen-vl-max",
    ]
    ordered = sort_model_names(models, selected="free/agnes-3.0-flash")
    assert ordered[0] == "free/agnes-3.0-flash"
    visionish = {"gpt-4o-mini", "qwen-vl-max"}
    assert set(ordered[1:3]) == visionish
    assert ordered[-2:] == ["qwen-plus", "text-embedding-3-small"]


def test_test_connection_uses_form_model_not_saved_default(client, auth, monkeypatch):
    captured = {}

    async def fake_test(base_url, api_key, model_name):
        captured["base_url"] = base_url
        captured["api_key"] = api_key
        captured["model_name"] = model_name
        return {"success": True, "message": f"连接成功，模型 {model_name} 响应正常", "model": model_name}

    monkeypatch.setattr("app.main.test_ai_connection", fake_test)

    # 库里仍是默认/旧模型，表单已改为新模型且尚未保存
    response = client.post(
        "/api/settings/ai/test",
        headers=auth,
        json={
            "base_url": "http://100.64.0.88:8317/v1",
            "api_key": "sk-form",
            "model_name": "free/agnes-3.0-flash",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["success"] is True
    assert data["model"] == "free/agnes-3.0-flash"
    assert captured == {
        "base_url": "http://100.64.0.88:8317/v1",
        "api_key": "sk-form",
        "model_name": "free/agnes-3.0-flash",
    }


def test_test_connection_does_not_fallback_to_builtin_default(client, auth, monkeypatch):
    async def fake_test(base_url, api_key, model_name):
        return {"success": False, "message": "接口地址、密钥和模型名称都不能为空", "model": model_name}

    monkeypatch.setattr("app.main.test_ai_connection", fake_test)

    response = client.post(
        "/api/settings/ai/test",
        headers=auth,
        json={"base_url": "http://example.invalid/v1", "api_key": "sk-form"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["success"] is False
    assert data["model"] == ""
    assert "不能为空" in data["message"]


def test_test_connection_falls_back_to_saved_model_and_key(client, auth, monkeypatch):
    captured = {}

    async def fake_test(base_url, api_key, model_name):
        captured["api_key"] = api_key
        captured["model_name"] = model_name
        return {"success": True, "message": f"连接成功，模型 {model_name} 响应正常", "model": model_name}

    monkeypatch.setattr("app.main.test_ai_connection", fake_test)

    save = client.post(
        "/api/settings/ai",
        headers=auth,
        json={
            "enabled": True,
            "base_url": "https://saved.example/v1",
            "api_key": "sk-saved",
            "model_name": "qwen-vl-max",
            "system_prompt": "",
        },
    )
    assert save.status_code == 200, save.text

    response = client.post(
        "/api/settings/ai/test",
        headers=auth,
        json={"base_url": "https://saved.example/v1", "api_key": "", "model_name": ""},
    )
    assert response.status_code == 200, response.text
    assert captured["api_key"] == "sk-saved"
    assert captured["model_name"] == "qwen-vl-max"


def test_fetch_models_uses_form_credentials(client, auth, monkeypatch):
    captured = {}

    async def fake_fetch(base_url, api_key):
        captured["base_url"] = base_url
        captured["api_key"] = api_key
        return {"success": True, "message": "获取到 2 个模型", "models": ["qwen-vl-max", "free/agnes-3.0-flash"]}

    monkeypatch.setattr("app.main.fetch_available_models", fake_fetch)

    response = client.post(
        "/api/settings/ai/models",
        headers=auth,
        json={"base_url": "http://100.64.0.88:8317/v1", "api_key": "sk-form"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["success"] is True
    assert captured["base_url"] == "http://100.64.0.88:8317/v1"
    assert captured["api_key"] == "sk-form"
    assert "free/agnes-3.0-flash" in data["models"]


def test_save_and_get_ai_settings_keep_key_when_blank(client, auth):
    first = client.post(
        "/api/settings/ai",
        headers=auth,
        json={
            "enabled": True,
            "base_url": "https://api.example/v1",
            "api_key": "sk-keep",
            "model_name": "qwen-vl-max",
            "system_prompt": "自定义提示词",
        },
    )
    assert first.status_code == 200, first.text

    second = client.post(
        "/api/settings/ai",
        headers=auth,
        json={
            "enabled": False,
            "base_url": "https://api.example/v1",
            "api_key": "",
            "model_name": "free/agnes-3.0-flash",
            "system_prompt": "自定义提示词",
        },
    )
    assert second.status_code == 200, second.text

    loaded = client.get("/api/settings/ai", headers=auth)
    assert loaded.status_code == 200, loaded.text
    data = loaded.json()
    assert data["enabled"] is False
    assert data["base_url"] == "https://api.example/v1"
    assert data["model_name"] == "free/agnes-3.0-flash"
    assert data["has_key"] is True
    assert data["system_prompt"] == "自定义提示词"
    assert data["default_system_prompt"]
    assert data["can_call"] is False
