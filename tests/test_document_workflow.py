from pathlib import Path

from app import document_workflow


def test_draft_publish_recycle_restore_and_purge(tmp_path, monkeypatch):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    monkeypatch.setenv("DOCUMENT_WORKFLOW_DIR", str(tmp_path / "workflow"))

    draft = document_workflow.create_draft(source_dir, "员工守则.txt", "local", None, "departments", ["人事部"], 4, "tier")
    Path(draft["stored_path"]).write_text("制度内容", encoding="utf-8")
    document_workflow.update_draft_size(source_dir, draft["id"], 12)
    assert document_workflow.get_record(source_dir, draft["id"])["status"] == "draft"

    publishing = document_workflow.begin_publish(source_dir, draft["id"])
    assert Path(publishing["stored_path"]).is_file()
    document_workflow.finish_publish(source_dir, draft["id"])
    assert document_workflow.get_record(source_dir, draft["id"])["status"] == "published"

    recycled_id = document_workflow.recycle_document(source_dir, "员工守则.txt", source_dir / "员工守则.txt", "departments", ["人事部"], "tier")
    assert recycled_id == draft["id"]
    assert not (source_dir / "员工守则.txt").exists()
    document_workflow.restore_document(source_dir, recycled_id)
    assert (source_dir / "员工守则.txt").is_file()

    document_workflow.recycle_document(source_dir, "员工守则.txt", source_dir / "员工守则.txt", "departments", ["人事部"], "tier")
    purged = document_workflow.purge_document(source_dir, recycled_id)
    assert purged["file_name"] == "员工守则.txt"
    assert document_workflow.get_record(source_dir, recycled_id) is None


def test_failed_publish_returns_file_to_staging(tmp_path, monkeypatch):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    monkeypatch.setenv("DOCUMENT_WORKFLOW_DIR", str(tmp_path / "workflow"))
    draft = document_workflow.create_draft(source_dir, "坏文件.txt", "local", None, "public", [], 1, "tier")
    Path(draft["stored_path"]).write_text("x", encoding="utf-8")
    document_workflow.begin_publish(source_dir, draft["id"])
    document_workflow.finish_publish(source_dir, draft["id"], "解析失败")
    record = document_workflow.get_record(source_dir, draft["id"])
    assert record["status"] == "publish_failed"
    assert Path(record["stored_path"]).is_file()
    assert not (source_dir / "坏文件.txt").exists()
