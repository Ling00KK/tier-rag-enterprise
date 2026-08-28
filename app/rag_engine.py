import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

import numpy as np
from openai import OpenAI

from .access_control import can_access
from .access_control import get_document_access
from .clickhouse_store import ClickHouseStore
from .document_loader import filter_chunks_for_question, load_all_sources
from .model_store import load_model_config


INSUFFICIENT_ANSWER = "未在资料中找到足够信息。"
DEFAULT_SYNONYMS = {
    "考勤": ["打卡", "签到", "漏打卡", "没打卡", "补卡", "考勤异常"],
    "薪酬": ["工资", "薪资", "薪酬", "发工资"],
    "休假": ["请假", "年假", "调休", "休假", "假期"],
    "离职": ["辞职", "离职", "解除劳动关系"],
    "报销": ["报销", "费用报销", "垫付款", "费用申请"],
    "缺勤": ["没来上班", "未到岗", "缺勤", "旷工"],
}


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


def _load_synonyms(path=None):
    groups = dict(DEFAULT_SYNONYMS)
    configured = Path(path) if path else Path(__file__).resolve().parents[1] / "config" / "query_synonyms.json"
    if not configured.exists():
        return groups
    try:
        custom = json.loads(configured.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return groups
    if isinstance(custom, dict):
        for name, values in custom.items():
            if isinstance(values, list):
                cleaned = [str(value).strip() for value in values if str(value).strip()]
                groups[str(name).strip()] = cleaned
    return groups


def _expand_with_synonyms(question, groups, limit=4):
    variants = [question.strip()]
    matched = []
    lowered = question.lower()
    for name, values in groups.items():
        terms = list(dict.fromkeys([name, *values]))
        if any(term.lower() in lowered for term in terms):
            matched.extend(terms)
            variants.append(f"{question.strip()} {' '.join(terms)}")
    if matched:
        variants.append(" ".join(dict.fromkeys(matched)))
    return list(dict.fromkeys(value[:300] for value in variants if value.strip()))[:limit]


def _parse_query_rewrites(content, limit=3):
    text = (content or "").strip().replace("```json", "").replace("```", "").strip()
    values = []
    try:
        parsed = json.loads(text)
        values = parsed.get("queries", []) if isinstance(parsed, dict) else parsed
    except (ValueError, TypeError):
        values = [re.sub(r"^[-*\d.、\s]+", "", line) for line in text.splitlines()]
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(str(value).strip()[:200] for value in values if isinstance(value, str) and value.strip()))[:limit]


def _citation_ids(answer):
    return [int(value) for value in re.findall(r"\[证据\s*(\d+)\]", answer)]


def _has_valid_citations(answer, source_count):
    citations = _citation_ids(answer)
    return bool(citations) and all(1 <= value <= source_count for value in citations)


