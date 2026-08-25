import hashlib
import hmac
import os
import time
import re
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from .rag_engine import RagEngine
from .document_loader import SUPPORTED_EXTENSIONS
from .integration_store import delete_online_source, list_integrations, save_integration, s3_config
from .access_control import add_department, authenticate, can_access, get_document_access, list_access_data, remove_document_access, save_user, set_document_access


BASE_DIR = Path(__file__).resolve().parent
USERNAME = os.getenv("APP_USERNAME", "tier")
PASSWORD_HASH = os.environ["APP_PASSWORD_HASH"]
PASSWORD_SALT = os.environ["APP_PASSWORD_SALT"].encode()
SESSION_SECRET = os.environ["SESSION_SECRET"]

app = FastAPI(title="企业知识库助手", docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    https_only=os.getenv("COOKIE_HTTPS_ONLY", "false").lower() == "true",
    same_site="strict",
    max_age=8 * 60 * 60,
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

engine = RagEngine(
    source_dir=os.getenv("SOURCE_DIR", "/data/source"),
    base_url=os.getenv("VLLM_BASE_URL", "http://192.168.18.146:8000/v1"),
    model=os.getenv("VLLM_MODEL", "Qwen3.8-27B"),
    api_key=os.getenv("VLLM_API_KEY", "EMPTY"),
)
attempts = defaultdict(list)


class LoginRequest(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=200)


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=1000)


class IntegrationRequest(BaseModel):
    provider: str
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    drive_id: str | None = None
    file_id: str | None = None
    endpoint: str | None = None
    content_field: str | None = None
    access_token: str | None = None
    app_id: str | None = None
    app_key: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    open_id: str | None = None
    bucket: str | None = None
    region: str | None = None
    endpoint_url: str | None = None
    prefix: str | None = None
    access_key: str | None = None
    secret_key: str | None = None
    access_scope: str = "public"
    departments: list[str] = Field(default_factory=list)


class DepartmentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class UserRequest(BaseModel):
    username: str = Field(min_length=2, max_length=100)
    display_name: str | None = Field(default=None, max_length=100)
    password: str | None = Field(default=None, min_length=6, max_length=200)
    role: str = "employee"
    departments: list[str] = Field(default_factory=list)
    enabled: bool = True


class DocumentAccessRequest(BaseModel):
    file_name: str = Field(min_length=1, max_length=255)
    access_scope: str
    departments: list[str] = Field(default_factory=list)


class DeleteDocumentRequest(BaseModel):
    document_id: str = Field(min_length=16, max_length=64)


def require_login(request):
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=401, detail="请先登录")


def current_user(request):
    require_login(request)
    user = request.session.get("user")
    if not user and request.session.get("username") == USERNAME:
        user = {"username": USERNAME, "role": "admin", "departments": [], "enabled": True}
        request.session["user"] = user
    if not user:
        raise HTTPException(status_code=401, detail="登录信息已更新，请重新登录")
    return user


def require_admin(request):
    user = current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可以执行此操作")
    return user


@app.get("/")
def home():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/session")
def session_status(request: Request):
    authenticated = bool(request.session.get("authenticated"))
    user = current_user(request) if authenticated else None
    return {"authenticated": authenticated, "user": user}


@app.post("/api/login")
def login(data: LoginRequest, request: Request):
    address = request.client.host if request.client else "unknown"
    now = time.time()
    attempts[address] = [stamp for stamp in attempts[address] if now - stamp < 60]
    if len(attempts[address]) >= 5:
        raise HTTPException(status_code=429, detail="尝试次数过多，请一分钟后重试")
    user = authenticate(data.username, data.password, USERNAME, PASSWORD_SALT, PASSWORD_HASH)
    if not user:
        attempts[address].append(now)
        raise HTTPException(status_code=401, detail="用户名或密码不正确")
    attempts.pop(address, None)
    request.session.clear()
    request.session["authenticated"] = True
    request.session["username"] = user["username"]
    request.session["user"] = user
    return {"ok": True, "user": user}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/status")
def status(request: Request):
    require_login(request)
    return engine.status()


@app.post("/api/sync")
def sync_knowledge_base(request: Request):
    require_admin(request)
    try:
        engine.load(force=True)
        return {"ok": True, **engine.status()}
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"知识库同步失败：{error}") from error


@app.get("/api/integrations")
def integrations(request: Request):
    require_admin(request)
    return {"items": list_integrations()}


@app.post("/api/integrations")
def add_integration(data: IntegrationRequest, request: Request):
    require_admin(request)
    try:
        item_id = save_integration(data.model_dump(exclude_none=True))
        return {"ok": True, "id": item_id}
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"保存连接失败：{error}") from error


