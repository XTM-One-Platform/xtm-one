"""Platform Health snapshot intake (#101 S5, dev-docs/platform-health.md section 5): what a
registration's ``health`` object keeps, bounded, and what it ignores, never refusing a heartbeat."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.health import snapshots


def test_a_snapshot_of_another_format_or_not_an_object_is_ignored() -> None:
    assert snapshots.read_snapshot(None) is None
    assert snapshots.read_snapshot([]) is None
    assert snapshots.read_snapshot({"connectors": []}) is None
    assert snapshots.read_snapshot({"schema_version": 2}) is None


def test_an_oversized_snapshot_is_ignored() -> None:
    raw = {"schema_version": 1, "padding": "x" * (snapshots.MAX_SNAPSHOT_BYTES + 1)}
    assert snapshots.read_snapshot(raw) is None


def test_known_keys_are_kept_bounded_and_unknown_keys_dropped() -> None:
    kept = snapshots.read_snapshot(
        {
            "schema_version": 1,
            "unknown": {"anything": 1},
            "connectors": [
                {
                    "id": "c1",
                    "name": "MITRE",
                    "active": True,
                    "updated_at": "2026-11-20T00:00:00Z",
                    "works_in_error": 12.7,
                    "queue_messages": -4,
                    "secret": "dropped",
                },
                {"name": "no id"},
                "not an object",
                {"id": "c2", "active": "yes", "works_in_error": True, "updated_at": "not a date"},
            ],
            "rabbitmq": {"queue_messages": 150000, "consumers": "two"},
            "search_engine": {"status": "purple", "disk_used_percent": 140},
            "ingestion_lag_seconds": 1200,
        }
    )
    assert kept == {
        "connectors": [
            {
                "id": "c1",
                "name": "MITRE",
                "active": True,
                "works_in_error": 12,
                "queue_messages": 0,
                "updated_at": "2026-11-20T00:00:00+00:00",
            },
            {"id": "c2", "name": "c2"},
        ],
        "rabbitmq": {"queue_messages": 150000},
        "search_engine": {"disk_used_percent": 100.0},
        "ingestion_lag_seconds": 1200,
    }


def test_connectors_past_the_cap_are_dropped() -> None:
    raw = {"schema_version": 1, "connectors": [{"id": f"c{i}"} for i in range(snapshots.MAX_CONNECTORS + 5)]}
    kept = snapshots.read_snapshot(raw)
    assert kept is not None
    assert len(kept["connectors"]) == snapshots.MAX_CONNECTORS


class _Rows:
    def scalar_one_or_none(self) -> None:
        return None


class _Savepoint:
    async def __aenter__(self) -> _Savepoint:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _DB:
    def __init__(self, fail: bool = False) -> None:
        self.added: list[Any] = []
        self.fail = fail

    async def execute(self, *_args: object, **_kwargs: object) -> _Rows:
        return _Rows()

    def begin_nested(self) -> _Savepoint:
        return _Savepoint()

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        if self.fail:
            raise RuntimeError("table missing")


def _registration() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), platform_identifier="opencti")


def _session(db: _DB) -> AsyncSession:
    return cast(AsyncSession, db)


@pytest.mark.asyncio
async def test_a_snapshot_is_stored_under_a_configured_token() -> None:
    db = _DB()
    stored = await snapshots.record_snapshot(
        _session(db), _registration(), {"schema_version": 1, "ingestion_lag_seconds": 5}, published_default=False
    )
    assert stored is True
    assert [row.snapshot for row in db.added] == [{"ingestion_lag_seconds": 5}]


@pytest.mark.asyncio
async def test_nothing_is_stored_under_the_published_default_token_or_without_a_snapshot() -> None:
    db = _DB()
    assert not await snapshots.record_snapshot(
        _session(db), _registration(), {"schema_version": 1}, published_default=True
    )
    assert not await snapshots.record_snapshot(_session(db), _registration(), None, published_default=False)
    assert db.added == []


@pytest.mark.asyncio
async def test_an_unreadable_snapshot_or_a_failed_write_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    assert not await snapshots.record_snapshot(
        _session(_DB()), _registration(), {"schema_version": 9}, published_default=False
    )
    assert "ignored" in caplog.text
    assert not await snapshots.record_snapshot(
        _session(_DB(fail=True)), _registration(), {"schema_version": 1}, published_default=False
    )
    assert "Could not record the health snapshot" in caplog.text
