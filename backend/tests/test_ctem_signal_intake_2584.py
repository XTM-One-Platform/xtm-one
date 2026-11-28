# SPDX-License-Identifier: Apache-2.0

"""#2584: the signal inbox survives an OpenCTI catch-up without taking the web pool.

* repeated updates of one OpenCTI object are folded into the latest one;
* a delete, an OpenAEV delivery or one without an object id is never folded;
* a verified delivery is never throttled; an unsigned one, or one that fails verification, still is;
* the signal is recorded on the intake engine, never the web pool.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

from app.services import ctem_signal_inbox as inbox
from app.services.ctem_signal_listeners import OPENCTI_EXTENSION


def _row(object_id: str, description: str, *, provider: str = "opencti", event_key: str = "update") -> tuple[Any, ...]:
    payload: dict[str, Any] = {
        "data": {
            "type": "vulnerability",
            "description": description,
            "extensions": {OPENCTI_EXTENSION: {"id": object_id}},
        }
    }
    return (uuid.uuid4(), provider, "integration-1", event_key, payload, True, 0)


def test_repeated_updates_of_one_object_fold_into_the_latest() -> None:
    first, other, latest = _row("obj-1", "first"), _row("obj-2", "other"), _row("obj-1", "latest")
    kept, folded = inbox.coalesce([first, other, latest])
    assert [row[4]["data"]["description"] for row in kept] == ["other", "latest"]
    assert folded == [first[0]]


def test_deletes_openaev_and_unidentified_deliveries_are_never_folded() -> None:
    deletes = [_row("obj-1", "a", event_key="delete"), _row("obj-1", "b", event_key="delete")]
    openaev = [_row("obj-1", "a", provider="openaev"), _row("obj-1", "b", provider="openaev")]
    unidentified = [_row("", "a"), _row("", "b")]
    for batch in (deletes, openaev, unidentified):
        kept, folded = inbox.coalesce(batch)
        assert kept == batch and folded == []


class _Db:
    def __init__(self, integration: Any, source: Any) -> None:
        self._integration, self._source = integration, source

    async def __aenter__(self) -> _Db:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, _query: Any) -> Any:
        integration = self._integration

        class _Result:
            def scalar_one_or_none(self) -> Any:
                return integration

        return _Result()

    async def get(self, _model: Any, _key: Any) -> Any:
        return self._source


async def _deliver(monkeypatch: Any, *, signed: bool, verify_fails: bool) -> tuple[list[str], int | None]:
    """One delivery to the signal-source receiver with its gates stubbed: the throttles taken and the error code."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.api.v1 import webhooks

    integration_id, source_id = str(uuid.uuid4()), str(uuid.uuid4())
    integration = SimpleNamespace(id=integration_id, provider="opencti", extra_config_enc="enc")
    source = SimpleNamespace(integration_id=integration_id, enabled=False)
    throttles: list[str] = []

    async def throttle(_request: Any, scope: str, **_kw: Any) -> None:
        throttles.append(scope)

    def verify(*_a: Any, **_kw: Any) -> None:
        if verify_fails:
            raise HTTPException(status_code=401, detail="Invalid OpenCTI token")

    async def body() -> bytes:
        return b"{}"

    monkeypatch.setattr(webhooks, "async_session", lambda: _Db(integration, source))
    monkeypatch.setattr(webhooks, "enforce_rate_limit", throttle)
    monkeypatch.setattr(webhooks, "_try_verify_webhook_signature", verify)
    monkeypatch.setattr(webhooks, "_verify_opencti_token_header", lambda *_a, **_kw: None)
    monkeypatch.setattr(webhooks, "_signed_with_webhook_secret", lambda _enc: signed)
    request = SimpleNamespace(body=body)
    try:
        await webhooks.receive_signal_source_webhook(integration_id, source_id, request, SimpleNamespace())  # type: ignore[arg-type]
    except HTTPException as exc:
        return throttles, exc.status_code
    return throttles, None


async def test_a_verified_delivery_is_never_throttled(monkeypatch: Any) -> None:
    assert await _deliver(monkeypatch, signed=True, verify_fails=False) == ([], None)


async def test_an_unsigned_delivery_is_still_throttled(monkeypatch: Any) -> None:
    assert await _deliver(monkeypatch, signed=False, verify_fails=False) == (["webhook-signal-source"], None)


async def test_a_delivery_that_fails_verification_is_throttled_then_refused(monkeypatch: Any) -> None:
    # Review of #2592: a bad token must not escape the per-source limit.
    assert await _deliver(monkeypatch, signed=True, verify_fails=True) == (["webhook-signal-source"], 401)


def test_the_intake_engine_is_its_own_pool() -> None:
    from app.core import database

    session = database.intake_session()
    try:
        bind: Any = session.bind
        assert bind is not database.engine
        assert bind.pool.size() >= 1
        assert bind.pool._max_overflow == 0
    finally:
        session.sync_session.close()


