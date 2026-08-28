"""在线文档连接器。

配置文件默认位于 SOURCE_DIR/online_sources.json。所有密钥只从环境变量读取，
配置文件中仅保存环境变量名，避免把 access token 提交到代码仓库。
"""

import hashlib
import hmac
import json
import os
from email.utils import formatdate
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .integration_store import online_sources as managed_online_sources
from .security import validate_outbound_url


def _secret(source, key, required=True):
    managed_name = key.removesuffix("_env")
    managed_value = source.get("_secrets", {}).get(managed_name, "")
    if managed_value:
        return managed_value
    direct_value = source.get(managed_name, "")
    if direct_value:
        return direct_value
    env_name = source.get(key)
    value = os.getenv(env_name, "") if env_name else ""
    if required and not value:
        raise RuntimeError(f"缺少环境变量：{env_name or key}")
    return value


class _SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, new_url):
        validate_outbound_url(new_url)
        return super().redirect_request(request, fp, code, msg, headers, new_url)


def _request(url, headers, timeout=30):
    validate_outbound_url(url)
    request = Request(url, headers=headers, method="GET")
    try:
        with build_opener(_SafeRedirectHandler()).open(request, timeout=timeout) as response:
            return response.read(), response.headers.get_content_type()
    except HTTPError as error:
        details = error.read(500).decode("utf-8", errors="replace")
        raise RuntimeError(f"在线文档接口返回 HTTP {error.code}：{details}") from error
    except URLError as error:
        raise RuntimeError(f"无法连接在线文档接口：{error.reason}") from error


def _wps_headers(source, request_uri):
    access_token = _secret(source, "access_token_env")
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    # WPS 后台关闭“接口签名”时只需要 Bearer Token；开启后自动生成 KSO-1。
    app_id = _secret(source, "app_id_env", required=False)
    app_key = _secret(source, "app_key_env", required=False)
    if bool(app_id) != bool(app_key):
        raise RuntimeError("WPS APPID 与 APPKEY 必须同时配置")
    if app_id:
        kso_date = formatdate(usegmt=True)
        content = "KSO-1" + "GET" + request_uri + "application/json" + kso_date
        signature = hmac.new(app_key.encode(), content.encode(), hashlib.sha256).hexdigest()
        headers["X-Kso-Date"] = kso_date
        headers["X-Kso-Authorization"] = f"KSO-1 {app_id}:{signature}"
    return headers


def _load_wps(source):
    drive_id = source.get("drive_id")
    file_id = source.get("file_id")
    if not drive_id or not file_id:
        raise RuntimeError("WPS 在线文档需要 drive_id 和 file_id")
    output_format = source.get("format", "markdown")
    query = urlencode({"format": output_format, "mode": "sync"})
    request_uri = f"/v7/drives/{drive_id}/files/{file_id}/content?{query}"
    base_url = os.getenv("WPS_API_BASE_URL", "https://openapi.wps.cn").rstrip("/")
    body, _ = _request(base_url + request_uri, _wps_headers(source, request_uri))
    payload = json.loads(body.decode("utf-8"))
    if payload.get("code", 0) != 0:
        raise RuntimeError(f"WPS 接口错误：{payload.get('msg', payload)}")
    data = payload.get("data", payload)
    if isinstance(data, str):
        return data
    for key in ("content", "text", "markdown", "result"):
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, str):
            return value
    raise RuntimeError("WPS 返回结果中没有可识别的文档内容")


def _load_tencent(source):
    """调用腾讯文档开放合作平台提供的内容/导出 URL。

    不硬编码已变更或仅向合作企业开放的端点；endpoint 可使用配置中的完整 URL，
    或相对于 TENCENT_DOCS_API_BASE_URL 的路径。
    """
    endpoint = source.get("endpoint")
    if not endpoint:
        raise RuntimeError("腾讯文档需要填写开放平台提供的 endpoint")
    if endpoint.startswith("https://"):
        url = endpoint
    else:
        base = os.getenv("TENCENT_DOCS_API_BASE_URL", "").rstrip("/")
        if not base:
            raise RuntimeError("缺少 TENCENT_DOCS_API_BASE_URL")
        url = base + "/" + endpoint.lstrip("/")

    headers = {"Accept": source.get("accept", "application/json")}
    token = _secret(source, "access_token_env")
    headers["Authorization"] = f"Bearer {token}"
    optional_headers = {
        "Client-Id": _secret(source, "client_id_env", required=False),
        "Open-Id": _secret(source, "open_id_env", required=False),
    }
    headers.update({key: value for key, value in optional_headers.items() if value})
    body, content_type = _request(url, headers)

    if content_type in {"application/json", "text/json"}:
        payload = json.loads(body.decode("utf-8"))
        data = payload.get("data", payload)
        selector = source.get("content_field", "content")
        value = data
        for part in selector.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if isinstance(value, str):
            return value
        raise RuntimeError(f"腾讯文档返回结果中找不到文本字段：{selector}")
    return body.decode(source.get("encoding", "utf-8"), errors="replace")


def _load_http(source):
    url = source.get("url")
    if not url or not url.startswith("https://"):
        raise RuntimeError("通用在线来源必须使用 HTTPS URL")
    headers = {"Accept": source.get("accept", "text/plain, text/markdown, application/json")}
    token = _secret(source, "access_token_env", required=False)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body, content_type = _request(url, headers)
    if content_type in {"application/json", "text/json"}:
        payload = json.loads(body.decode("utf-8"))
        selector = source.get("content_field", "content")
        value = payload
        for part in selector.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if not isinstance(value, str):
            raise RuntimeError(f"在线来源中找不到文本字段：{selector}")
        return value
    return body.decode(source.get("encoding", "utf-8"), errors="replace")


def load_online_sources(config_path):
    config_path = Path(config_path)
    sources = managed_online_sources()
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        sources.extend(config.get("sources", []))
    items, failures = [], []
    loaders = {
        "wps": _load_wps,
        "kingsoft": _load_wps,
        "jinshan": _load_wps,
        "tencent_docs": _load_tencent,
        "http": _load_http,
    }
    for index, source in enumerate(sources, start=1):
        if source.get("enabled", True) is False:
            continue
        name = source.get("name") or f"在线文档_{index}"
        provider = source.get("provider", "http").lower()
        try:
            text = loaders[provider](source).strip()
            if text:
                items.append({
                    "file_name": name,
                    "file_path": f"online://{provider}/{name}",
                    "location": source.get("location", "在线文档全文"),
                    "text": text,
                    "source_url": source.get("url") or source.get("endpoint"),
                    "online_provider": provider,
                    "access_scope": source.get("access_scope", "public"),
                    "departments": source.get("departments", []),
                })
        except Exception as error:
            failures.append(f"{name}：{error}")
    return items, failures
