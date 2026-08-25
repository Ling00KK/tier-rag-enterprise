import hashlib
import math
import os
import re
import sqlite3
import threading
import time
from collections import Counter
from pathlib import Path

import faiss
import numpy as np
from openai import OpenAI
from sentence_transformers import CrossEncoder, SentenceTransformer

from .access_control import can_access
from .document_loader import filter_chunks_for_question, load_all_sources


def _is_bad_text(text):
    text = text.strip()
    if len(text) < 8:
        return True
    return (text.count(".") + text.count("…")) > max(30, len(text) // 3)


def _split_documents(documents, chunk_size=600, overlap=100):
    """按段落组织切片，并保留少量前文，避免条款在边界处失去上下文。"""
    chunks = []
    for document in documents:
        paragraphs = [value.strip() for value in document["text"].splitlines() if value.strip()]
        current, current_length = [], 0
        for paragraph in paragraphs:
            if current and current_length + len(paragraph) > chunk_size:
                text = "\n".join(current).strip()
                if text and (not _is_bad_text(text) or "工作表" in document.get("location", "")):
                    chunks.append({**document, "text": text})
                tail = text[-overlap:]
                current = [tail, paragraph] if tail else [paragraph]
                current_length = sum(len(value) for value in current)
            else:
                current.append(paragraph)
                current_length += len(paragraph)
        text = "\n".join(current).strip()
        if text and (not _is_bad_text(text) or "工作表" in document.get("location", "")):
            chunks.append({**document, "text": text})
    return chunks


def _tokens(text):
    text = text.lower()
    tokens = re.findall(r"[a-z0-9_.-]+", text)
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    tokens.extend(chinese[index:index + 2] for index in range(max(0, len(chinese) - 1)))
    tokens.extend(chinese)
    return [value for value in tokens if value]


def _bm25(question, chunks):
    query_tokens = set(_tokens(question))
    if not query_tokens:
        return np.zeros(len(chunks), dtype="float32")
    tokenized = [_tokens(chunk["text"]) for chunk in chunks]
    average_length = max(1.0, sum(map(len, tokenized)) / max(1, len(tokenized)))
    document_frequency = Counter()
    for tokens in tokenized:
        document_frequency.update(set(tokens) & query_tokens)
    scores, total = [], len(tokenized)
    for tokens in tokenized:
        counts, score = Counter(tokens), 0.0
        for token in query_tokens:
            frequency = counts[token]
            if not frequency:
                continue
            inverse = math.log(1 + (total - document_frequency[token] + 0.5) / (document_frequency[token] + 0.5))
            score += inverse * frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * len(tokens) / average_length))
        scores.append(score)
    return np.asarray(scores, dtype="float32")


