"""ClickHouse persistence and permission-aware vector retrieval.

The module is optional: local development keeps using FAISS when CLICKHOUSE_ENABLED=false.
"""
import os
import re
import time
import uuid
from datetime import datetime, timezone


def _identifier(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("ClickHouse 数据库或表名不合法")
    return value


class ClickHouseStore:
    def __init__(self):
        self.enabled = os.getenv("CLICKHOUSE_ENABLED", "false").lower() == "true"
        self.database = _identifier(os.getenv("CLICKHOUSE_DATABASE", "tier_rag"))
        self.table = _identifier(os.getenv("CLICKHOUSE_CHUNKS_TABLE", "document_chunks"))
        self.dimension = int(os.getenv("EMBEDDING_DIMENSION", "512"))
        self.client = None
        self.active_snapshot = None
        self.last_error = None

    def connect(self):
        if not self.enabled:
            return None
        if self.client is None:
            import clickhouse_connect
            self.client = clickhouse_connect.get_client(
                host=os.getenv("CLICKHOUSE_HOST", "localhost"),
                port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
                username=os.getenv("CLICKHOUSE_USER", "default"),
                password=os.getenv("CLICKHOUSE_PASSWORD", ""),
                secure=os.getenv("CLICKHOUSE_SECURE", "false").lower() == "true",
                connect_timeout=int(os.getenv("CLICKHOUSE_CONNECT_TIMEOUT", "10")),
            )
        return self.client

    def ensure_schema(self):
        client = self.connect()
        if not client:
            return
        client.command(f"CREATE DATABASE IF NOT EXISTS {self.database}")
        client.command(f"""
            CREATE TABLE IF NOT EXISTS {self.database}.{self.table} (
                snapshot_id UUID,
                chunk_id String,
                document_id String,
                file_name String,
                file_path String,
                location String,
                content String,
                embedding Array(Float32),
                access_scope LowCardinality(String),
                departments Array(String),
                version_label String,
                document_kind LowCardinality(String),
                effective_order String,
                indexed_at DateTime64(3),
                INDEX vector_idx embedding TYPE vector_similarity('hnsw', 'cosineDistance', {self.dimension}, 'bf16', 64, 256) GRANULARITY 1
            ) ENGINE = MergeTree
            PARTITION BY snapshot_id
            ORDER BY (snapshot_id, document_id, chunk_id)
            TTL indexed_at + INTERVAL 30 DAY DELETE
        """)

    def health(self):
        if not self.enabled:
            return {"enabled": False, "healthy": True, "mode": "faiss_local"}
        started = time.time()
        try:
            version = self.connect().command("SELECT version()")
            self.last_error = None
            return {"enabled": True, "healthy": True, "mode": "clickhouse", "version": str(version), "latency_ms": int((time.time() - started) * 1000)}
        except Exception as error:
            self.last_error = str(error)
            return {"enabled": True, "healthy": False, "mode": "clickhouse", "error": self.last_error}

    def publish(self, chunks, vectors, acl_resolver):
        if not self.enabled:
            return None
        self.ensure_schema()
        snapshot = uuid.uuid4()
        rows = []
        for chunk, vector in zip(chunks, vectors):
            acl = acl_resolver(chunk["file_name"], chunk)
            path = str(chunk["file_path"])
            chunk_id = chunk["_chunk_id"]
            rows.append([
                snapshot, chunk_id, chunk["_document_id"], chunk["file_name"], path,
                chunk.get("location", ""), chunk["text"], vector.tolist(), acl["scope"],
                acl.get("departments", []), chunk.get("version_label") or "",
                chunk.get("document_kind", "full"), repr(chunk.get("effective_order", ())),
                datetime.now(timezone.utc),
            ])
        self.connect().insert(
            f"{self.database}.{self.table}", rows,
            column_names=["snapshot_id", "chunk_id", "document_id", "file_name", "file_path", "location", "content", "embedding", "access_scope", "departments", "version_label", "document_kind", "effective_order", "indexed_at"],
        )
        self.active_snapshot = str(snapshot)
        self.last_error = None
        return self.active_snapshot

    def search(self, vector, user, limit=30):
        if not self.enabled or not self.active_snapshot:
            return []
        is_admin = user.get("role") == "admin"
        departments = user.get("departments") or []
        sql = f"""
            SELECT chunk_id, 1 - cosineDistance(embedding, {{vector:Array(Float32)}}) AS score
            FROM {self.database}.{self.table}
            WHERE snapshot_id = {{snapshot:UUID}}
              AND ({{is_admin:UInt8}} = 1 OR access_scope = 'public'
                   OR (access_scope = 'departments' AND hasAny(departments, {{departments:Array(String)}})))
            ORDER BY cosineDistance(embedding, {{vector:Array(Float32)}})
            LIMIT {{limit:UInt32}}
        """
        result = self.connect().query(sql, parameters={"vector": vector.tolist(), "snapshot": self.active_snapshot, "is_admin": int(is_admin), "departments": departments, "limit": limit})
        return [{"chunk_id": row[0], "score": float(row[1])} for row in result.result_rows]
