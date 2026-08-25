import hashlib
import hmac
import json
import os
import secrets
import threading
from pathlib import Path

DEFAULT_DEPARTMENTS = ["人事部", "财务部", "销售部", "技术部"]
_lock = threading.Lock()

def _path():
    explicit = os.getenv("ACCESS_CONTROL_CONFIG")
    if explicit:
        return Path(explicit)
    integrations = Path(os.getenv("INTEGRATIONS_CONFIG", "/data/config/integrations.json"))
    return integrations.with_name("access_control.json")

def _read():
    path = _path()
    if not path.exists():
        return {"departments": DEFAULT_DEPARTMENTS, "users": [], "documents": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("departments", DEFAULT_DEPARTMENTS)
    data.setdefault("users", [])
    data.setdefault("documents", {})
    return data

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

def _password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return salt, digest

def authenticate(username, password, legacy_username, legacy_salt, legacy_hash):
    if username == legacy_username:
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), legacy_salt, 200_000).hex()
        if hmac.compare_digest(digest, legacy_hash):
            return {"username": username, "role": "admin", "departments": [], "enabled": True}
    user = next((item for item in _read()["users"] if item["username"] == username), None)
    if not user or not user.get("enabled", True):
        return None
    _, digest = _password(password, user["password_salt"])
    if not hmac.compare_digest(digest, user["password_hash"]):
        return None
    return {key: user.get(key) for key in ("username", "display_name", "role", "departments", "enabled")}

def list_access_data():
    data = _read()
    users = [{key: item.get(key) for key in ("username", "display_name", "role", "departments", "enabled")} for item in data["users"]]
    return {"departments": data["departments"], "users": users}

def add_department(name):
    name = name.strip()
    if not name:
        raise ValueError("部门名称不能为空")
    with _lock:
        data = _read()
        if name not in data["departments"]:
            data["departments"].append(name)
            _write(data)

def save_user(payload):
    username = payload["username"].strip()
    if not username or username == os.getenv("APP_USERNAME", "tier"):
        raise ValueError("该用户名不可用")
    with _lock:
        data = _read()
        if set(payload.get("departments", [])) - set(data["departments"]):
            raise ValueError("包含不存在的部门")
        user = next((item for item in data["users"] if item["username"] == username), None)
        if not user:
            if not payload.get("password"):
                raise ValueError("新员工必须设置密码")
            user = {"username": username}
            data["users"].append(user)
        user.update({"display_name": payload.get("display_name") or username, "role": payload.get("role", "employee"), "departments": payload.get("departments", []), "enabled": payload.get("enabled", True)})
        if payload.get("password"):
            user["password_salt"], user["password_hash"] = _password(payload["password"])
        _write(data)

def set_document_access(name, scope="public", departments=None):
    if scope not in {"public", "departments", "admin"}:
        raise ValueError("资料可见范围无效")
    with _lock:
        data = _read()
        data["documents"][name] = {"scope": scope, "departments": departments or []}
        _write(data)

def get_document_access(name, item=None):
    stored = _read()["documents"].get(name)
    if stored:
        return stored
    item = item or {}
    return {
        "scope": item.get("access_scope", "public"),
        "departments": item.get("departments") or [],
    }

def can_access(name, user, item=None):
    if user.get("role") == "admin":
        return True
    acl = get_document_access(name, item)
    scope, departments = acl["scope"], acl.get("departments", [])
    if scope == "public":
        return True
    if scope == "admin":
        return False
    return bool(set(user.get("departments") or []) & set(departments))
