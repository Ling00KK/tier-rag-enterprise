import hashlib
import hmac
import logging
import os
import time
import re
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
import csv
import io
import difflib
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from .rag_engine import RagEngine
from .document_loader import SUPPORTED_EXTENSIONS
from .integration_store import delete_online_source, list_integrations, save_integration, s3_config
from .access_control import add_department, authenticate, can_access, get_active_user, get_document_access, list_access_data, remove_document_access, save_user, set_document_access
from .admin_store import add_evaluation_case, audit_status, delete_evaluation_case, evaluation_summary, feedback_summary, list_audit, list_evaluation_cases, list_evaluation_runs, log_event, save_evaluation_run, save_feedback, update_evaluation_case
from .document_workflow import begin_publish, create_draft, finish_publish, get_record, list_records, purge_document, recycle_document, remove_draft, restore_document, update_draft_size
from .model_store import activate_model_config, delete_model_config, list_model_configs, load_model_config, save_model_config, test_model_config
from .runtime_log import read_runtime_logs, runtime_logger
from .security import csv_safe
from .task_manager import get_task, list_tasks, submit_task


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


@app.middleware("http")
async def browser_security(request: Request, call_next):
    unsafe = request.method not in {"GET", "HEAD", "OPTIONS"}
    fetch_site = request.headers.get("sec-fetch-site", "").lower()
    origin = request.headers.get("origin")
    forwarded_scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    expected_origin = f"{forwarded_scheme}://{request.headers.get('host', request.url.netloc)}"
    allowed_origins = {expected_origin, *[value.strip() for value in os.getenv("ALLOWED_ORIGINS", "").split(",") if value.strip()]}
    if unsafe and (fetch_site == "cross-site" or (origin and origin not in allowed_origins)):
        response = JSONResponse({"detail": "已拒绝跨站请求"}, status_code=403)
    else:
        response = await call_next(request)
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.path == "/" or request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    if os.getenv("ENABLE_HSTS", "false").lower() == "true":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.middleware("http")
async def runtime_request_log(request: Request, call_next):
    started = time.perf_counter()
    path = request.url.path
    try:
        response = await call_next(request)
    except Exception:
        runtime_logger.exception(
            "请求处理异常",
            extra={
                "method": request.method,
                "path": path,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "client": request.client.host if request.client else "unknown",
            },
        )
        raise
    if not path.startswith("/static/") and path != "/api/admin/system-logs":
        level = logging.WARNING if response.status_code >= 400 else logging.INFO
        runtime_logger.log(
            level,
            "请求完成",
            extra={
                "method": request.method,
                "path": path,
                "status": response.status_code,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "client": request.client.host if request.client else "unknown",
            },
        )
    return response


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


class EvaluationCaseRequest(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    expected_file: str = Field(min_length=1, max_length=255)
    expected_terms: list[str] = Field(default_factory=list, max_length=20)
    forbidden_terms: list[str] = Field(default_factory=list, max_length=20)
    enabled: bool = True


class FeedbackRequest(BaseModel):
    answer_id: str = Field(min_length=16, max_length=64)
    question_hash: str = Field(min_length=64, max_length=64)
    helpful: bool
    comment: str = Field(default="", max_length=500)


class ModelConfigRequest(BaseModel):
    id: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=100)
    provider: str = Field(default="openai_compatible", max_length=50)
    base_url: str = Field(min_length=8, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    api_key: str | None = Field(default=None, max_length=500)
    timeout: int = Field(default=120, ge=5, le=300)


def require_login(request):
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=401, detail="请先登录")


def current_user(request):
    require_login(request)
    username = request.session.get("username")
    user = get_active_user(username, USERNAME) if username else None
    if not user:
        request.session.clear()
        raise HTTPException(status_code=401, detail="登录信息已更新，请重新登录")
    request.session["user"] = user
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
        log_event(data.username, "login", result="rate_limited")
        raise HTTPException(status_code=429, detail="尝试次数过多，请一分钟后重试")
    user = authenticate(data.username, data.password, USERNAME, PASSWORD_SALT, PASSWORD_HASH)
    if not user:
        attempts[address].append(now)
        log_event(data.username, "login", result="failed")
        raise HTTPException(status_code=401, detail="用户名或密码不正确")
    attempts.pop(address, None)
    request.session.clear()
    request.session["authenticated"] = True
    request.session["username"] = user["username"]
    request.session["user"] = user
    log_event(user["username"], "login", result="success")
    return {"ok": True, "user": user}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/status")
