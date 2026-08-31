import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


_lock = threading.RLock()


def _root(source_dir):
    configured = os.getenv("DOCUMENT_WORKFLOW_DIR")
    root = Path(configured) if configured else Path(source_dir).resolve().parent / ".document-workflow"
    root.mkdir(parents=True, exist_ok=True)
    return root


@contextmanager
def _database(source_dir):
    database = sqlite3.connect(_root(source_dir) / "workflow.sqlite3")
    try:
        database.row_factory = sqlite3.Row
        database.execute(
            """CREATE TABLE IF NOT EXISTS document_records (
                id TEXT PRIMARY KEY, file_name TEXT NOT NULL, status TEXT NOT NULL,
                stored_path TEXT NOT NULL, storage TEXT NOT NULL, cloud_config_id TEXT,
                access_scope TEXT NOT NULL, departments TEXT NOT NULL, size INTEGER NOT NULL,
                created_by TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                published_at REAL, recycled_at REAL, error TEXT
            )"""
        )
        database.execute("CREATE INDEX IF NOT EXISTS idx_document_status ON document_records(status, updated_at DESC)")
        yield database
        database.commit()
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def _as_dict(row):
    item = dict(row)
    item["departments"] = json.loads(item.get("departments") or "[]")
    item["stored_path"] = str(item.get("stored_path") or "")
    return item


def create_draft(source_dir, file_name, storage, cloud_config_id, access_scope, departments, size, username):
    record_id = uuid.uuid4().hex
    target = _root(source_dir) / "drafts" / f"{record_id}_{file_name}"
    target.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with _lock, _database(source_dir) as database:
        database.execute(
            "INSERT INTO document_records VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
            (record_id, file_name, str(target), storage, cloud_config_id, access_scope, json.dumps(departments, ensure_ascii=False), int(size), username, now, now),
        )
    return {"id": record_id, "stored_path": str(target), "status": "draft", "file_name": file_name}


def update_draft_size(source_dir, record_id, size):
    with _lock, _database(source_dir) as database:
        database.execute("UPDATE document_records SET size=?, updated_at=? WHERE id=? AND status='draft'", (int(size), time.time(), record_id))


def remove_draft(source_dir, record_id):
    with _lock, _database(source_dir) as database:
        row = database.execute("SELECT stored_path, status FROM document_records WHERE id=?", (record_id,)).fetchone()
        if not row or row["status"] not in {"draft", "publish_failed"}:
            return False
        path = Path(row["stored_path"])
        if path.is_file():
            path.unlink()
        database.execute("DELETE FROM document_records WHERE id=?", (record_id,))
    return True


def get_record(source_dir, record_id):
    with _database(source_dir) as database:
        row = database.execute("SELECT * FROM document_records WHERE id=?", (record_id,)).fetchone()
    return _as_dict(row) if row else None


def list_records(source_dir, statuses=None):
    with _database(source_dir) as database:
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            rows = database.execute(f"SELECT * FROM document_records WHERE status IN ({placeholders}) ORDER BY updated_at DESC", tuple(statuses)).fetchall()
        else:
            rows = database.execute("SELECT * FROM document_records ORDER BY updated_at DESC").fetchall()
    return [_as_dict(row) for row in rows]


def begin_publish(source_dir, record_id):
    with _lock, _database(source_dir) as database:
        row = database.execute("SELECT * FROM document_records WHERE id=?", (record_id,)).fetchone()
        if not row or row["status"] not in {"draft", "publish_failed"}:
            raise ValueError("草稿不存在或已经处理")
        record = _as_dict(row)
        source_root = Path(source_dir).resolve()
        source_root.mkdir(parents=True, exist_ok=True)
        target = source_root / record["file_name"]
        if target.exists():
            raise ValueError("资料库中已存在同名文件，请先修改文件名或回收旧版本")
        source = Path(record["stored_path"])
        if not source.is_file():
            raise ValueError("草稿文件不存在")
        shutil.move(str(source), str(target))
        now = time.time()
        database.execute("UPDATE document_records SET status='publishing', stored_path=?, updated_at=?, error=NULL WHERE id=?", (str(target), now, record_id))
    return {**record, "stored_path": str(target), "status": "publishing"}


