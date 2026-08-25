import base64
import hashlib
import json
import os
import uuid
from pathlib import Path

from cryptography.fernet import Fernet


SECRET_FIELDS = {
    "wps": {"access_token", "app_key"},
    "kingsoft": {"access_token", "app_key"},
    "tencent_docs": {"access_token", "client_secret"},
    "s3": {"access_key", "secret_key"},
}


def _path():
    return Path(os.getenv("INTEGRATIONS_CONFIG", "/data/config/integrations.json"))


def _fernet():
    secret = os.environ["SESSION_SECRET"].encode()
    key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(key)


def _read():
    path = _path()
    if not path.exists():
        return {"integrations": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _write(data):
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def list_integrations():
    result = []
    for item in _read()["integrations"]:
        public = {key: value for key, value in item.items() if key != "secrets"}
        public["configured_secrets"] = sorted(item.get("secrets", {}).keys())
        result.append(public)
    return result


def save_integration(payload):
    provider = payload.get("provider", "").lower()
    if provider not in SECRET_FIELDS:
        raise ValueError("不支持的在线资料类型")
    data = _read()
    item_id = payload.get("id") or uuid.uuid4().hex
    existing = next((item for item in data["integrations"] if item["id"] == item_id), None)
    item = existing or {"id": item_id, "secrets": {}}
    item.update({
        key: value for key, value in payload.items()
        if key not in SECRET_FIELDS[provider] and key not in {"id", "secrets"}
    })
    item["provider"] = provider
    for field in SECRET_FIELDS[provider]:
        value = payload.get(field)
        if value:
            item["secrets"][field] = _fernet().encrypt(value.encode()).decode()
    if not existing:
        data["integrations"].append(item)
    _write(data)
    return item_id


def online_sources():
    sources = []
    for item in _read()["integrations"]:
        if not item.get("enabled", True) or item["provider"] == "s3":
            continue
        source = {key: value for key, value in item.items() if key != "secrets"}
        source["_secrets"] = {
            key: _fernet().decrypt(value.encode()).decode()
            for key, value in item.get("secrets", {}).items()
        }
        sources.append(source)
    return sources


def delete_online_source(provider, name):
    data = _read()
    before = len(data["integrations"])
    data["integrations"] = [
        item for item in data["integrations"]
        if not (item.get("provider") == provider and item.get("name") == name)
    ]
    if len(data["integrations"]) == before:
        return False
    _write(data)
    return True


def s3_config(config_id=None):
    items = [item for item in _read()["integrations"] if item["provider"] == "s3"]
    item = next((value for value in items if value["id"] == config_id), None) if config_id else (items[0] if items else None)
    if not item:
        raise RuntimeError("尚未配置云端资料库")
    result = {key: value for key, value in item.items() if key != "secrets"}
    result.update({
        key: _fernet().decrypt(value.encode()).decode()
        for key, value in item.get("secrets", {}).items()
    })
    return result
