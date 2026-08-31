import json
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from .runtime_log import runtime_logger


_executor = ThreadPoolExecutor(max_workers=max(1, int(os.getenv("BACKGROUND_WORKERS", "2"))), thread_name_prefix="tier-job")
_lock = threading.RLock()
_initialized = False


def _path():
    configured = os.getenv("TASK_DB_PATH")
    if configured:
        return Path(configured)
    config_dir = Path(os.getenv("INTEGRATIONS_CONFIG", "/data/config/integrations.json")).parent
    return config_dir / "tasks.sqlite3"


@contextmanager
def _database():
    global _initialized
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(path)
    try:
        database.row_factory = sqlite3.Row
        database.execute(
            """CREATE TABLE IF NOT EXISTS background_tasks (
                id TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                owner TEXT NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,
                status TEXT NOT NULL, progress INTEGER NOT NULL, message TEXT NOT NULL,
                result TEXT, error TEXT
            )"""
        )
        database.execute("CREATE INDEX IF NOT EXISTS idx_tasks_created ON background_tasks(created_at DESC)")
        if not _initialized:
            database.execute("UPDATE background_tasks SET status='interrupted', error='服务重启，任务已中断', updated_at=? WHERE status IN ('pending','running')", (time.time(),))
            _initialized = True
        yield database
        database.commit()
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def _update(task_id, **values):
    allowed = {"status", "progress", "message", "result", "error"}
    values = {key: value for key, value in values.items() if key in allowed}
    if "result" in values and not isinstance(values["result"], str):
        values["result"] = json.dumps(values["result"], ensure_ascii=False)
    values["updated_at"] = time.time()
    columns = ", ".join(f"{key}=?" for key in values)
    with _lock, _database() as database:
        database.execute(f"UPDATE background_tasks SET {columns} WHERE id=?", (*values.values(), task_id))


def submit_task(kind, target, owner, work):
    task_id = uuid.uuid4().hex
    now = time.time()
    with _lock, _database() as database:
        database.execute("INSERT INTO background_tasks VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, '等待处理', NULL, NULL)", (task_id, now, now, owner, kind, target))

    def progress(value, message):
        _update(task_id, progress=min(99, max(0, int(value))), message=str(message)[:500])

    def runner():
        _update(task_id, status="running", progress=1, message="开始处理")
        try:
            result = work(progress) or {}
            _update(task_id, status="succeeded", progress=100, message="处理完成", result=result, error=None)
        except Exception as error:
            runtime_logger.exception("后台任务失败", extra={"task_id": task_id, "kind": kind, "target": target})
            _update(task_id, status="failed", message="处理失败", error=str(error)[:1000])

    _executor.submit(runner)
    return task_id


def list_tasks(limit=100):
    with _database() as database:
        rows = database.execute("SELECT * FROM background_tasks ORDER BY created_at DESC LIMIT ?", (min(max(int(limit), 1), 500),)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["result"] = json.loads(item["result"]) if item.get("result") else None
        items.append(item)
    return items


def get_task(task_id):
    with _database() as database:
        row = database.execute("SELECT * FROM background_tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["result"] = json.loads(item["result"]) if item.get("result") else None
    return item
