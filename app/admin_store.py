import json
import os
import sqlite3
import time
import uuid
from pathlib import Path


def _path():
    configured = os.getenv("ADMIN_DB_PATH")
    if configured:
        return Path(configured)
    config_dir = Path(os.getenv("INTEGRATIONS_CONFIG", "/data/config/integrations.json")).parent
    return config_dir / "admin.sqlite3"


def _database():
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(path)
    database.row_factory = sqlite3.Row
    database.execute("CREATE TABLE IF NOT EXISTS audit_events (id TEXT PRIMARY KEY, created_at REAL NOT NULL, username TEXT NOT NULL, event_type TEXT NOT NULL, target TEXT, result TEXT NOT NULL, details TEXT)")
    database.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at DESC)")
    database.execute("CREATE TABLE IF NOT EXISTS evaluation_cases (id TEXT PRIMARY KEY, created_at REAL NOT NULL, question TEXT NOT NULL, expected_file TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1)")
    database.execute("CREATE TABLE IF NOT EXISTS evaluation_runs (id TEXT PRIMARY KEY, created_at REAL NOT NULL, case_id TEXT NOT NULL, passed INTEGER NOT NULL, matched_files TEXT NOT NULL, best_score REAL NOT NULL, latency_ms INTEGER NOT NULL)")
    return database


def log_event(username, event_type, target="", result="success", details=None):
    try:
        with _database() as database:
            database.execute("INSERT INTO audit_events VALUES (?, ?, ?, ?, ?, ?, ?)", (uuid.uuid4().hex, time.time(), username or "anonymous", event_type, target[:300], result, json.dumps(details or {}, ensure_ascii=False)))
    except Exception:
        pass


def list_audit(limit=100):
    with _database() as database:
        rows = database.execute("SELECT created_at, username, event_type, target, result, details FROM audit_events ORDER BY created_at DESC LIMIT ?", (min(max(limit, 1), 500),)).fetchall()
    return [{**dict(row), "details": json.loads(row["details"] or "{}") } for row in rows]


def add_evaluation_case(question, expected_file):
    case_id = uuid.uuid4().hex
    with _database() as database:
        database.execute("INSERT INTO evaluation_cases VALUES (?, ?, ?, ?, 1)", (case_id, time.time(), question.strip(), expected_file.strip()))
    return case_id


def list_evaluation_cases():
    with _database() as database:
        rows = database.execute("SELECT id, created_at, question, expected_file, enabled FROM evaluation_cases ORDER BY created_at DESC").fetchall()
    return [dict(row) for row in rows]


def save_evaluation_run(case_id, passed, matched_files, best_score, latency_ms):
    with _database() as database:
        database.execute("INSERT INTO evaluation_runs VALUES (?, ?, ?, ?, ?, ?, ?)", (uuid.uuid4().hex, time.time(), case_id, int(passed), json.dumps(matched_files, ensure_ascii=False), float(best_score), int(latency_ms)))


def evaluation_summary():
    with _database() as database:
        row = database.execute("SELECT COUNT(*) total, COALESCE(SUM(passed), 0) passed, COALESCE(AVG(latency_ms), 0) latency FROM evaluation_runs").fetchone()
    total = row["total"]
    return {"runs": total, "passed": row["passed"], "pass_rate": round(row["passed"] / total * 100, 1) if total else None, "average_latency_ms": round(row["latency"])}
