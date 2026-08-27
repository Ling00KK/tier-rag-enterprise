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


if __name__ == "__main__":
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as directory:
        from pathlib import Path
        import os
        os.environ["MODEL_CONFIG_PATH"] = str(Path(directory) / "model.json")
        os.environ["SESSION_SECRET"] = "test-secret-that-is-long-enough-123"
        test_model_config_is_encrypted_and_masked(Path(directory), type("M", (), {"setenv": staticmethod(os.environ.__setitem__)})())
    print("model-store tests passed")