def status(request: Request):
    current_user(request)
    return engine.status()


@app.get("/api/health")
def health():
    state = engine.status()
    vector = state["vector_store"]
    return {"status": "healthy" if vector.get("healthy") else "degraded", "ready": state["ready"], "vector_store": vector.get("mode"), "model_provider": state["model_provider"]}


@app.get("/api/admin/health")
def admin_health(request: Request):
    require_admin(request)
    return engine.status()


@app.post("/api/sync")
def sync_knowledge_base(request: Request):
    user = require_admin(request)
    def work(progress):
        progress(10, "正在读取资料")
        engine.load(force=True)
        progress(90, "正在发布新索引")
        log_event(user["username"], "knowledge_sync", details=engine.status())
        return engine.status()
    task_id = submit_task("knowledge_sync", "全部资料", user["username"], work)
    return {"ok": True, "task_id": task_id, "status": "pending"}


@app.get("/api/integrations")
def integrations(request: Request):
    require_admin(request)
    return {"items": list_integrations()}


@app.post("/api/integrations")
def add_integration(data: IntegrationRequest, request: Request):
    user = require_admin(request)
    try:
        item_id = save_integration(data.model_dump(exclude_none=True))
        log_event(user["username"], "integration_save", data.name, details={"provider": data.provider, "access_scope": data.access_scope})
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
    user = require_admin(request)
    original_name = Path(file.filename or "document").name
    safe_name = re.sub(r"[^\w.（）()\-\u4e00-\u9fff]", "_", original_name)
    extension = Path(safe_name).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="暂不支持该文件格式")
    selected_departments = [value for value in departments.split(",") if value]
    draft = create_draft(engine.source_dir, safe_name, storage, cloud_config_id, access_scope, selected_departments, 0, user["username"])
    target = Path(draft["stored_path"])
    size = 0
    try:
        with target.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 50 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="单个文件不能超过 50MB")
                output.write(chunk)
        update_draft_size(engine.source_dir, draft["id"], size)
        log_event(user["username"], "document_draft", safe_name, details={"size": size, "storage": storage, "access_scope": access_scope})
        return {"ok": True, "record_id": draft["id"], "file_name": safe_name, "size": size, "storage": storage, "status": "draft"}
    except HTTPException:
        remove_draft(engine.source_dir, draft["id"])
        raise
    except Exception as error:
        remove_draft(engine.source_dir, draft["id"])
        raise HTTPException(status_code=503, detail=f"上传失败：{error}") from error
    finally:
        await file.close()


@app.post("/api/admin/documents/{record_id}/publish")
def publish_document(record_id: str, request: Request):
    user = require_admin(request)
    try:
        record = begin_publish(engine.source_dir, record_id)
        set_document_access(record["file_name"], record["access_scope"], record["departments"])
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    def work(progress):
        try:
            if record["storage"] == "cloud":
                progress(15, "正在上传云端资料库")
                config = s3_config(record.get("cloud_config_id"))
                import boto3
                client = boto3.client(
                    "s3",
                    region_name=config.get("region") or None,
                    endpoint_url=config.get("endpoint_url") or None,
                    aws_access_key_id=config["access_key"],
                    aws_secret_access_key=config["secret_key"],
                )
                key = (config.get("prefix", "knowledge-base").strip("/") + "/" + record["file_name"]).lstrip("/")
                client.upload_file(record["stored_path"], config["bucket"], key)
            progress(45, "正在解析文档并生成索引")
            engine.load(force=True)
            finish_publish(engine.source_dir, record_id)
            progress(95, "资料已经发布")
            log_event(user["username"], "document_publish", record["file_name"], details={"storage": record["storage"], "access_scope": record["access_scope"]})
            return {"record_id": record_id, "file_name": record["file_name"], **engine.status()}
        except Exception as error:
            finish_publish(engine.source_dir, record_id, error)
            log_event(user["username"], "document_publish", record["file_name"], result="failed", details={"error": str(error)})
            raise

    task_id = submit_task("document_publish", record["file_name"], user["username"], work)
    return {"ok": True, "task_id": task_id, "record_id": record_id}


