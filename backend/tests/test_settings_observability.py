from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.api.deps import require_admin
from app.api.v1.settings import (
    ObservabilityConfigUpdate,
    get_observability_config,
    router,
    update_observability_config,
)
from app.services.trace_switches import TraceSwitches, trace_switches


class _MockSession:
    def __init__(self, execute_results=None) -> None:
        self.flush_calls = 0
        self.commit_calls = 0
        self._execute_results = list(execute_results or [])

    async def flush(self) -> None:
        self.flush_calls += 1

    async def commit(self) -> None:
        self.commit_calls += 1

    async def execute(self, _statement):
        if not self._execute_results:
            raise AssertionError("Unexpected execute() call")
        return self._execute_results.pop(0)


class _RowResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


@pytest.fixture(autouse=True)
def _stub_trace_store_health(monkeypatch):
    async def _fake_trace_store_health(_provider: str) -> tuple[str, str]:
        return "healthy", "Trace store connection is healthy."

    monkeypatch.setattr(
        "app.api.v1.settings._resolve_trace_store_health",
        _fake_trace_store_health,
    )


@pytest.mark.asyncio
async def test_get_observability_config_returns_platform_settings_values(monkeypatch):
    now = datetime.now(UTC)

    async def _fake_load_ps(_db):
        return SimpleNamespace(
            trace_storage_enabled=True,
            trace_retention_days=90,
            trace_store_payloads=True,
            last_aggregation_at=None,
        )

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _fake_load_ps,
    )

    response = await get_observability_config(
        user=SimpleNamespace(),
        db=_MockSession(
            execute_results=[
                _RowResult([SimpleNamespace(pricing_last_updated_at=now - timedelta(days=7))]),
                _RowResult([SimpleNamespace(model="gpt-5.3-codex", unresolved_count=2)]),
                _RowResult([]),  # configured price rules (none)
            ]
        ),
    )

    assert response.trace_storage_enabled is True
    assert response.trace_store_health_status == "healthy"
    assert response.trace_store_health_message == "Trace store connection is healthy."
    assert response.trace_retention_days == 90
    assert response.trace_store_payloads is True
    assert response.last_aggregation_at is None
    assert response.aggregation_stale is True
    assert response.pricing_stale is False
    assert response.pricing_staleness_days == 7
    assert response.models_without_pricing == ["gpt-5.3-codex"]


@pytest.mark.asyncio
async def test_update_observability_config_updates_and_audits(monkeypatch):
    platform_settings = SimpleNamespace(
        trace_storage_enabled=True,
        trace_retention_days=90,
        trace_store_payloads=True,
        last_aggregation_at=datetime.now(UTC),
    )
    db = _MockSession(
        execute_results=[
            _RowResult([SimpleNamespace(pricing_last_updated_at=datetime.now(UTC) - timedelta(days=45))]),
            _RowResult([SimpleNamespace(model="unknown-model", unresolved_count=3)]),
            _RowResult([]),  # configured price rules (none)
        ]
    )

    async def _fake_load_ps(_db):
        return platform_settings

    audit_calls: list[dict] = []

    async def _fake_log_audit(*_args, **kwargs):
        audit_calls.append(kwargs)

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _fake_load_ps,
    )
    monkeypatch.setattr("app.api.v1.settings.log_audit", _fake_log_audit)

    response = await update_observability_config(
        body=ObservabilityConfigUpdate(
            trace_storage_enabled=False,
            trace_retention_days=45,
            trace_store_payloads=False,
        ),
        request=SimpleNamespace(),
        user=SimpleNamespace(id="admin-id", email="admin@example.com"),
        db=db,
    )

    assert platform_settings.trace_storage_enabled is False
    assert platform_settings.trace_retention_days == 45
    assert platform_settings.trace_store_payloads is False
    assert db.flush_calls == 1
    # Committed before answering, then in force in this process.
    assert db.commit_calls == 1
    assert trace_switches() == TraceSwitches(storage_enabled=False, store_payloads=False)
    assert response.trace_storage_enabled is False
    assert response.trace_store_health_status == "healthy"
    assert response.trace_store_health_message == "Trace store connection is healthy."
    assert response.trace_retention_days == 45
    assert response.trace_store_payloads is False
    assert response.last_aggregation_at is not None
    assert response.aggregation_stale is False
    assert response.pricing_stale is True
    assert response.pricing_staleness_days == 45
    assert response.models_without_pricing == ["unknown-model"]
    assert len(audit_calls) == 1
    assert audit_calls[0]["summary"] == "Updated observability settings"
    assert audit_calls[0]["details"] == {
        "trace_storage_enabled": {"from": True, "to": False},
        "trace_store_payloads": {"from": True, "to": False},
        "trace_retention_days": {"from": 90, "to": 45},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("saved_days", "expected_sync"),
    [(45, [45]), (90, [])],
)
async def test_update_observability_config_pushes_changed_retention_to_clickhouse(
    monkeypatch, saved_days: int, expected_sync: list[int]
):
    # ClickHouse expires traces by a TTL declared on the table, so a new
    # retention window has to be pushed to it - the hourly purge that enforces
    # retention in PostgreSQL does nothing for ClickHouse. Only on an actual
    # change: re-saving the same value must not re-ALTER the tables.
    platform_settings = SimpleNamespace(
        trace_storage_enabled=True,
        trace_retention_days=90,
        trace_store_payloads=True,
        last_aggregation_at=datetime.now(UTC),
    )
    db = _MockSession(
        execute_results=[
            _RowResult([SimpleNamespace(pricing_last_updated_at=datetime.now(UTC))]),
            _RowResult([]),
            _RowResult([]),
        ]
    )
    synced: list[int] = []

    async def _fake_load_ps(_db):
        return platform_settings

    async def _fake_log_audit(*_args, **_kwargs):
        return None

    async def _fake_sync(retention_days: int, **_kwargs) -> list[str]:
        synced.append(retention_days)
        return ["llm_traces"]

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _fake_load_ps,
    )
    monkeypatch.setattr("app.api.v1.settings.log_audit", _fake_log_audit)
    monkeypatch.setattr(
        "app.services.trace_storage.sync_clickhouse_trace_retention", _fake_sync
    )

    await update_observability_config(
        body=ObservabilityConfigUpdate(
            trace_storage_enabled=True,
            trace_retention_days=saved_days,
            trace_store_payloads=True,
        ),
        request=SimpleNamespace(),
        user=SimpleNamespace(id="admin-id", email="admin@example.com"),
        db=db,
    )

    assert synced == expected_sync