def test_a_growing_inbox_is_logged_once_per_growth_step(monkeypatch: Any, caplog: Any) -> None:
    # #2584 point 4: a log line when the inbox depth grows, never one per tick.
    monkeypatch.setattr(inbox, "_last_logged_depth", 0)
    with caplog.at_level("WARNING", logger=inbox.__name__):
        assert inbox.note_depth(inbox.DEPTH_WARNING - 1) is False
        assert inbox.note_depth(inbox.DEPTH_WARNING) is True
        assert inbox.note_depth(inbox.DEPTH_WARNING + 10) is False
        assert inbox.note_depth(2 * inbox.DEPTH_WARNING) is True
        assert inbox.note_depth(10) is False
        assert inbox.note_depth(inbox.DEPTH_WARNING) is True
    lines = [r.getMessage() for r in caplog.records if "signal inbox" in r.getMessage()]
    assert len(lines) == 3
    assert f"{2 * inbox.DEPTH_WARNING} verified deliveries waiting" in lines[1]


async def test_a_tick_drains_batch_after_batch_until_the_inbox_is_empty(monkeypatch: Any) -> None:
    # #2584 gate line 2: one batch per tick drained a 10,000-update catch-up in about 50 minutes.
    batches = [{"recorded": 150, "retried": 0, "dropped": 0, "coalesced": 50}] * 3
    calls: list[int] = []

    async def drain(limit: int, *, deadline: float | None = None) -> dict[str, int]:
        calls.append(limit)
        return dict(batches[len(calls) - 1]) if len(calls) <= len(batches) else dict.fromkeys(batches[0], 0)

    monkeypatch.setattr(inbox, "drain", drain)
    totals = await inbox.drain_within(60)
    assert len(calls) == 4
    assert totals == {"recorded": 450, "retried": 0, "dropped": 0, "coalesced": 150, "deferred": 0}


async def test_a_tick_stops_at_its_budget_and_on_retries_alone(monkeypatch: Any) -> None:
    calls: list[int] = []

    async def busy(limit: int, *, deadline: float | None = None) -> dict[str, int]:
        calls.append(limit)
        return {"recorded": 200, "retried": 0, "dropped": 0, "coalesced": 0}

    monkeypatch.setattr(inbox, "drain", busy)
    await inbox.drain_within(0)
    assert len(calls) == 1

    calls.clear()

    async def failing(limit: int, *, deadline: float | None = None) -> dict[str, int]:
        calls.append(limit)
        return {"recorded": 0, "retried": 200, "dropped": 0, "coalesced": 0}

    monkeypatch.setattr(inbox, "drain", failing)
    assert (await inbox.drain_within(60))["retried"] == 200
    assert len(calls) == 1


class _InboxSession:
    """The inbox table as the drain sees it: a select answers the rows, an update or delete is recorded."""

    def __init__(self, rows: list[Any], log: list[str]) -> None:
        self.rows = rows
        self.log = log

    async def __aenter__(self) -> _InboxSession:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def execute(self, statement: Any) -> Any:
        kind = type(statement).__name__.lower()
        self.log.append(kind)
        rows = self.rows
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    async def commit(self) -> None:
        return None


async def test_a_batch_stops_at_its_deadline_row_by_row_and_leaves_the_rest(monkeypatch: Any) -> None:
    # #2774: a 200-row batch whose recorder is slow per row returns within the budget plus one row.
    import time

    from app.core import database

    rows = [
        SimpleNamespace(
            id=i, provider="openaev", integration_id="i", event_key="update", payload={}, verified=True, attempts=0
        )
        for i in range(200)
    ]
    log: list[str] = []
    deleted: list[int] = []
    given_back: list[list[int]] = []
    monkeypatch.setattr(database, "async_session", lambda: _InboxSession(rows, log))

    async def slow_record(*_a: Any, **_kw: Any) -> bool:
        await asyncio.sleep(0.01)
        return True

    async def delete(row_id: int) -> None:
        deleted.append(row_id)

    async def give_back(row_ids: list[int]) -> None:
        given_back.append(list(row_ids))

    monkeypatch.setattr(inbox, "record", slow_record)
    monkeypatch.setattr(inbox, "_delete", delete)
    monkeypatch.setattr(inbox, "_give_back", give_back)
    budget = 0.2
    started = time.monotonic()
    totals = await inbox.drain_within(deadline=started + budget)
    took = time.monotonic() - started
    assert took < budget + 0.15, took
    assert 0 < totals["recorded"] < 200
    assert totals["recorded"] + totals["deferred"] == 200
    assert deleted == list(range(totals["recorded"]))
    assert given_back == [list(range(totals["recorded"], 200))]


async def test_a_drain_without_deadline_records_the_whole_batch(monkeypatch: Any) -> None:
    from app.core import database

    rows = [
        SimpleNamespace(
            id=i, provider="openaev", integration_id="i", event_key="update", payload={}, verified=True, attempts=0
        )
        for i in range(5)
    ]
    monkeypatch.setattr(database, "async_session", lambda: _InboxSession(rows, []))

    async def record(*_a: Any, **_kw: Any) -> bool:
        return True

    async def delete(_row_id: int) -> None:
        return None

    monkeypatch.setattr(inbox, "record", record)
    monkeypatch.setattr(inbox, "_delete", delete)
    counts = await inbox.drain(5)
    assert counts["recorded"] == 5 and counts["deferred"] == 0