@app.post("/api/admin/documents/{record_id}/restore")
def restore_recycled_document(record_id: str, request: Request):
    user = require_admin(request)
    try:
        record = restore_document(engine.source_dir, record_id)
        set_document_access(record["file_name"], record["access_scope"], record["departments"])
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    def work(progress):
        progress(30, "正在恢复资料索引")
        engine.load(force=True)
        progress(95, "资料已经恢复")
        log_event(user["username"], "document_restore", record["file_name"])
        return {"record_id": record_id, "file_name": record["file_name"], **engine.status()}

    task_id = submit_task("document_restore", record["file_name"], user["username"], work)
    return {"ok": True, "task_id": task_id}


@app.delete("/api/admin/documents/{record_id}/purge")
def permanently_delete_document(record_id: str, request: Request):
    user = require_admin(request)
    try:
        record = purge_document(engine.source_dir, record_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    remove_document_access(record["file_name"])
    log_event(user["username"], "document_purge", record["file_name"])
    return {"ok": True, "file_name": record["file_name"]}


@app.get("/api/admin/tasks")
def background_tasks(request: Request, limit: int = 100):
    require_admin(request)
    return {"items": list_tasks(limit)}


@app.get("/api/admin/tasks/{task_id}")
def background_task(task_id: str, request: Request):
    require_admin(request)
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="后台任务不存在")
    return task


@app.post("/api/ask")
def ask(data: AskRequest, request: Request):
    user = current_user(request)
    try:
        return engine.ask(data.question.strip(), user)
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"知识库暂时不可用：{error}") from error


@app.post("/api/feedback")
def answer_feedback(data: FeedbackRequest, request: Request):
    user = current_user(request)
    save_feedback(data.answer_id, user["username"], data.question_hash, data.helpful, data.comment)
    log_event(user["username"], "answer_feedback", result="helpful" if data.helpful else "unhelpful")
    return {"ok": True}


@app.get("/api/admin/access")
def access_data(request: Request):
    require_admin(request)
    return list_access_data()


@app.get("/api/admin/model")
def model_config(request: Request):
    require_admin(request)
    return load_model_config()


@app.get("/api/admin/models")
def model_configs(request: Request):
    require_admin(request)
    return {"items": list_model_configs()}


@app.post("/api/admin/model/test")
def test_model(data: ModelConfigRequest, request: Request):
    user = require_admin(request)
    try:
        result = test_model_config(data.model_dump(exclude_none=True))
        log_event(user["username"], "model_test", data.model)
        return result
    except Exception as error:
        log_event(user["username"], "model_test", data.model, result="failed", details={"error": str(error)})
        raise HTTPException(status_code=400, detail=f"模型连接测试失败：{error}") from error


@app.post("/api/admin/model")
def update_model(data: ModelConfigRequest, request: Request):
    user = require_admin(request)
    try:
        result = save_model_config(data.model_dump(exclude_none=True))
        engine._model_signature = None
        log_event(user["username"], "model_change", data.model, details={"provider": data.provider, "base_url": data.base_url})
        return {"ok": True, **result}
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"模型配置保存失败：{error}") from error


@app.post("/api/admin/models")
def create_or_update_model(data: ModelConfigRequest, request: Request):
    user = require_admin(request)
    try:
        result = save_model_config(data.model_dump(exclude_none=True), activate=False)
        log_event(user["username"], "model_save", data.model, details={"provider": data.provider, "base_url": data.base_url})
        return {"ok": True, **result}
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"模型配置保存失败：{error}") from error


@app.post("/api/admin/models/{model_id}/activate")
def activate_model(model_id: str, request: Request):
    user = require_admin(request)
    try:
        result = activate_model_config(model_id)
        engine._model_signature = None
        log_event(user["username"], "model_change", result["model"], details={"provider": result["provider"], "base_url": result["base_url"]})
        return {"ok": True, **result}
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.delete("/api/admin/models/{model_id}")
def delete_model(model_id: str, request: Request):
    user = require_admin(request)
    try:
        delete_model_config(model_id)
        log_event(user["username"], "model_delete", model_id)
        return {"ok": True}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/admin/departments")
def create_department(data: DepartmentRequest, request: Request):
    user = require_admin(request)
    try:
        add_department(data.name)
        log_event(user["username"], "department_save", data.name)
        return {"ok": True}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/admin/users")
