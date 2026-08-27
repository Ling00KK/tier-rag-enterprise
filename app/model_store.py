import base64
import hashlib
import json
import os
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


def load_model_config(include_secret=False):
    defaults = {"provider": "company_vllm", "base_url": os.getenv("VLLM_BASE_URL", "http://192.168.18.146:8000/v1"), "model": os.getenv("VLLM_MODEL", "Qwen3.8-27B"), "timeout": 120, "configured_key": bool(os.getenv("VLLM_API_KEY"))}
    if not _path().exists():
        if include_secret:
            defaults["api_key"] = os.getenv("VLLM_API_KEY", "EMPTY")
        return defaults
    data = json.loads(_path().read_text(encoding="utf-8"))
    public = {key: value for key, value in data.items() if key != "api_key_encrypted"}
    public["configured_key"] = bool(data.get("api_key_encrypted"))
    if include_secret:
        public["api_key"] = _fernet().decrypt(data["api_key_encrypted"].encode()).decode() if data.get("api_key_encrypted") else "EMPTY"
    return public


def save_model_config(payload):
    validate_model_config(payload)
    current = load_model_config(include_secret=True)
    api_key = payload.get("api_key") or current.get("api_key") or "EMPTY"
    data = {
        "provider": payload.get("provider", "openai_compatible"),
        "base_url": payload["base_url"].rstrip("/"),
        "model": payload["model"],
        "timeout": int(payload.get("timeout", 120)),
        "api_key_encrypted": _fernet().encrypt(api_key.encode()).decode(),
    }
    path = _path(); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    try: path.chmod(0o600)
    except OSError: pass
    return load_model_config()


def test_model_config(payload):
    validate_model_config(payload)
    current = load_model_config(include_secret=True)
    key = payload.get("api_key") or current.get("api_key") or "EMPTY"
    client = OpenAI(base_url=payload["base_url"].rstrip("/"), api_key=key, timeout=float(payload.get("timeout", 30)))
    response = client.chat.completions.create(model=payload["model"], messages=[{"role": "user", "content": "只回答：连接成功"}], temperature=0, max_tokens=20)
    return {"ok": True, "response": (response.choices[0].message.content or "").strip(), "model": getattr(response, "model", payload["model"])}
