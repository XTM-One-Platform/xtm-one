"""Coordination retention (#2070): closed work items and resolved shared state.

The windows an administrator sets in Settings win over the environment
defaults, 0 keeps forever, both purges anchor on ``updated_at``, a non-terminal
item or an active entry is never purged, and a backlog drains across ticks.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

import app.services.autonomous as autonomous
import app.services.coordination_retention as cr
import app.services.work_item as wi_service
from app.core.config import Settings
from app.services.platform_insights.overview import _coordination_retention


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect())).lower()


class _Result:
    def __init__(self, rows=None, rowcount=0):
        self._rows = rows or []
        self.rowcount = rowcount

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, results):
        self._results = list(results)
        self.statements: list = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        self.statements.append(statement)
        return self._results.pop(0)

    async def commit(self):
        self.committed = True


# -- defaults and the effective windows -------------------------------------------


def test_the_environment_defaults_keep_closed_items_and_resolved_entries_90_days():
    fields = Settings.model_fields
    assert fields["work_item_resolved_retention_days"].default == 90
    assert fields["shared_state_resolved_retention_days"].default == 90
    # The other coordination windows keep their defaults.
    assert fields["work_item_abandoned_after_days"].default == 14
    assert fields["shared_state_archive_retention_days"].default == 14
    assert fields["workset_resolved_retention_days"].default == 7


def test_no_stored_value_answers_the_environment_defaults(monkeypatch):
    monkeypatch.setattr(cr.settings, "work_item_resolved_retention_days", 90)
    monkeypatch.setattr(cr.settings, "shared_state_resolved_retention_days", 90)
    monkeypatch.setattr(cr.settings, "work_item_abandoned_after_days", 14)
    monkeypatch.setattr(cr.settings, "shared_state_archive_retention_days", 14)
    monkeypatch.setattr(cr.settings, "workset_resolved_retention_days", 7)
    monkeypatch.setattr(cr.settings, "assignment_run_retention_days", 90)
    monkeypatch.setattr(cr.settings, "ctem_signal_discarded_retention_days", 30)
    monkeypatch.setattr(cr.settings, "ctem_signal_watchlist_retention_days", 90)
    assert cr.coordination_retention_days(None) == {
        "work_item_resolved_days": 90,
        "work_item_abandoned_days": 14,
        "shared_state_resolved_days": 90,
        "shared_state_archive_days": 14,
        "workset_resolved_days": 7,
        "assignment_run_days": 90,
        "ctem_signal_discarded_days": 30,
        "ctem_signal_watchlist_days": 90,
    }


def test_an_admin_value_wins_over_the_environment_and_0_keeps_forever(monkeypatch):
    monkeypatch.setattr(cr.settings, "work_item_resolved_retention_days", 90)
    monkeypatch.setattr(cr.settings, "shared_state_resolved_retention_days", 90)
    windows = cr.coordination_retention_days({"work_item_resolved_days": 30, "shared_state_resolved_days": 0})
    assert windows["work_item_resolved_days"] == 30
    assert windows["shared_state_resolved_days"] == 0


@pytest.mark.parametrize("bad", [-1, 3651, "30", 1.5, True, None])
def test_a_stored_value_out_of_bounds_falls_back_to_the_default(monkeypatch, bad):
    monkeypatch.setattr(cr.settings, "work_item_resolved_retention_days", 90)
    assert cr.coordination_retention_days({"work_item_resolved_days": bad})["work_item_resolved_days"] == 90


def test_the_overview_reports_the_effective_retention(monkeypatch):
    monkeypatch.setattr(cr.settings, "shared_state_resolved_retention_days", 90)
    monkeypatch.setattr(cr.settings, "assignment_run_retention_days", 90)
    ps = SimpleNamespace(coordination_retention={"work_item_resolved_days": 45})
    assert _coordination_retention(ps) == {
        "work_item_retention_days": 45,
        "shared_state_retention_days": 90,
        "run_retention_days": 90,
    }


# -- the settings API --------------------------------------------------------------


def test_the_settings_body_refuses_a_window_outside_0_to_3650():
    from app.api.v1.settings import CoordinationRetention

    good = dict(
        work_item_resolved_days=90,
        work_item_abandoned_days=14,
        shared_state_resolved_days=90,
        shared_state_archive_days=14,
        workset_resolved_days=7,
    )
    assert CoordinationRetention(**good).work_item_resolved_days == 90
    assert CoordinationRetention(**{**good, "shared_state_resolved_days": 0}).shared_state_resolved_days == 0
    for bad in (-1, 3651):
        with pytest.raises(ValidationError):
            CoordinationRetention(**{**good, "work_item_resolved_days": bad})


@pytest.mark.asyncio
async def test_the_settings_write_stores_the_windows_and_audits_the_change(monkeypatch):
    import app.api.v1.settings as settings_api

    ps = SimpleNamespace(
        trace_retention_days=90,
        trace_storage_enabled=True,
        trace_store_payloads=True,
        audit_log_retention_days=90,
        coordination_retention=None,
    )
    monkeypatch.setattr("app.api.v1.platform_settings_helpers.get_platform_settings", AsyncMock(return_value=ps))
    audit = AsyncMock()
    monkeypatch.setattr(settings_api, "log_audit", audit)
    monkeypatch.setattr("app.services.trace_switches.publish_trace_switches", lambda *_a, **_k: None)
    monkeypatch.setattr(settings_api, "_build_observability_config_response", AsyncMock(return_value="ok"))
    monkeypatch.setattr(cr.settings, "work_item_resolved_retention_days", 90)
    db = SimpleNamespace(flush=AsyncMock(), commit=AsyncMock())
    body = settings_api.ObservabilityConfigUpdate(
        trace_storage_enabled=True,
        trace_retention_days=90,
        trace_store_payloads=True,
        coordination_retention=settings_api.CoordinationRetention(
            work_item_resolved_days=30,
            work_item_abandoned_days=14,
            shared_state_resolved_days=90,
            shared_state_archive_days=14,
            workset_resolved_days=7,
        ),
    )
    out = await settings_api.update_observability_config(body, request=None, user=SimpleNamespace(), db=db)
    assert out == "ok"
    assert ps.coordination_retention["work_item_resolved_days"] == 30
    details = audit.await_args.kwargs["details"]
    assert details["coordination_retention"]["work_item_resolved_days"] == {"from": 90, "to": 30}


# -- the purges ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_closed_item_purge_anchors_on_updated_at_and_rechecks_terminal(monkeypatch):
    session = _Session([_Result(rows=[uuid.uuid4(), uuid.uuid4()]), _Result(rowcount=2)])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    assert await wi_service.purge_terminal_work_items(older_than_days=90, limit=10) == 2
    select_sql, delete_sql = _sql(session.statements[0]), _sql(session.statements[1])
    for sql in (select_sql, delete_sql):
        assert "work_items.updated_at <" in sql
        assert "work_items.status in" in sql
        assert "last_activity_at" not in sql
    assert delete_sql.startswith("delete")
    assert session.committed


@pytest.mark.asyncio
async def test_a_0_window_never_purges_a_closed_item(monkeypatch):
    def _no_session():
        raise AssertionError("a disabled retention opened a session")

    monkeypatch.setattr("app.core.database.async_session", _no_session)
    assert await wi_service.purge_terminal_work_items(older_than_days=0) == 0


@pytest.mark.asyncio
async def test_the_hourly_reap_uses_the_admin_windows(monkeypatch):
    monkeypatch.setattr(
        cr,
        "effective_coordination_retention",
        AsyncMock(
            return_value={
                **cr.coordination_retention_days(None),
                "work_item_resolved_days": 0,
                "work_item_abandoned_days": 0,
            }
        ),
    )
    reap = AsyncMock(return_value={"cancelled": 0, "purged": 0})
    monkeypatch.setattr(wi_service, "reap_work_items", reap)
    await autonomous._cleanup_work_items(None)
    assert reap.await_args.kwargs == {"abandon_days": 0, "retain_days": 0}


@pytest.mark.asyncio
async def test_the_shared_state_purge_reads_the_admin_window(monkeypatch):
    monkeypatch.setattr(
        cr,
        "effective_coordination_retention",
        AsyncMock(return_value={**cr.coordination_retention_days(None), "shared_state_resolved_days": 0}),
    )
    purge = AsyncMock(return_value=0)
    monkeypatch.setattr("app.services.shared_state.purge_resolved_states", purge)
    assert await autonomous._cleanup_resolved_shared_states(None) == 0
    purge.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_backlog_past_retention_keeps_the_drain_running(monkeypatch):
    windows = {**cr.coordination_retention_days(None), "work_item_resolved_days": 90, "shared_state_resolved_days": 90}
    monkeypatch.setattr(cr, "effective_coordination_retention", AsyncMock(return_value=windows))
    full = autonomous.COORDINATION_PURGE_BATCH
    monkeypatch.setattr(wi_service, "purge_terminal_work_items", AsyncMock(side_effect=[full, 12]))
    monkeypatch.setattr("app.services.shared_state.purge_resolved_states", AsyncMock(return_value=3))

    purged, more = await autonomous._drain_coordination_retention()
    assert purged == {"work_items": full, "shared_states": 3} and more is True
    purged, more = await autonomous._drain_coordination_retention()
    assert purged == {"work_items": 12, "shared_states": 3} and more is False