def create_or_update_user(data: UserRequest, request: Request):
    user = require_admin(request)
    if data.role not in {"employee", "admin"}:
        raise HTTPException(status_code=400, detail="用户角色无效")
    try:
        save_user(data.model_dump())
        log_event(user["username"], "user_save", data.username, details={"role": data.role, "departments": data.departments, "enabled": data.enabled})
        return {"ok": True}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _library_items(user):
    try:
        engine.load()
    except RuntimeError:
        engine.clear()
    workflow = list_records(engine.source_dir)
    workflow_by_name = {item["file_name"]: item for item in workflow if item["status"] == "published"}
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
            "status": "published",
            "workflow_id": workflow_by_name.get(name, {}).get("id"),
        })
        record["locations"] += 1
        if len(record["preview"]) < 1200:
            record["preview"] += ("\n" if record["preview"] else "") + item["text"][:600]
        if user.get("role") == "admin":
            acl = get_document_access(name, item)
            record["access_scope"] = acl["scope"]
            record["departments"] = acl.get("departments", [])
    if user.get("role") == "admin":
        for item in workflow:
            if item["status"] == "published":
                continue
            grouped[f"workflow://{item['id']}"] = {
                "document_id": item["id"],
                "workflow_id": item["id"],
                "file_name": item["file_name"],
                "file_type": Path(item["file_name"]).suffix.lower().lstrip(".") or "文件",
                "version_label": None,
                "document_kind": "full",
                "source_type": "workflow",
                "locations": 0,
                "preview": item.get("error") or "尚未发布，不会进入员工检索结果。",
                "access_scope": item["access_scope"],
                "departments": item["departments"],
                "status": item["status"],
                "created_at": item["created_at"],
                "updated_at": item["updated_at"],
                "size": item["size"],
            }
    return sorted(grouped.values(), key=lambda value: (value.get("status") != "draft", value["file_name"].lower()))


@app.get("/api/library")
def library(request: Request):
    user = current_user(request)
    try:
        return {"items": _library_items(user), "is_admin": user.get("role") == "admin"}
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"资料库暂时不可用：{error}") from error


@app.post("/api/admin/documents/access")
def update_document_access(data: DocumentAccessRequest, request: Request):
    user = require_admin(request)
    if data.access_scope == "departments" and not data.departments:
        raise HTTPException(status_code=400, detail="请至少选择一个部门")
    known_names = {item["file_name"] for item in engine.documents} if engine.ready else set()
    if known_names and data.file_name not in known_names:
        raise HTTPException(status_code=404, detail="没有找到该资料")
    try:
        set_document_access(data.file_name, data.access_scope, data.departments)
        if engine.ready and engine.clickhouse.enabled:
            engine.load(force=True)
        log_event(user["username"], "document_access_change", data.file_name, details={"access_scope": data.access_scope, "departments": data.departments})
        return {"ok": True}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/admin/documents/delete")
def delete_document(data: DeleteDocumentRequest, request: Request):
    user = require_admin(request)
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
    recycled_id = None
    if file_path.startswith("online://"):
        parts = file_path.split("/", 3)
        provider = parts[2] if len(parts) > 2 else ""
        if not delete_online_source(provider, name):
            raise HTTPException(status_code=404, detail="没有找到对应的在线资料连接")
        remove_document_access(name)
    else:
        acl = get_document_access(name, item)
        try:
            recycled_id = recycle_document(engine.source_dir, name, file_path, acl["scope"], acl.get("departments", []), user["username"])
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    try:
        engine.load(force=True)
    except RuntimeError:
        engine.clear()
    event = "document_recycle" if recycled_id else "document_delete"
    log_event(user["username"], event, name)
    return {"ok": True, "file_name": name, "record_id": recycled_id, "status": "recycled" if recycled_id else "deleted"}


@app.get("/api/admin/dashboard")
def dashboard(request: Request):
    require_admin(request)
    if not engine.ready:
        try:
            engine.load()
        except Exception as error:
            log_event("system", "dashboard_load", result="failed", details={"error": str(error)})
    return {"retrieval": engine.metrics_summary(), "trend": engine.metrics_trend(), "failed_questions": engine.failed_questions(), "feedback": feedback_summary(), "evaluation": evaluation_summary(), "knowledge": engine.status(), "recent_audit": list_audit(8)}


@app.get("/api/admin/audit")
def audit(request: Request, limit: int = 100):
    require_admin(request)
    return {"items": list_audit(limit), "status": audit_status()}


@app.get("/api/admin/system-logs")
def system_logs(request: Request, limit: int = 200, level: str = "ALL"):
    require_admin(request)
    return {"items": read_runtime_logs(limit=limit, level=level)}


