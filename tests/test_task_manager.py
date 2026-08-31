import time

from app import task_manager


def test_background_task_persists_progress_and_result(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "tasks.sqlite3"))

    def work(progress):
        progress(40, "处理中")
        return {"files": 3}

    task_id = task_manager.submit_task("test", "测试任务", "tier", work)
    for _ in range(100):
        task = task_manager.get_task(task_id)
        if task["status"] == "succeeded":
            break
        time.sleep(0.01)
    assert task["status"] == "succeeded"
    assert task["progress"] == 100
    assert task["result"] == {"files": 3}
    assert task_manager.list_tasks()[0]["id"] == task_id
