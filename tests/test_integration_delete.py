import json

from app import integration_store


def test_delete_online_source(tmp_path, monkeypatch):
    path = tmp_path / "integrations.json"
    monkeypatch.setenv("INTEGRATIONS_CONFIG", str(path))
    path.write_text(json.dumps({"integrations": [
        {"id": "1", "provider": "wps", "name": "员工守则", "secrets": {}},
        {"id": "2", "provider": "s3", "name": "云存储", "secrets": {}},
    ]}, ensure_ascii=False), encoding="utf-8")
    assert integration_store.delete_online_source("wps", "员工守则")
    assert not integration_store.delete_online_source("wps", "员工守则")
    remaining = json.loads(path.read_text(encoding="utf-8"))["integrations"]
    assert [item["name"] for item in remaining] == ["云存储"]


if __name__ == "__main__":
    from pathlib import Path
    from tempfile import TemporaryDirectory
    import os
    with TemporaryDirectory() as directory:
        target = Path(directory) / "integrations.json"
        os.environ["INTEGRATIONS_CONFIG"] = str(target)
        target.write_text(json.dumps({"integrations": [{"id": "1", "provider": "wps", "name": "测试", "secrets": {}}]}), encoding="utf-8")
        assert integration_store.delete_online_source("wps", "测试")
        assert json.loads(target.read_text())["integrations"] == []
    print("integration-delete tests passed")
