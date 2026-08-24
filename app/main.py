import hashlib
import hmac
import os
import time
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from .rag_engine import RagEngine


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


@app.post("/api/ask")
def ask(data: AskRequest, request: Request):
    require_login(request)
    try:
        return engine.ask(data.question.strip())
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"知识库暂时不可用：{error}") from error
