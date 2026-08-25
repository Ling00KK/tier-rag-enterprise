import numpy as np

from app.rag_engine import RagEngine, _bm25, _split_documents

def document(text, location="第 1 页"):
    return {"file_name": "制度.pdf", "file_path": "制度.pdf", "location": location, "text": text}

def test_bm25_prioritizes_exact_business_terms():
    chunks = [document("员工年度休假和请假制度"), document("QMT 股票批量埋单交易功能")]
    scores = _bm25("QMT批量埋单", chunks)
    assert scores[1] > scores[0]

def test_excel_short_rows_are_not_dropped():
    chunks = _split_documents([document("姓名 | 金额\n张三 | 10", "工作表 报销 第 2 行")])
    assert chunks and "张三" in chunks[0]["text"]

def test_chunks_keep_overlap_context():
    text = "第一章 总则\n" + "A" * 580 + "\n第二条 具体规定"
    chunks = _split_documents([document(text)], chunk_size=300, overlap=30)
    assert len(chunks) >= 2
    assert "A" * 20 in chunks[-1]["text"]

class FakeEmbedding:
    def __init__(self): self.calls = 0
    def encode(self, texts, **_):
        self.calls += len(texts)
        return np.asarray([[len(text), 1.0] for text in texts], dtype="float32")

def test_embedding_cache_reuses_unchanged_chunks(tmp_path, monkeypatch):
    monkeypatch.setenv("VECTOR_CACHE_PATH", str(tmp_path / "vectors.sqlite3"))
    engine = RagEngine(tmp_path, "http://example/v1", "model")
    engine.embedding = FakeEmbedding()
    chunks = [document("一段不会变化的知识内容")]
    first, second = engine._encode_documents(chunks), engine._encode_documents(chunks)
    assert np.array_equal(first, second)
    assert engine.embedding.calls == 1 and engine.cache_hits == 1

if __name__ == "__main__":
    from tempfile import TemporaryDirectory
    from pathlib import Path
    import os
    test_bm25_prioritizes_exact_business_terms()
    test_excel_short_rows_are_not_dropped()
    test_chunks_keep_overlap_context()
    with TemporaryDirectory() as directory:
        os.environ["VECTOR_CACHE_PATH"] = str(Path(directory) / "vectors.sqlite3")
        engine = RagEngine(directory, "http://example/v1", "model")
        engine.embedding = FakeEmbedding()
        chunks = [document("一段不会变化的知识内容")]
        engine._encode_documents(chunks); engine._encode_documents(chunks)
        assert engine.embedding.calls == 1
    print("retrieval-quality tests passed")
