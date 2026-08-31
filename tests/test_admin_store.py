from app import admin_store
import pytest
from contextlib import contextmanager


def test_audit_and_evaluation_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.sqlite3"))
    admin_store.log_event("tier", "document_upload", "制度.pdf", details={"size": 10})
    audit = admin_store.list_audit()
    assert audit[0]["username"] == "tier"
    assert audit[0]["target"] == "制度.pdf"
    case_id = admin_store.add_evaluation_case("报销标准是什么？", "报销制度.pdf", ["100元"], ["500元"])
    assert admin_store.list_evaluation_cases()[0]["id"] == case_id
    assert admin_store.list_evaluation_cases()[0]["expected_terms"] == ["100元"]
    admin_store.save_evaluation_run(case_id, True, ["报销制度.pdf"], 0.99, 120, "标准为100元", [])
    assert admin_store.list_evaluation_runs()[0]["answer"] == "标准为100元"
    summary = admin_store.evaluation_summary()
    assert summary["runs"] == 1 and summary["pass_rate"] == 100.0
    assert admin_store.update_evaluation_case(case_id, "新问题", "新版.pdf", False, ["新标准"], [])
    assert admin_store.list_evaluation_cases()[0]["enabled"] == 0
    admin_store.save_feedback("answer-1234567890", "tier", "a" * 64, False, "没有回答到")
    feedback = admin_store.feedback_summary()
    assert feedback["total"] == 1 and feedback["helpful_rate"] == 0.0
    assert admin_store.delete_evaluation_case(case_id)


def test_database_context_closes_connection(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.sqlite3"))
    with admin_store._database() as database:
        database.execute("SELECT 1").fetchone()
    with pytest.raises(admin_store.sqlite3.ProgrammingError, match="closed"):
        database.execute("SELECT 1")


def test_audit_failure_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "blocked" / "admin.sqlite3"))

    @contextmanager
    def broken_database():
        raise OSError("disk is full")
        yield

    class FakeLogger:
        called = False
        def exception(self, *_args, **_kwargs):
            self.called = True

    logger = FakeLogger()
    monkeypatch.setattr(admin_store, "_database", broken_database)
    monkeypatch.setattr(admin_store, "runtime_logger", logger)
    assert admin_store.log_event("tier", "test") is False
    assert admin_store.audit_status()["healthy"] is False
    assert "disk is full" in admin_store.audit_status()["last_error"]
    assert logger.called


if __name__ == "__main__":
    from pathlib import Path
    from tempfile import TemporaryDirectory
    import os
    with TemporaryDirectory() as directory:
        os.environ["ADMIN_DB_PATH"] = str(Path(directory) / "admin.sqlite3")
        admin_store.log_event("tier", "test", "target")
        case_id = admin_store.add_evaluation_case("问题", "资料.pdf")
        admin_store.save_evaluation_run(case_id, True, ["资料.pdf"], 1.0, 10)
        assert admin_store.evaluation_summary()["pass_rate"] == 100.0
    print("admin-store tests passed")
