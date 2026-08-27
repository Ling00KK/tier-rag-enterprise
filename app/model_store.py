import base64
import hashlib
import json
import os
import uuid
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from openai import OpenAI


def validate_model_config(payload):
    parsed = urlparse(payload.get("base_url", ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("模型地址必须是有效的 HTTP/HTTPS 地址，且不能在地址中包含账号密码")
    allowed = [item.strip().lower() for item in os.getenv("MODEL_ALLOWED_HOSTS", "").split(",") if item.strip()]
    if allowed and parsed.hostname.lower() not in allowed:
        raise ValueError("该模型地址不在服务器允许列表中")
    if not str(payload.get("model", "")).strip():
        raise ValueError("模型名称不能为空")
    timeout = int(payload.get("timeout", 120))
    if timeout < 5 or timeout > 300:
        raise ValueError("超时时间必须在 5 到 300 秒之间")


def _path():
    configured = os.getenv("MODEL_CONFIG_PATH")
    if configured:
        return Path(configured)
    return Path(os.getenv("INTEGRATIONS_CONFIG", "/data/config/integrations.json")).parent / "model.json"


def _fernet():
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(os.environ["SESSION_SECRET"].encode()).digest()))


def _default_model():
    key = os.getenv("VLLM_API_KEY", "EMPTY")
    return {"id": "company-default", "name": "公司本地 Qwen", "provider": "company_vllm", "base_url": os.getenv("VLLM_BASE_URL", "http://192.168.18.146:8000/v1"), "model": os.getenv("VLLM_MODEL", "Qwen3.8-27B"), "timeout": 120, "api_key_encrypted": _fernet().encrypt(key.encode()).decode()}


def _read_store():
    if not _path().exists():
        model = _default_model()
        return {"active_id": model["id"], "models": [model]}
    data = json.loads(_path().read_text(encoding="utf-8"))
    if "models" in data:
        return data
    legacy = dict(data)
    legacy["id"] = legacy.get("id", "legacy-default")
    legacy["name"] = legacy.get("name", legacy.get("model", "默认模型"))
    return {"active_id": legacy["id"], "models": [legacy]}


def _write_store(store):
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _public(model, active_id, include_secret=False):
    result = {key: value for key, value in model.items() if key != "api_key_encrypted"}
    result["configured_key"] = bool(model.get("api_key_encrypted"))
    result["active"] = model.get("id") == active_id
    if include_secret:
        result["api_key"] = _fernet().decrypt(model["api_key_encrypted"].encode()).decode() if model.get("api_key_encrypted") else "EMPTY"
    return result


def list_model_configs():
    store = _read_store()
    return [_public(model, store["active_id"]) for model in store["models"]]


def load_model_config(include_secret=False):
    store = _read_store()
    model = next((item for item in store["models"] if item.get("id") == store.get("active_id")), None)
    model = model or (store["models"][0] if store["models"] else _default_model())
    return _public(model, model["id"], include_secret=include_secret)


def save_model_config(payload, activate=True):
    validate_model_config(payload)
    store = _read_store()
    model_id = str(payload.get("id") or uuid.uuid4().hex)
    current = next((item for item in store["models"] if item.get("id") == model_id), None)
    existing_key = "EMPTY"
    if current and current.get("api_key_encrypted"):
        existing_key = _fernet().decrypt(current["api_key_encrypted"].encode()).decode()
    api_key = payload.get("api_key") or existing_key
    data = {"id": model_id, "name": str(payload.get("name") or payload["model"]).strip(), "provider": payload.get("provider", "openai_compatible"), "base_url": payload["base_url"].rstrip("/"), "model": payload["model"].strip(), "timeout": int(payload.get("timeout", 120)), "api_key_encrypted": _fernet().encrypt(api_key.encode()).decode()}
    if current:
        store["models"][store["models"].index(current)] = data
    else:
        store["models"].append(data)
    if activate:
        store["active_id"] = model_id
    _write_store(store)
    return _public(data, store["active_id"])


def activate_model_config(model_id):
    store = _read_store()
    if not any(item.get("id") == model_id for item in store["models"]):
        raise ValueError("模型不存在")
    store["active_id"] = model_id
    _write_store(store)
    return load_model_config()


def delete_model_config(model_id):
    store = _read_store()
    if store.get("active_id") == model_id:
        raise ValueError("当前启用的模型不能删除，请先切换到其他模型")
    remaining = [item for item in store["models"] if item.get("id") != model_id]
    if len(remaining) == len(store["models"]):
        raise ValueError("模型不存在")
    store["models"] = remaining
    _write_store(store)


def test_model_config(payload):
    validate_model_config(payload)
    key = payload.get("api_key")
    if not key and payload.get("id"):
        store = _read_store()
        current = next((item for item in store["models"] if item.get("id") == payload["id"]), None)
        if current and current.get("api_key_encrypted"):
            key = _fernet().decrypt(current["api_key_encrypted"].encode()).decode()
    client = OpenAI(base_url=payload["base_url"].rstrip("/"), api_key=key or "EMPTY", timeout=float(payload.get("timeout", 30)))
    response = client.chat.completions.create(model=payload["model"], messages=[{"role": "user", "content": "只回答：连接成功"}], temperature=0, max_tokens=20)
    return {"ok": True, "response": (response.choices[0].message.content or "").strip(), "model": getattr(response, "model", payload["model"])}
