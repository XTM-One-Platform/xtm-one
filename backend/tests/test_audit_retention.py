"""Audit log retention: the window, its bounds and the batched purge (no database)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.api.v1.settings import ObservabilityConfigUpdate
from app.services import audit_retention
from app.services.audit_retention import (
    DEFAULT_AUDIT_LOG_RETENTION_DAYS,
    audit_log_retention_days,
    audit_purge_cutoff,
    purge_expired_audit_logs,
)

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def test_the_default_window_is_90_days():
    assert DEFAULT_AUDIT_LOG_RETENTION_DAYS == 90
    assert audit_log_retention_days(None) == 90
    assert audit_purge_cutoff(None, NOW) == NOW - timedelta(days=90)  # type: ignore[arg-type]


@pytest.mark.parametrize(("stored", "effective"), [(30, 30), (365, 365), (3650, 3650), (1, 30), (99999, 3650)])
def test_a_stored_window_is_clamped_to_the_bounds(stored, effective):
    assert audit_log_retention_days(stored) == effective


def test_the_cutoff_follows_the_setting():
    assert audit_purge_cutoff(120, NOW) == NOW - timedelta(days=120)


@pytest.mark.parametrize("days", [30, 90, 3650])
def test_the_settings_body_accepts_the_bounds(days):
    body = ObservabilityConfigUpdate(
        trace_storage_enabled=True, trace_retention_days=90, trace_store_payloads=True, audit_log_retention_days=days
    )
    assert body.audit_log_retention_days == days


@pytest.mark.parametrize("days", [0, 29, 3651, -1])
def test_the_settings_body_refuses_a_window_out_of_bounds(days):
    with pytest.raises(ValidationError):
        ObservabilityConfigUpdate(
            trace_storage_enabled=True,
            trace_retention_days=90,
            trace_store_payloads=True,
            audit_log_retention_days=days,
        )


def test_the_settings_body_leaves_the_window_unchanged_when_absent():
    body = ObservabilityConfigUpdate(trace_storage_enabled=True, trace_retention_days=90, trace_store_payloads=True)
    assert body.audit_log_retention_days is None


class _Rows:
    def __init__(self, ids):
        self._ids = ids

    def all(self):
        return [(i,) for i in self._ids]


class _FakeSession:
    """Answers the batch SELECTs from *batches*, records the DELETEs and commits."""

    def __init__(self, batches):
        self._batches = list(batches)
        self.selects: list[dict] = []
        self.deleted: list[list] = []
        self.commits = 0

    async def execute(self, statement, params):
        if "DELETE" in str(statement):
            self.deleted.append(list(params["ids"]))
            return None
        self.selects.append(params)
        return _Rows(self._batches.pop(0) if self._batches else [])

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_the_purge_deletes_in_batches_each_committed(monkeypatch):
    monkeypatch.setattr(audit_retention, "_BATCH_SIZE", 2)
    db = _FakeSession([[1, 2], [3, 4], [5]])

    deleted = await purge_expired_audit_logs(db, 90, now=NOW)  # type: ignore[arg-type]

    assert deleted == 5
    assert db.deleted == [[1, 2], [3, 4], [5]]
    assert db.commits == 3
    assert all(params["cutoff"] == NOW - timedelta(days=90) for params in db.selects)


@pytest.mark.asyncio
async def test_the_purge_is_idempotent_when_nothing_is_past_the_window():
    db = _FakeSession([])

    assert await purge_expired_audit_logs(db, None, now=NOW) == 0  # type: ignore[arg-type]
    assert db.deleted == []
    assert db.commits == 0


@pytest.mark.asyncio
async def test_one_run_stops_at_its_batch_budget(monkeypatch):
    monkeypatch.setattr(audit_retention, "_BATCH_SIZE", 1)
    monkeypatch.setattr(audit_retention, "_MAX_BATCHES_PER_RUN", 2)
    db = _FakeSession([[1], [2], [3]])

    assert await purge_expired_audit_logs(db, 90, now=NOW) == 2  # type: ignore[arg-type]
    assert db.deleted == [[1], [2]]
