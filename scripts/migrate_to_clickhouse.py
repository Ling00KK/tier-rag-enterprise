"""Load current documents, generate/reuse embeddings and publish one ClickHouse snapshot."""
import os

from app.rag_engine import RagEngine


def main():
    if os.getenv("CLICKHOUSE_ENABLED", "false").lower() != "true":
        raise SystemExit("请先设置 CLICKHOUSE_ENABLED=true")
    engine = RagEngine(
        source_dir=os.getenv("SOURCE_DIR", "/data/source"),
        base_url=os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1"),
        model=os.getenv("VLLM_MODEL", "model"),
        api_key=os.getenv("VLLM_API_KEY", "EMPTY"),
    )
    health = engine.clickhouse.health()
    if not health.get("healthy"):
        raise SystemExit(f"ClickHouse 连接失败：{health.get('error')}")
    engine.load(force=True)
    status = engine.status()
    if not status["vector_store"].get("healthy"):
        raise SystemExit("迁移后 ClickHouse 健康检查失败")
    print(f"迁移完成：{status['files']} 个来源，{status['chunks']} 个切片，snapshot={engine.clickhouse.active_snapshot}")


if __name__ == "__main__":
    main()