class RagEngine:
    def __init__(self, source_dir, base_url, model, api_key="EMPTY"):
        self.source_dir = Path(source_dir)
        self.base_url, self.model, self.api_key = base_url, model, api_key
        self._lock = threading.Lock()
        self.ready, self.last_sync_at = False, 0.0
        self.refresh_seconds = max(30, int(os.getenv("ONLINE_REFRESH_SECONDS", "300")))
        self.rerank_min_score = float(os.getenv("RERANK_MIN_SCORE", "0.05"))
        self.evidence_min_score = float(os.getenv("EVIDENCE_MIN_SCORE", "0.15"))
        self.query_rewrite_enabled = os.getenv("QUERY_REWRITE_ENABLED", "true").lower() == "true"
        self.answer_verification_enabled = os.getenv("ANSWER_VERIFICATION_ENABLED", "true").lower() == "true"
        self.verification_fail_closed = os.getenv("VERIFICATION_FAIL_CLOSED", "false").lower() == "true"
        self.synonyms = _load_synonyms(os.getenv("QUERY_SYNONYMS_PATH"))
        self._query_rewrite_cache = {}
        self.sync_failures, self.cache_hits, self.cache_misses = [], 0, 0
        self.clickhouse = ClickHouseStore()
        self.clickhouse_required = os.getenv("CLICKHOUSE_REQUIRED", "false").lower() == "true"
        if self.clickhouse_required and not self.clickhouse.enabled:
            raise RuntimeError("CLICKHOUSE_REQUIRED=true 时必须同时设置 CLICKHOUSE_ENABLED=true")
        self._model_signature = None

    def _configure_generation_client(self):
        config = load_model_config(include_secret=True)
        signature = (config["base_url"], config["model"], config.get("api_key"), config.get("timeout", 120))
        if signature != self._model_signature:
            self.base_url, self.model, self.api_key = signature[:3]
            self.client = OpenAI(base_url=self.base_url, api_key=self.api_key, timeout=float(signature[3]))
            self._model_signature = signature

    def _cache_path(self):
        configured = os.getenv("VECTOR_CACHE_PATH")
        if configured:
            return Path(configured)
        config_dir = Path(os.getenv("INTEGRATIONS_CONFIG", "/data/config/integrations.json")).parent
        return config_dir / "embedding_cache.sqlite3"

    @contextmanager
    def _database(self):
        path = self._cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS embeddings (content_hash TEXT PRIMARY KEY, model TEXT NOT NULL, dimension INTEGER NOT NULL, vector BLOB NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS retrieval_events (created_at REAL, username TEXT, question_hash TEXT, permitted_chunks INTEGER, candidates INTEGER, best_score REAL, answered INTEGER, latency_ms INTEGER)")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

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
                path = str(chunk["file_path"])
                chunk["_document_id"] = hashlib.sha256(path.encode()).hexdigest()[:32]
                chunk["_chunk_id"] = hashlib.sha256((path + "\0" + chunk.get("location", "") + "\0" + chunk["text"]).encode()).hexdigest()
            if not hasattr(self, "embedding"):
                from sentence_transformers import CrossEncoder, SentenceTransformer
                self.embedding = SentenceTransformer("BAAI/bge-small-zh-v1.5")
                self.reranker = CrossEncoder("BAAI/bge-reranker-base")
                self._configure_generation_client()
            vectors = self._encode_documents(chunks)
            if self.clickhouse.enabled:
                try:
                    self.clickhouse.publish(chunks, vectors, get_document_access)
                except Exception as error:
                    failures.append({"source": "clickhouse", "error": str(error)})
                    if self.clickhouse_required:
                        raise RuntimeError(f"ClickHouse 同步失败：{error}") from error
            self.documents, self.chunks, self.vectors = documents, chunks, vectors
            self.sync_failures, self.last_sync_at, self.ready = failures, now, True

    def status(self):
        model_config = load_model_config()
        return {"ready": self.ready, "files": len({item["file_path"] for item in self.documents}) if self.ready else 0, "chunks": len(self.chunks) if self.ready else 0, "model": model_config["model"], "model_provider": model_config["provider"], "last_sync_at": self.last_sync_at or None, "refresh_seconds": self.refresh_seconds, "sync_failures": self.sync_failures, "retrieval": "multi_query_clickhouse_hybrid_rerank" if self.clickhouse.enabled else "multi_query_faiss_bm25_rerank", "answer_verification": self.answer_verification_enabled, "vector_store": self.clickhouse.health(), "vector_cache_hits": self.cache_hits, "vector_cache_misses": self.cache_misses}

    def metrics_summary(self, days=30):
        since = time.time() - days * 86400
        with self._database() as database:
            row = database.execute("SELECT COUNT(*) questions, COALESCE(SUM(answered), 0) answered, COALESCE(AVG(latency_ms), 0) latency, COALESCE(AVG(best_score), 0) score FROM retrieval_events WHERE created_at >= ?", (since,)).fetchone()
            users = database.execute("SELECT COUNT(DISTINCT username) FROM retrieval_events WHERE created_at >= ?", (since,)).fetchone()[0]
        questions = row[0]
        return {"questions": questions, "answered": row[1], "answer_rate": round(row[1] / questions * 100, 1) if questions else None, "average_latency_ms": round(row[2]), "average_best_score": round(row[3], 4), "active_users": users, "days": days}

    def metrics_trend(self, days=14):
        since = time.time() - days * 86400
        with self._database() as database:
            rows = database.execute("SELECT date(created_at, 'unixepoch', 'localtime') day, COUNT(*) questions, SUM(answered) answered, AVG(latency_ms) latency FROM retrieval_events WHERE created_at >= ? GROUP BY day ORDER BY day", (since,)).fetchall()
        return [{"day": row[0], "questions": row[1], "answered": row[2] or 0, "latency_ms": round(row[3] or 0)} for row in rows]

    def failed_questions(self, limit=50):
        with self._database() as database:
            rows = database.execute("SELECT created_at, username, question_hash, best_score, latency_ms FROM retrieval_events WHERE answered=0 ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [{"created_at": row[0], "username": row[1], "question_hash": row[2], "best_score": row[3], "latency_ms": row[4]} for row in rows]

    def clear(self):
        with self._lock:
            self.documents, self.chunks = [], []
            self.vectors = np.empty((0, 0), dtype="float32")
            self.ready, self.last_sync_at = False, 0.0

    def _query_variants(self, question):
        deterministic = _expand_with_synonyms(question, self.synonyms)
        if not self.query_rewrite_enabled:
            return deterministic
        self._configure_generation_client()
        cache_key = (self._model_signature, question)
        if cache_key in self._query_rewrite_cache:
            return list(dict.fromkeys([*deterministic, *self._query_rewrite_cache[cache_key]]))[:6]
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "你只负责把员工口语问题改写成企业制度检索语句。保留原意、实体、年份、数字和否定词，不回答问题，不添加事实。输出 JSON：{\"queries\":[\"检索语句1\",\"检索语句2\",\"检索语句3\"]}。"},
                    {"role": "user", "content": question},
                ],
                temperature=0,
                max_tokens=180,
            )
            rewrites = _parse_query_rewrites(response.choices[0].message.content)
        except Exception:
            rewrites = []
        if len(self._query_rewrite_cache) >= 500:
            self._query_rewrite_cache.clear()
        self._query_rewrite_cache[cache_key] = rewrites
        return list(dict.fromkeys([*deterministic, *rewrites]))[:6]

    def _verify_answer(self, question, answer, context):
        if not self.answer_verification_enabled:
            return True
        try:
            for _ in range(2):
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": "你是严格的事实核验器。检查回答中的每个事实是否都能由证据直接支持，且引用编号与证据一致。只要有一个事实无依据、扩大解释、数字冲突或引用错误，只输出 UNSUPPORTED；全部有依据才输出 SUPPORTED。不要解释。"},
                        {"role": "user", "content": f"问题：\n{question}\n\n待核验回答：\n{answer}\n\n证据：\n{context}"},
                    ],
                    temperature=0,
                    max_tokens=80,
                )
                verdict = (response.choices[0].message.content or "").strip().upper()
                if verdict.startswith("UNSUPPORTED"):
                    return False
                if verdict.startswith("SUPPORTED"):
                    return True
            return not self.verification_fail_closed
        except Exception:
            return not self.verification_fail_closed

    def retrieve(self, question, user):
        self.load()
        permitted = [chunk for chunk in self.chunks if can_access(chunk["file_name"], user, chunk)]
        search_chunks, version_note = filter_chunks_for_question(permitted, question)
        if not search_chunks:
            return {"results": [], "version_note": version_note, "permitted_chunks": 0, "best_score": 0.0}
        query_variants = self._query_variants(question)
        query_vectors = np.asarray(self.embedding.encode(["为这个句子生成表示以用于检索相关文章：" + value for value in query_variants], normalize_embeddings=True), dtype="float32")
        recall_size = min(30, len(search_chunks))
        vector_results = []
        if self.clickhouse.enabled:
            allowed_ids = {chunk["_chunk_id"] for chunk in search_chunks}
            try:
                for query_vector in query_vectors:
                    vector_results.append([(item["chunk_id"], item["score"]) for item in self.clickhouse.search(query_vector, user, recall_size * 3) if item["chunk_id"] in allowed_ids][:recall_size])
            except Exception:
                vector_results = []
                if self.clickhouse_required:
                    raise
        if not vector_results:
            import faiss
            subset = self.vectors[[chunk["_vector_id"] for chunk in search_chunks]]
            index = faiss.IndexFlatIP(subset.shape[1]); index.add(subset)
            vector_scores, vector_ids = index.search(query_vectors, k=recall_size)
            vector_results = [[(search_chunks[int(chunk_id)]["_chunk_id"], float(vector_scores[query_index][rank])) for rank, chunk_id in enumerate(vector_ids[query_index])] for query_index in range(len(query_variants))]
        id_to_position = {chunk["_chunk_id"]: index for index, chunk in enumerate(search_chunks)}
        lexical_results = [_bm25(value, search_chunks) for value in query_variants]
        lexical_scores = np.max(np.stack(lexical_results), axis=0)
        fused, vector_lookup = {}, {}
        for vector_pairs in vector_results:
            for rank, (stable_id, score) in enumerate(vector_pairs):
                chunk_id = id_to_position.get(stable_id)
                if chunk_id is None:
                    continue
                fused[chunk_id] = fused.get(chunk_id, 0) + 1 / (61 + rank)
                vector_lookup[chunk_id] = max(vector_lookup.get(chunk_id, -1.0), score)
        for scores in lexical_results:
            for rank, chunk_id in enumerate(np.argsort(-scores)[:recall_size]):
                if scores[int(chunk_id)] <= 0:
                    continue
                fused[int(chunk_id)] = fused.get(int(chunk_id), 0) + 1 / (61 + rank)
        candidate_ids = [item[0] for item in sorted(fused.items(), key=lambda value: value[1], reverse=True)[:30]]
        candidates = [{**search_chunks[chunk_id], "faiss_score": vector_lookup.get(chunk_id, 0.0), "bm25_score": float(lexical_scores[chunk_id]), "fusion_score": fused[chunk_id]} for chunk_id in candidate_ids]
        rerank_question = question if len(query_variants) == 1 else f"{question}\n相关表达：{'；'.join(query_variants[1:])}"
        rerank_scores = self.reranker.predict([[rerank_question, item["text"]] for item in candidates])
        for item, score in zip(candidates, rerank_scores):
            item["rerank_score"] = float(score)
        candidates.sort(key=lambda item: item["rerank_score"], reverse=True)
        best_score = candidates[0]["rerank_score"] if candidates else 0.0
        required_score = max(self.rerank_min_score, self.evidence_min_score)
        if not candidates or best_score < required_score:
            return {"results": [], "version_note": version_note + "；相关度不足，已停止生成", "permitted_chunks": len(search_chunks), "best_score": best_score}
        return {"results": candidates[:3], "version_note": version_note, "permitted_chunks": len(search_chunks), "best_score": best_score, "candidate_count": len(candidates), "query_variants": query_variants}

    def ask(self, question, user):
        started = time.time()
        retrieval = self.retrieve(question, user)
        top_results = retrieval["results"]
        if not top_results:
            self._record_metric(user, question, retrieval["permitted_chunks"], 0, retrieval["best_score"], False, started)
            return {"answer_id": uuid.uuid4().hex, "question_hash": hashlib.sha256(question.encode()).hexdigest(), "answer": INSUFFICIENT_ANSWER, "sources": [], "version_note": retrieval["version_note"], "grounded": False}
        top_results.sort(key=lambda item: item.get("effective_order", (0,)))
        context = "\n\n".join(f"[证据{index}｜来源：{item['file_name']}，{item['location']}，类型：{'增量修订' if item.get('document_kind') == 'amendment' else '完整版本'}]\n{item['text']}" for index, item in enumerate(top_results, 1))
        self._configure_generation_client()
        response = self.client.chat.completions.create(model=self.model, messages=[{"role": "system", "content": "你是企业内部文档答疑助手。只能依据检索证据回答，不得使用常识补充、猜测或编造。资料按生效顺序从旧到新排列；同一事项冲突时，后面的增量修订覆盖前面的完整版本或旧修订，未被后续修订的内容继续有效。资料不足时只回答：未在资料中找到足够信息。每个事实性句子的末尾必须标注支持它的 [证据N]；不得引用不存在的编号。使用简洁中文回答。"}, {"role": "user", "content": f"用户问题：\n{question}\n\n检索证据：\n{context}"}], temperature=0, max_tokens=500)
        answer = (response.choices[0].message.content or INSUFFICIENT_ANSWER).strip()
        grounded = answer != INSUFFICIENT_ANSWER and _has_valid_citations(answer, len(top_results))
        if grounded:
            grounded = self._verify_answer(question, answer, context)
        if not grounded:
            answer = INSUFFICIENT_ANSWER
            sources = []
        else:
            used = set(_citation_ids(answer))
            sources = [{"file_name": item["file_name"], "location": item["location"], "excerpt": item["text"][:500], "version_label": item.get("version_label"), "document_kind": item.get("document_kind", "full")} for index, item in enumerate(top_results, 1) if index in used]
        self._record_metric(user, question, retrieval["permitted_chunks"], retrieval["candidate_count"], retrieval["best_score"], grounded, started)
        return {"answer_id": uuid.uuid4().hex, "question_hash": hashlib.sha256(question.encode()).hexdigest(), "answer": answer, "sources": sources, "version_note": retrieval["version_note"], "grounded": grounded}