class RagEngine:
    def __init__(self, source_dir, base_url, model, api_key="EMPTY"):
        self.source_dir = Path(source_dir)
        self.base_url, self.model, self.api_key = base_url, model, api_key
        self._lock = threading.Lock()
        self.ready, self.last_sync_at = False, 0.0
        self.refresh_seconds = max(30, int(os.getenv("ONLINE_REFRESH_SECONDS", "300")))
        self.rerank_min_score = float(os.getenv("RERANK_MIN_SCORE", "0.05"))
        self.sync_failures, self.cache_hits, self.cache_misses = [], 0, 0

    def _cache_path(self):
        configured = os.getenv("VECTOR_CACHE_PATH")
        if configured:
            return Path(configured)
        config_dir = Path(os.getenv("INTEGRATIONS_CONFIG", "/data/config/integrations.json")).parent
        return config_dir / "embedding_cache.sqlite3"

    def _database(self):
        path = self._cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE IF NOT EXISTS embeddings (content_hash TEXT PRIMARY KEY, model TEXT NOT NULL, dimension INTEGER NOT NULL, vector BLOB NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS retrieval_events (created_at REAL, username TEXT, question_hash TEXT, permitted_chunks INTEGER, candidates INTEGER, best_score REAL, answered INTEGER, latency_ms INTEGER)")
        return connection

    def _encode_documents(self, chunks):
        model_name = "BAAI/bge-small-zh-v1.5"
        keys = [hashlib.sha256((model_name + "\0" + item["text"]).encode()).hexdigest() for item in chunks]
        cached = {}
        with self._database() as database:
            for start in range(0, len(keys), 500):
                batch = keys[start:start + 500]
                marks = ",".join("?" for _ in batch)
                rows = database.execute(f"SELECT content_hash, dimension, vector FROM embeddings WHERE content_hash IN ({marks})", batch).fetchall()
                cached.update({key: np.frombuffer(blob, dtype="float32", count=dimension).copy() for key, dimension, blob in rows})
            missing_positions = [index for index, key in enumerate(keys) if key not in cached]
            if missing_positions:
                encoded = np.asarray(self.embedding.encode([chunks[index]["text"] for index in missing_positions], normalize_embeddings=True, show_progress_bar=False), dtype="float32")
                database.executemany("INSERT OR REPLACE INTO embeddings(content_hash, model, dimension, vector) VALUES (?, ?, ?, ?)", [(keys[position], model_name, len(vector), vector.tobytes()) for position, vector in zip(missing_positions, encoded)])
                cached.update({keys[position]: vector for position, vector in zip(missing_positions, encoded)})
            self.cache_hits, self.cache_misses = len(keys) - len(missing_positions), len(missing_positions)
        return np.asarray([cached[key] for key in keys], dtype="float32")

    def _record_metric(self, user, question, permitted, candidates, best_score, answered, started):
        try:
            with self._database() as database:
                database.execute("INSERT INTO retrieval_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (time.time(), user.get("username", ""), hashlib.sha256(question.encode()).hexdigest(), permitted, candidates, best_score, int(answered), int((time.time() - started) * 1000)))
        except Exception:
            pass

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
                self.client = OpenAI(base_url=self.base_url, api_key=self.api_key, timeout=120.0)
            vectors = self._encode_documents(chunks)
            self.documents, self.chunks, self.vectors = documents, chunks, vectors
            self.sync_failures, self.last_sync_at, self.ready = failures, now, True

    def status(self):
        return {"ready": self.ready, "files": len({item["file_path"] for item in self.documents}) if self.ready else 0, "chunks": len(self.chunks) if self.ready else 0, "model": self.model, "last_sync_at": self.last_sync_at or None, "refresh_seconds": self.refresh_seconds, "sync_failures": self.sync_failures, "retrieval": "hybrid_bm25_vector_rerank", "vector_cache_hits": self.cache_hits, "vector_cache_misses": self.cache_misses}

    def metrics_summary(self, days=30):
        since = time.time() - days * 86400
        with self._database() as database:
            row = database.execute("SELECT COUNT(*) questions, COALESCE(SUM(answered), 0) answered, COALESCE(AVG(latency_ms), 0) latency, COALESCE(AVG(best_score), 0) score FROM retrieval_events WHERE created_at >= ?", (since,)).fetchone()
            users = database.execute("SELECT COUNT(DISTINCT username) FROM retrieval_events WHERE created_at >= ?", (since,)).fetchone()[0]
        questions = row[0]
        return {"questions": questions, "answered": row[1], "answer_rate": round(row[1] / questions * 100, 1) if questions else None, "average_latency_ms": round(row[2]), "average_best_score": round(row[3], 4), "active_users": users, "days": days}

    def clear(self):
        with self._lock:
            self.documents, self.chunks = [], []
            self.vectors = np.empty((0, 0), dtype="float32")
            self.ready, self.last_sync_at = False, 0.0

    def retrieve(self, question, user):
        self.load()
        permitted = [chunk for chunk in self.chunks if can_access(chunk["file_name"], user, chunk)]
        search_chunks, version_note = filter_chunks_for_question(permitted, question)
        if not search_chunks:
            return {"results": [], "version_note": version_note, "permitted_chunks": 0, "best_score": 0.0}
        subset = self.vectors[[chunk["_vector_id"] for chunk in search_chunks]]
        index = faiss.IndexFlatIP(subset.shape[1])
        index.add(subset)
        query_vector = np.asarray(self.embedding.encode(["为这个句子生成表示以用于检索相关文章：" + question], normalize_embeddings=True), dtype="float32")
        recall_size = min(30, len(search_chunks))
        vector_scores, vector_ids = index.search(query_vector, k=recall_size)
        lexical_scores = _bm25(question, search_chunks)
        lexical_ids = np.argsort(-lexical_scores)[:recall_size]
        fused, vector_lookup = {}, {}
        for rank, chunk_id in enumerate(vector_ids[0]):
            fused[int(chunk_id)] = fused.get(int(chunk_id), 0) + 1 / (61 + rank)
            vector_lookup[int(chunk_id)] = float(vector_scores[0][rank])
        for rank, chunk_id in enumerate(lexical_ids):
            if lexical_scores[int(chunk_id)] <= 0:
                continue
            fused[int(chunk_id)] = fused.get(int(chunk_id), 0) + 1 / (61 + rank)
        candidate_ids = [item[0] for item in sorted(fused.items(), key=lambda value: value[1], reverse=True)[:30]]
        candidates = [{**search_chunks[chunk_id], "faiss_score": vector_lookup.get(chunk_id, 0.0), "bm25_score": float(lexical_scores[chunk_id]), "fusion_score": fused[chunk_id]} for chunk_id in candidate_ids]
        rerank_scores = self.reranker.predict([[question, item["text"]] for item in candidates])
        for item, score in zip(candidates, rerank_scores):
            item["rerank_score"] = float(score)
        candidates.sort(key=lambda item: item["rerank_score"], reverse=True)
        best_score = candidates[0]["rerank_score"] if candidates else 0.0
        if not candidates or best_score < self.rerank_min_score:
            return {"results": [], "version_note": version_note + "；相关度不足，已停止生成", "permitted_chunks": len(search_chunks), "best_score": best_score}
        return {"results": candidates[:3], "version_note": version_note, "permitted_chunks": len(search_chunks), "best_score": best_score, "candidate_count": len(candidates)}

    def ask(self, question, user):
        started = time.time()
        retrieval = self.retrieve(question, user)
        top_results = retrieval["results"]
        if not top_results:
            self._record_metric(user, question, retrieval["permitted_chunks"], 0, retrieval["best_score"], False, started)
            return {"answer": "未在资料中找到足够信息。", "sources": [], "version_note": retrieval["version_note"]}
        top_results.sort(key=lambda item: item.get("effective_order", (0,)))
        context = "\n\n".join(f"[来源：{item['file_name']}，{item['location']}，类型：{'增量修订' if item.get('document_kind') == 'amendment' else '完整版本'}]\n{item['text']}" for item in top_results)
        response = self.client.chat.completions.create(model=self.model, messages=[{"role": "system", "content": "你是企业内部文档答疑助手。只能依据检索资料回答，不得猜测或编造。资料按生效顺序从旧到新排列；同一事项冲突时，后面的增量修订覆盖前面的完整版本或旧修订，未被后续修订的内容继续有效。资料不足时只回答：未在资料中找到足够信息。使用简洁中文回答，不要编造来源。"}, {"role": "user", "content": f"用户问题：\n{question}\n\n检索资料：\n{context}"}], temperature=0.1, max_tokens=500)
        answer = (response.choices[0].message.content or "未在资料中找到足够信息。").strip()
        sources = [{"file_name": item["file_name"], "location": item["location"], "excerpt": item["text"][:500], "version_label": item.get("version_label"), "document_kind": item.get("document_kind", "full")} for item in top_results]
        self._record_metric(user, question, retrieval["permitted_chunks"], retrieval["candidate_count"], retrieval["best_score"], answer != "未在资料中找到足够信息。", started)
        return {"answer": answer, "sources": sources, "version_note": retrieval["version_note"]}
