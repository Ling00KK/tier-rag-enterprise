import json

from app.runtime_log import read_runtime_logs


def test_read_runtime_logs_filters_and_returns_newest_first(tmp_path):
    path = tmp_path / "runtime.log"
    records = [
        {"timestamp": "2026-08-27T01:00:00+00:00", "level": "INFO", "message": "请求完成"},
        {"timestamp": "2026-08-27T01:01:00+00:00", "level": "WARNING", "message": "请求完成", "status": 401},
        {"timestamp": "2026-08-27T01:02:00+00:00", "level": "ERROR", "message": "请求处理异常"},
    ]
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in records), encoding="utf-8")

    assert [item["level"] for item in read_runtime_logs(limit=2, path=path)] == ["ERROR", "WARNING"]
    assert [item["level"] for item in read_runtime_logs(level="WARNING", path=path)] == ["WARNING"]


def test_read_runtime_logs_ignores_invalid_lines(tmp_path):
    path = tmp_path / "runtime.log"
    path.write_text("not-json\n" + json.dumps({"level": "INFO", "message": "ok"}), encoding="utf-8")
    assert read_runtime_logs(path=path) == [{"level": "INFO", "message": "ok"}]
