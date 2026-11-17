from app.core.config import settings
from app.services.tracing.local_sink import LocalPGSink
from app.services.tracing.store import (
    MirrorTracingSink,
    build_trace_read_store,
    build_trace_write_sink,
    ensure_trace_read_provider_supported,
)


def test_postgres_trace_store_provider_builds_pg_sink(monkeypatch):
    monkeypatch.setattr(settings, "tracing_store_provider", "postgres")
    sink = build_trace_write_sink()
    assert isinstance(sink, LocalPGSink)
    assert build_trace_read_store().provider == "postgres"


def test_clickhouse_trace_store_provider_builds_clickhouse_sink(monkeypatch):
    monkeypatch.setattr(settings, "tracing_store_provider", "clickhouse")
    sink = build_trace_write_sink()
    assert isinstance(sink, MirrorTracingSink)
    # Windows served from ``llm_daily_stats`` read PostgreSQL in this mode too,
    # and the PostgreSQL sink records the day of a late rating for its rebuild.
    assert any(isinstance(inner, LocalPGSink) for inner in sink._sinks)
    assert build_trace_read_store().provider == "clickhouse"
    ensure_trace_read_provider_supported()
