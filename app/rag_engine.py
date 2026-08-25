import threading
import os
import time
from pathlib import Path

import faiss
import numpy as np
from openai import OpenAI
from sentence_transformers import CrossEncoder, SentenceTransformer

from .document_loader import filter_chunks_for_question, load_all_sources


def _is_bad_text(text):
    text = text.strip()
    return len(text) < 50 or text.count(".") > 40 or text.count("…") > 20


def _split_documents(documents, chunk_size=500):
    chunks = []
    for document in documents:
        current = ""
        for paragraph in document["text"].splitlines():
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            if len(current) + len(paragraph) <= chunk_size:
                current += paragraph + "\n"
            else:
                if current.strip() and not _is_bad_text(current):
                    chunks.append({**document, "text": current.strip()})
                current = paragraph + "\n"
        if current.strip() and not _is_bad_text(current):
            chunks.append({**document, "text": current.strip()})
    return chunks


class RagEngine:
    def __init__(self, source_dir, base_url, model, api_key="EMPTY"):
        self.source_dir = Path(source_dir)
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self._lock = threading.Lock()
        self.ready = False
        self.last_sync_at = 0.0
        self.refresh_seconds = max(30, int(os.getenv("ONLINE_REFRESH_SECONDS", "300")))
        self.sync_failures = []

    def load(self, force=False):
        with self._lock:
            now = time.time()
            if self.ready and not force and now - self.last_sync_at < self.refresh_seconds:
                return
            documents, failures = load_all_sources(self.source_dir)
            chunks = _split_documents(documents)
            if not chunks:
                raise RuntimeError("资料中没有生成可检索的文本块")
            for vector_id, chunk in enumerate(chunks):
                chunk["_vector_id"] = vector_id

            if not hasattr(self, "embedding"):
                self.embedding = SentenceTransformer("BAAI/bge-small-zh-v1.5")
                self.reranker = CrossEncoder("BAAI/bge-reranker-base")
                self.client = OpenAI(
                    base_url=self.base_url, api_key=self.api_key, timeout=120.0
                )
            vectors = np.asarray(
                self.embedding.encode(
                    [chunk["text"] for chunk in chunks],
                    normalize_embeddings=True,
                    show_progress_bar=False,
                ),
                dtype="float32",
            )

            # 所有新数据准备完毕后再原子替换，刷新失败不会破坏上一版索引。
            self.documents = documents
            self.chunks = chunks
            self.vectors = vectors
            self.sync_failures = failures
            self.last_sync_at = now
            self.ready = True

    def status(self):
        return {
            "ready": self.ready,
            "files": len({item["file_path"] for item in self.documents}) if self.ready else 0,
            "chunks": len(self.chunks) if self.ready else 0,
            "model": self.model,
            "last_sync_at": self.last_sync_at or None,
            "refresh_seconds": self.refresh_seconds,
            "sync_failures": self.sync_failures,
        }

    def ask(self, question):
        self.load()
        search_chunks, version_note = filter_chunks_for_question(self.chunks, question)
        if not search_chunks:
            return {"answer": "未在资料中找到足够信息。", "sources": [], "version_note": version_note}

        subset = self.vectors[[chunk["_vector_id"] for chunk in search_chunks]]
        index = faiss.IndexFlatIP(subset.shape[1])
        index.add(subset)
        query = "为这个句子生成表示以用于检索相关文章：" + question
        query_vector = np.asarray(
            self.embedding.encode([query], normalize_embeddings=True), dtype="float32"
        )
        scores, ids = index.search(query_vector, k=min(15, len(search_chunks)))
        candidates = []
        pairs = []
        for rank, chunk_id in enumerate(ids[0]):
            chunk = search_chunks[int(chunk_id)]
            candidates.append({**chunk, "faiss_score": float(scores[0][rank])})
            pairs.append([question, chunk["text"]])
        rerank_scores = self.reranker.predict(pairs)
        for item, score in zip(candidates, rerank_scores):
            item["rerank_score"] = float(score)
        candidates.sort(key=lambda item: item["rerank_score"], reverse=True)
        top_results = candidates[:3]
        # 给模型按时间从旧到新展示；同一条款冲突时，后出现的修订覆盖前文。
        top_results.sort(key=lambda item: item.get("effective_order", (0,)))
        context = "\n\n".join(
            f"[来源：{item['file_name']}，{item['location']}，"
            f"类型：{'增量修订' if item.get('document_kind') == 'amendment' else '完整版本'}]\n"
            f"{item['text']}"
            for item in top_results
        )
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "你是企业内部文档答疑助手。只能依据检索资料回答，不得猜测或编造。资料按生效顺序从旧到新排列；同一事项冲突时，后面的增量修订覆盖前面的完整版本或旧修订，未被后续修订的内容继续有效。资料不足，或只有修订文件而缺少必要基础版本时，只回答：未在资料中找到足够信息。使用简洁中文回答，不要编造来源。"},
                {"role": "user", "content": f"用户问题：\n{question}\n\n检索资料：\n{context}"},
            ],
            temperature=0.1,
            max_tokens=500,
        )
        answer = response.choices[0].message.content or "未在资料中找到足够信息。"
        sources = [
            {
                "file_name": item["file_name"],
                "location": item["location"],
                "excerpt": item["text"][:500],
                "version_label": item.get("version_label"),
                "document_kind": item.get("document_kind", "full"),
            }
            for item in top_results
        ]
        return {"answer": answer.strip(), "sources": sources, "version_note": version_note}
