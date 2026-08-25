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
from .integration_store import list_integrations, save_integration, s3_config


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


def require_login(request):
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=401, detail="请先登录")


@app.get("/")
def home():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/session")
def session_status(request: Request):
    return {"authenticated": bool(request.session.get("authenticated"))}


@app.post("/api/login")
def login(data: LoginRequest, request: Request):
    address = request.client.host if request.client else "unknown"
    now = time.time()
    attempts[address] = [stamp for stamp in attempts[address] if now - stamp < 60]
    if len(attempts[address]) >= 5:
        raise HTTPException(status_code=429, detail="尝试次数过多，请一分钟后重试")
    digest = hashlib.pbkdf2_hmac("sha256", data.password.encode(), PASSWORD_SALT, 200_000).hex()
    if data.username != USERNAME or not hmac.compare_digest(digest, PASSWORD_HASH):
        attempts[address].append(now)
        raise HTTPException(status_code=401, detail="用户名或密码不正确")
    attempts.pop(address, None)
    request.session.clear()
    request.session["authenticated"] = True
    request.session["username"] = USERNAME
    return {"ok": True}


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
    require_login(request)
    try:
        engine.load(force=True)
        return {"ok": True, **engine.status()}
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"知识库同步失败：{error}") from error


@app.get("/api/integrations")
def integrations(request: Request):
    require_login(request)
    return {"items": list_integrations()}


@app.post("/api/integrations")
def add_integration(data: IntegrationRequest, request: Request):
    require_login(request)
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
):
    require_login(request)
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
    require_login(request)
    try:
        return engine.ask(data.question.strip())
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"知识库暂时不可用：{error}") from error
