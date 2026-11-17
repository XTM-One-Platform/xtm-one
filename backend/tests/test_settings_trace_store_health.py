import pytest

from app.api.v1.settings import _resolve_trace_store_health


@pytest.mark.asyncio
async def test_trace_store_health_returns_healthy_for_postgres():
    status, message = await _resolve_trace_store_health("postgres")

    assert status == "healthy"
    assert "PostgreSQL" in message


@pytest.mark.asyncio
async def test_trace_store_health_reports_unhealthy_when_clickhouse_config_missing(
    monkeypatch,
):
    monkeypatch.setattr(
        "app.services.tracing.clickhouse_client.has_clickhouse_runtime_config",
        lambda: False,
    )

    status, message = await _resolve_trace_store_health("clickhouse")

    assert status == "unhealthy"
    assert "credentials" in message.lower()


@pytest.mark.asyncio
async def test_trace_store_health_reports_healthy_when_clickhouse_query_succeeds(
    monkeypatch,
):
    monkeypatch.setattr(
        "app.services.tracing.clickhouse_client.has_clickhouse_runtime_config",
        lambda: True,
    )

    close_calls = {"count": 0}

    class _FakeClient:
        @classmethod
        def from_settings(cls):
            return cls()

        async def query_json_rows(self, _query: str):
            return [{"ok": 1}]

        async def close(self):
            close_calls["count"] += 1

    monkeypatch.setattr(
        "app.services.tracing.clickhouse_client.HTTPClickHouseClient",
        _FakeClient,
    )

    status, message = await _resolve_trace_store_health("clickhouse")

    assert status == "healthy"
    assert "healthy" in message.lower()
    assert close_calls["count"] == 1