def finish_publish(source_dir, record_id, error=None):
    now = time.time()
    with _lock, _database(source_dir) as database:
        if error:
            row = database.execute("SELECT file_name, stored_path FROM document_records WHERE id=?", (record_id,)).fetchone()
            stored_path = row["stored_path"] if row else ""
            if row:
                source = Path(row["stored_path"])
                draft = _root(source_dir) / "drafts" / f"{record_id}_{row['file_name']}"
                draft.parent.mkdir(parents=True, exist_ok=True)
                if source.is_file():
                    shutil.move(str(source), str(draft))
                stored_path = str(draft)
            database.execute("UPDATE document_records SET status='publish_failed', stored_path=?, updated_at=?, error=? WHERE id=?", (stored_path, now, str(error)[:1000], record_id))
        else:
            database.execute("UPDATE document_records SET status='published', updated_at=?, published_at=?, error=NULL WHERE id=?", (now, now, record_id))


def recycle_document(source_dir, file_name, file_path, access_scope, departments, username):
    source_root = Path(source_dir).resolve()
    target = Path(file_path).resolve()
    if not target.is_relative_to(source_root) or not target.is_file():
        raise ValueError("资料文件路径无效")
    size = target.stat().st_size
    with _lock, _database(source_dir) as database:
        row = database.execute("SELECT * FROM document_records WHERE file_name=? AND status IN ('published','publish_failed') ORDER BY updated_at DESC LIMIT 1", (file_name,)).fetchone()
        record_id = row["id"] if row else uuid.uuid4().hex
        trash = _root(source_dir) / "trash" / f"{record_id}_{file_name}"
        trash.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(trash))
        now = time.time()
        if row:
            database.execute("UPDATE document_records SET status='recycled', stored_path=?, updated_at=?, recycled_at=?, error=NULL WHERE id=?", (str(trash), now, now, record_id))
        else:
            database.execute(
                "INSERT INTO document_records VALUES (?, ?, 'recycled', ?, 'local', NULL, ?, ?, ?, ?, ?, ?, NULL, ?, NULL)",
                (record_id, file_name, str(trash), access_scope, json.dumps(departments, ensure_ascii=False), size, username, now, now, now),
            )
    return record_id


def restore_document(source_dir, record_id):
    with _lock, _database(source_dir) as database:
        row = database.execute("SELECT * FROM document_records WHERE id=?", (record_id,)).fetchone()
        if not row or row["status"] != "recycled":
            raise ValueError("回收站中没有该资料")
        record = _as_dict(row)
        source = Path(record["stored_path"])
        target = Path(source_dir).resolve() / record["file_name"]
        if target.exists():
            raise ValueError("资料库中已存在同名文件，不能恢复")
        if not source.is_file():
            raise ValueError("回收站文件不存在")
        shutil.move(str(source), str(target))
        now = time.time()
        database.execute("UPDATE document_records SET status='published', stored_path=?, updated_at=?, recycled_at=NULL, error=NULL WHERE id=?", (str(target), now, record_id))
    return {**record, "stored_path": str(target), "status": "published"}


def purge_document(source_dir, record_id):
    with _lock, _database(source_dir) as database:
        row = database.execute("SELECT * FROM document_records WHERE id=?", (record_id,)).fetchone()
        if not row or row["status"] not in {"draft", "recycled", "publish_failed"}:
            raise ValueError("只能永久删除草稿、发布失败或回收站资料")
        path = Path(row["stored_path"])
        if path.is_file():
            path.unlink()
        database.execute("DELETE FROM document_records WHERE id=?", (record_id,))
    return _as_dict(row)