@pytest.mark.asyncio
async def test_observability_response_marks_recent_aggregation_as_fresh():
    from app.api.v1.settings import _build_observability_config_response

    now = datetime.now(UTC)
    response = await _build_observability_config_response(
        SimpleNamespace(
            trace_storage_enabled=True,
            trace_retention_days=90,
            trace_store_payloads=True,
            last_aggregation_at=now - timedelta(hours=24),
        ),
        _MockSession(
            execute_results=[
                _RowResult([SimpleNamespace(pricing_last_updated_at=now - timedelta(days=1))]),
                _RowResult([]),  # unresolved models (none)
                _RowResult([]),  # configured price rules (none)
            ]
        ),
    )
    assert response.aggregation_stale is False


@pytest.mark.asyncio
async def test_observability_response_marks_old_aggregation_as_stale():
    from app.api.v1.settings import _build_observability_config_response

    now = datetime.now(UTC)
    response = await _build_observability_config_response(
        SimpleNamespace(
            trace_storage_enabled=True,
            trace_retention_days=90,
            trace_store_payloads=True,
            last_aggregation_at=now - timedelta(hours=49),
        ),
        _MockSession(
            execute_results=[
                _RowResult([SimpleNamespace(pricing_last_updated_at=None)]),
                _RowResult([SimpleNamespace(pricing_last_updated_at=None)]),
                _RowResult([]),  # unresolved models (none)
                _RowResult([]),  # configured price rules (none)
            ]
        ),
    )
    assert response.aggregation_stale is True


@pytest.mark.asyncio
async def test_observability_response_marks_pricing_as_stale_when_old():
    from app.api.v1.settings import _build_observability_config_response

    now = datetime.now(UTC)
    response = await _build_observability_config_response(
        SimpleNamespace(
            trace_storage_enabled=True,
            trace_retention_days=90,
            trace_store_payloads=True,
            last_aggregation_at=now,
        ),
        _MockSession(
            execute_results=[
                _RowResult([SimpleNamespace(pricing_last_updated_at=now - timedelta(days=31))]),
                _RowResult([SimpleNamespace(model="legacy-model", unresolved_count=1)]),
                _RowResult([]),  # configured price rules (none)
            ]
        ),
    )
    assert response.pricing_stale is True
    assert response.pricing_staleness_days == 31
    assert response.models_without_pricing == ["legacy-model"]


def test_update_observability_route_requires_admin_dependency():
    put_route = next(
        route
        for route in router.routes
        if getattr(route, "path", "") == "/observability"
        and "PUT" in getattr(route, "methods", set())
    )
    dependency_calls = {dependency.call for dependency in put_route.dependant.dependencies}
    assert require_admin in dependency_calls


# ---------------------------------------------------------------------------
# Pydantic validation boundary tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_value", [0, -1, 366, 999])
def test_observability_config_update_rejects_invalid_retention_days(bad_value):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ObservabilityConfigUpdate(
            trace_storage_enabled=True,
            trace_retention_days=bad_value,
            trace_store_payloads=True,
        )


@pytest.mark.parametrize("good_value", [1, 90, 365])
def test_observability_config_update_accepts_valid_retention_days(good_value):
    model = ObservabilityConfigUpdate(
        trace_storage_enabled=True,
        trace_retention_days=good_value,
        trace_store_payloads=True,
    )
    assert model.trace_retention_days == good_value