@app.post("/api/documents/upload")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    storage: str = Form("local"),
    cloud_config_id: str | None = Form(None),
    access_scope: str = Form("public"),
    departments: str = Form(""),
):
    require_admin(request)
    original_name = Path(file.filename or "document").name
    safe_name = re.sub(r"[^\w.（）()\-\u4e00-\u9fff]", "_", original_name)
    extension = Path(safe_name).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="暂不支持该文件格式")
    target = engine.source_dir / safe_name
    target.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with target.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 50 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="单个文件不能超过 50MB")
                output.write(chunk)
        if storage == "cloud":
            config = s3_config(cloud_config_id)
            import boto3
            client = boto3.client(
                "s3",
                region_name=config.get("region") or None,
                endpoint_url=config.get("endpoint_url") or None,
                aws_access_key_id=config["access_key"],
                aws_secret_access_key=config["secret_key"],
            )
            key = (config.get("prefix", "knowledge-base").strip("/") + "/" + safe_name).lstrip("/")
            client.upload_file(str(target), config["bucket"], key)
        set_document_access(safe_name, access_scope, [value for value in departments.split(",") if value])
        engine.load(force=True)
        return {"ok": True, "file_name": safe_name, "size": size, "storage": storage}
    except HTTPException:
        if target.exists():
            target.unlink()
        raise
    except Exception as error:
        if target.exists() and size == 0:
            target.unlink()
        raise HTTPException(status_code=503, detail=f"上传失败：{error}") from error
    finally:
        await file.close()


@app.post("/api/ask")
def ask(data: AskRequest, request: Request):
    user = current_user(request)
    try:
        return engine.ask(data.question.strip(), user)
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"知识库暂时不可用：{error}") from error


@app.get("/api/admin/access")
def access_data(request: Request):
    require_admin(request)
    return list_access_data()


@app.post("/api/admin/departments")
def create_department(data: DepartmentRequest, request: Request):
    require_admin(request)
    try:
        add_department(data.name)
        return {"ok": True}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/admin/users")
def create_or_update_user(data: UserRequest, request: Request):
    require_admin(request)
    if data.role not in {"employee", "admin"}:
        raise HTTPException(status_code=400, detail="用户角色无效")
    try:
        save_user(data.model_dump())
        return {"ok": True}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _library_items(user):
    engine.load()
    grouped = {}
    for item in engine.documents:
        name = item["file_name"]
        if not can_access(name, user, item):
            continue
        file_path = str(item["file_path"])
        record = grouped.setdefault(file_path, {
            "document_id": hashlib.sha256(file_path.encode()).hexdigest()[:24],
            "file_name": name,
            "file_type": Path(name).suffix.lower().lstrip(".") or "在线文档",
            "version_label": item.get("version_label"),
            "document_kind": item.get("document_kind", "full"),
            "source_type": "online" if str(item.get("file_path", "")).startswith("online://") else "local",
            "locations": 0,
            "preview": "",
        })
        record["locations"] += 1
        if len(record["preview"]) < 1200:
            record["preview"] += ("\n" if record["preview"] else "") + item["text"][:600]
        if user.get("role") == "admin":
            acl = get_document_access(name, item)
            record["access_scope"] = acl["scope"]
            record["departments"] = acl.get("departments", [])
    return sorted(grouped.values(), key=lambda value: value["file_name"].lower())


@app.get("/api/library")
def library(request: Request):
    user = current_user(request)
    try:
        return {"items": _library_items(user), "is_admin": user.get("role") == "admin"}
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"资料库暂时不可用：{error}") from error


@app.post("/api/admin/documents/access")
def update_document_access(data: DocumentAccessRequest, request: Request):
    require_admin(request)
    if data.access_scope == "departments" and not data.departments:
        raise HTTPException(status_code=400, detail="请至少选择一个部门")
    known_names = {item["file_name"] for item in engine.documents} if engine.ready else set()
    if known_names and data.file_name not in known_names:
        raise HTTPException(status_code=404, detail="没有找到该资料")
    try:
        set_document_access(data.file_name, data.access_scope, data.departments)
        return {"ok": True}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/admin/documents/delete")
def delete_document(data: DeleteDocumentRequest, request: Request):
    require_admin(request)
    engine.load()
    sources = {}
    for item in engine.documents:
        path = str(item["file_path"])
        sources.setdefault(hashlib.sha256(path.encode()).hexdigest()[:24], item)
    item = sources.get(data.document_id)
    if not item:
        raise HTTPException(status_code=404, detail="没有找到该资料，可能已经被删除")
    file_path = str(item["file_path"])
    name = item["file_name"]
    if file_path.startswith("online://"):
        parts = file_path.split("/", 3)
        provider = parts[2] if len(parts) > 2 else ""
        if not delete_online_source(provider, name):
            raise HTTPException(status_code=404, detail="没有找到对应的在线资料连接")
    else:
        target = Path(file_path).resolve()
        root = engine.source_dir.resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise HTTPException(status_code=400, detail="资料文件路径无效")
        target.unlink()
    remove_document_access(name)
    try:
        engine.load(force=True)
    except RuntimeError:
        engine.clear()
    return {"ok": True, "file_name": name}