@app.get("/api/admin/audit/export")
def export_audit(request: Request):
    user = require_admin(request)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["时间戳", "用户", "事件", "对象", "结果", "详情"])
    for item in list_audit(500):
        writer.writerow([csv_safe(item["created_at"]), csv_safe(item["username"]), csv_safe(item["event_type"]), csv_safe(item["target"]), csv_safe(item["result"]), csv_safe(str(item["details"]))])
    log_event(user["username"], "audit_export")
    return StreamingResponse(iter(["\ufeff" + output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=audit.csv"})


@app.get("/api/admin/evaluations")
def evaluations(request: Request):
    require_admin(request)
    return {"items": list_evaluation_cases(), "summary": evaluation_summary(), "recent_runs": list_evaluation_runs(50)}


@app.post("/api/admin/evaluations")
def create_evaluation(data: EvaluationCaseRequest, request: Request):
    user = require_admin(request)
    case_id = add_evaluation_case(data.question, data.expected_file, data.expected_terms, data.forbidden_terms)
    log_event(user["username"], "evaluation_case_add", data.expected_file)
    return {"ok": True, "id": case_id}


@app.put("/api/admin/evaluations/{case_id}")
def edit_evaluation(case_id: str, data: EvaluationCaseRequest, request: Request):
    user = require_admin(request)
    if not update_evaluation_case(case_id, data.question, data.expected_file, data.enabled, data.expected_terms, data.forbidden_terms):
        raise HTTPException(status_code=404, detail="评测题不存在")
    log_event(user["username"], "evaluation_case_update", data.expected_file)
    return {"ok": True}


@app.delete("/api/admin/evaluations/{case_id}")
def remove_evaluation(case_id: str, request: Request):
    user = require_admin(request)
    if not delete_evaluation_case(case_id):
        raise HTTPException(status_code=404, detail="评测题不存在")
    log_event(user["username"], "evaluation_case_delete", case_id)
    return {"ok": True}


@app.get("/api/admin/versions/diff")
def version_diff(request: Request, first: str, second: str):
    require_admin(request)
    engine.load()
    def content(name):
        return "\n".join(item["text"] for item in engine.documents if item["file_name"] == name)
    before, after = content(first), content(second)
    if not before or not after:
        raise HTTPException(status_code=404, detail="找不到用于比较的文档")
    lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(), fromfile=first, tofile=second, lineterm=""))
    return {"first": first, "second": second, "changes": lines[:1000], "truncated": len(lines) > 1000}


@app.post("/api/admin/evaluations/run")
def run_evaluations(request: Request):
    user = require_admin(request)
    cases = [item for item in list_evaluation_cases() if item["enabled"]]

    def work(progress):
        results = []
        total = max(len(cases), 1)
        for index, case in enumerate(cases, 1):
            progress(int((index - 1) / total * 90), f"正在评测第 {index}/{len(cases)} 题")
            started = time.time()
            answer_result = engine.ask(case["question"], user)
            files = list(dict.fromkeys(item["file_name"] for item in answer_result.get("sources", [])))
            answer = answer_result.get("answer", "")
            expected = case["expected_file"].lower()
            failures = []
            if not any(expected in name.lower() for name in files):
                failures.append("未命中期望文件")
            for term in case.get("expected_terms", []):
                if term.lower() not in answer.lower():
                    failures.append(f"答案缺少：{term}")
            for term in case.get("forbidden_terms", []):
                if term.lower() in answer.lower():
                    failures.append(f"答案包含禁用内容：{term}")
            if not answer_result.get("grounded"):
                failures.append("答案未通过证据核验")
            passed = not failures
            latency = int((time.time() - started) * 1000)
            best_score = max([float(item.get("rerank_score", 0.0)) for item in answer_result.get("sources", [])] or [0.0])
            save_evaluation_run(case["id"], passed, files, best_score, latency, answer, failures)
            results.append({"case_id": case["id"], "question": case["question"], "expected_file": case["expected_file"], "matched_files": files, "answer": answer, "failures": failures, "passed": passed, "best_score": best_score, "latency_ms": latency})
        log_event(user["username"], "evaluation_run", details={"cases": len(results), "passed": sum(item["passed"] for item in results)})
        return {"items": results, "summary": evaluation_summary()}

    task_id = submit_task("evaluation_run", f"{len(cases)} 道评测题", user["username"], work)
    return {"ok": True, "task_id": task_id, "cases": len(cases)}
