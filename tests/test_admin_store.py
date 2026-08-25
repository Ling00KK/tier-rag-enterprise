from app import admin_store


def test_audit_and_evaluation_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.sqlite3"))
    admin_store.log_event("tier", "document_upload", "制度.pdf", details={"size": 10})
    audit = admin_store.list_audit()
    assert audit[0]["username"] == "tier"
    assert audit[0]["target"] == "制度.pdf"
    case_id = admin_store.add_evaluation_case("报销标准是什么？", "报销制度.pdf")
    assert admin_store.list_evaluation_cases()[0]["id"] == case_id
    admin_store.save_evaluation_run(case_id, True, ["报销制度.pdf"], 0.99, 120)
    summary = admin_store.evaluation_summary()
    assert summary["runs"] == 1 and summary["pass_rate"] == 100.0
    assert admin_store.update_evaluation_case(case_id, "新问题", "新版.pdf", False)
    assert admin_store.list_evaluation_cases()[0]["enabled"] == 0
    admin_store.save_feedback("answer-1234567890", "tier", "a" * 64, False, "没有回答到")
    feedback = admin_store.feedback_summary()
    assert feedback["total"] == 1 and feedback["helpful_rate"] == 0.0
    assert admin_store.delete_evaluation_case(case_id)


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
