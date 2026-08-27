import os
import numpy as np

from app.clickhouse_store import ClickHouseStore


class Result:
    result_rows = [("chunk-a", 0.91)]


class FakeClient:
    def __init__(self): self.commands, self.inserted, self.parameters = [], [], None
    def command(self, sql): self.commands.append(sql); return "26.3.1"
    def insert(self, table, rows, column_names): self.inserted = rows
    def query(self, sql, parameters): self.parameters = parameters; return Result()


def test_publish_and_permission_query(monkeypatch):
    monkeypatch.setenv("CLICKHOUSE_ENABLED", "true")
    store = ClickHouseStore(); store.client = FakeClient()
    chunk = {"_chunk_id": "chunk-a", "_document_id": "doc-a", "file_name": "制度.txt", "file_path": "/source/制度.txt", "location": "文本", "text": "制度内容", "document_kind": "full"}
    store.publish([chunk], np.zeros((1, 512), dtype="float32"), lambda *_: {"scope": "departments", "departments": ["人事部"]})
    result = store.search(np.zeros(512, dtype="float32"), {"role": "employee", "departments": ["人事部"]})
    assert result[0]["chunk_id"] == "chunk-a"
    assert store.client.parameters["is_admin"] == 0
    assert store.client.parameters["departments"] == ["人事部"]


if __name__ == "__main__":
    class Monkey:
        @staticmethod
        def setenv(key, value): os.environ[key] = value
    test_publish_and_permission_query(Monkey())
    print("clickhouse-store tests passed")
