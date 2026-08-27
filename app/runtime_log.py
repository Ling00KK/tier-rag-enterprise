import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
LOG_PATH = Path(os.getenv("SYSTEM_LOG_PATH", BASE_DIR / "config" / "runtime.log"))
MAX_BYTES = int(os.getenv("SYSTEM_LOG_MAX_BYTES", str(5 * 1024 * 1024)))
BACKUP_COUNT = int(os.getenv("SYSTEM_LOG_BACKUP_COUNT", "5"))


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        for key in ("method", "path", "status", "latency_ms", "client"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info).splitlines()[-1][:1000]
        return json.dumps(payload, ensure_ascii=False)


def _build_logger():
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("tier_rag.runtime")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(LOG_PATH, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    return logger


runtime_logger = _build_logger()


def read_runtime_logs(limit=200, level="ALL", path=None):
    log_path = Path(path) if path else LOG_PATH
    limit = max(1, min(int(limit), 1000))
    requested_level = str(level or "ALL").upper()
    if requested_level not in {"ALL", "INFO", "WARNING", "ERROR"}:
        requested_level = "ALL"
    if not log_path.exists():
        return []
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    items = []
    for line in reversed(lines):
        try:
            item = json.loads(line)
        except (TypeError, ValueError):
            continue
        if requested_level != "ALL" and item.get("level") != requested_level:
            continue
        items.append(item)
        if len(items) >= limit:
            break
    return items
