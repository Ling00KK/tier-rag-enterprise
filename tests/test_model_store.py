from app import model_store


def test_model_config_is_encrypted_and_masked(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_CONFIG_PATH", str(tmp_path / "model.json"))
    monkeypatch.setenv("SESSION_SECRET", "test-secret-that-is-long-enough-123")
    payload = {"provider": "openrouter", "base_url": "https://openrouter.ai/api/v1/", "model": "openrouter/free", "api_key": "secret-key", "timeout": 30}
    public = model_store.save_model_config(payload)
    raw = (tmp_path / "model.json").read_text(encoding="utf-8")
    assert "secret-key" not in raw
    assert "api_key" not in public and public["configured_key"]
    assert model_store.load_model_config(include_secret=True)["api_key"] == "secret-key"


def test_model_host_allowlist(monkeypatch):
    monkeypatch.setenv("MODEL_ALLOWED_HOSTS", "openrouter.ai")
    try:
        model_store.validate_model_config({"base_url": "http://127.0.0.1:1/v1", "model": "x", "timeout": 30})
        raise AssertionError("allowlist should reject host")
    except ValueError:
        pass


def test_multiple_models_can_be_saved_activated_and_deleted(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_CONFIG_PATH", str(tmp_path / "models.json"))
    monkeypatch.setenv("SESSION_SECRET", "test-secret-that-is-long-enough-123")
    monkeypatch.setenv("MODEL_ALLOWED_HOSTS", "")
    first = model_store.save_model_config({"id": "company-default", "name": "本地模型", "provider": "company_vllm", "base_url": "http://192.168.18.146:8000/v1", "model": "qwen", "api_key": "EMPTY", "timeout": 30})
    second = model_store.save_model_config({"name": "OpenRouter", "provider": "openrouter", "base_url": "https://openrouter.ai/api/v1", "model": "openrouter/free", "api_key": "key", "timeout": 30}, activate=False)
    items = model_store.list_model_configs()
    assert len(items) == 2
    assert next(item for item in items if item["id"] == first["id"])["active"]
    assert not next(item for item in items if item["id"] == second["id"])["active"]
    model_store.activate_model_config(second["id"])
    assert model_store.load_model_config()["id"] == second["id"]
    model_store.delete_model_config(first["id"])
    assert len(model_store.list_model_configs()) == 1


if __name__ == "__main__":
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as directory:
        from pathlib import Path
        import os
        os.environ["MODEL_CONFIG_PATH"] = str(Path(directory) / "model.json")
        os.environ["SESSION_SECRET"] = "test-secret-that-is-long-enough-123"
        test_model_config_is_encrypted_and_masked(Path(directory), type("M", (), {"setenv": staticmethod(os.environ.__setitem__)})())
    with TemporaryDirectory() as directory:
        test_multiple_models_can_be_saved_activated_and_deleted(Path(directory), type("M", (), {"setenv": staticmethod(os.environ.__setitem__)})())
    print("model-store tests passed")
