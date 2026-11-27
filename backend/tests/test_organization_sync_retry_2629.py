"""#2629: organization syncs retry statement timeouts and throttles; a partial read removes nothing.

Pure checks with the scheduler, the recheck and the link plan stubbed: no
database, no network.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.organization_asset_record import OrganizationPrimaryAssetLink
from app.services import organization_context_opencrq_links as links
from app.services import organization_context_sync as osync


class QueryCanceledError(Exception):
    """asyncpg's name for a statement cancelled by ``statement_timeout``."""


NOW = datetime(2026, 11, 27, 5, 0, tzinfo=UTC)


def _sync(**kwargs: Any) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "section": "assets",
        "source": "github",
        "frequency": "daily",
        "hour": 5,
        "minute": 0,
        "weekday": 0,
        "timezone": "UTC",
        "enabled": True,
        "paused_reason": "",
        "last_result": None,
        "last_run_at": None,
        "next_run_at": NOW,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


@pytest.mark.parametrize(
    "error",
    [
        "canceling statement due to statement timeout",
        "TimeoutError: ",
        "Intune: throttled, retry later",
        "DBAPIError: connection was closed in the middle of operation",
        QueryCanceledError("canceling statement due to statement timeout"),
        TimeoutError(),
    ],
)
def test_timeouts_and_throttles_are_transient(error: Any) -> None:
    assert osync.is_transient(error)


@pytest.mark.parametrize("error", [None, "", "OpenAEV: not connected, or no token", "section must be one of"])
def test_a_missing_token_or_a_refusal_is_not_transient(error: Any) -> None:
    assert not osync.is_transient(error)


def test_a_transient_failure_retries_within_the_hour_then_keeps_the_schedule() -> None:
    sync = _sync()
    at = NOW
    attempts = 0
    retries: list[datetime] = []
    for _ in range(len(osync.RETRY_DELAYS)):
        result = {"error": "statement timeout", "transient": True}
        at = osync.schedule_after(sync, result, prior_attempts=attempts, now=at)
        retries.append(at)
        attempts = result["retry_attempt"]
        assert result["retry_at"] == at.isoformat()
    assert retries[-1] - NOW <= timedelta(hours=1)
    # Every retry spent: the next run is the schedule's.
    result = {"error": "statement timeout", "transient": True}
    assert osync.schedule_after(sync, result, prior_attempts=attempts, now=at) == osync.next_run(sync, at)
    assert "retry_at" not in result


def test_a_success_keeps_the_schedule() -> None:
    sync = _sync()
    assert osync.schedule_after(sync, {"error": None}, prior_attempts=0, now=NOW) == osync.next_run(sync, NOW)


@pytest.mark.asyncio
async def test_a_sync_whose_step_times_out_retries_and_completes_within_the_hour(monkeypatch) -> None:
    sync = _sync()
    outcomes = iter(
        [
            QueryCanceledError("canceling statement due to statement timeout"),
            {"added": 3, "updated": 0, "removed": 0, "version": None, "error": None},
        ]
    )
    commits: list[str] = []

    class _Db:
        async def execute(self, _query: Any) -> Any:
            due = [sync.id] if sync.next_run_at and sync.next_run_at <= clock["now"] else []
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: due))

        async def get(self, _model: Any, _id: Any) -> Any:
            return sync

        async def commit(self) -> None:
            commits.append("commit")

        async def rollback(self) -> None:
            commits.append("rollback")

    async def run_now(_db: Any, row: Any, *, trigger: str) -> dict[str, Any]:
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        row.last_result = dict(outcome)
        return outcome

    monkeypatch.setattr(osync, "run_now", run_now)
    clock = {"now": NOW}
    db = _Db()
    await osync.run_due_syncs(db, now=clock["now"])
    assert sync.last_result["transient"] and "statement timeout" in sync.last_result["error"]
    assert sync.last_result["retry_attempt"] == 1
    assert NOW < sync.next_run_at <= NOW + timedelta(hours=1)
    clock["now"] = sync.next_run_at
    assert await osync.run_due_syncs(db, now=clock["now"]) == 1
    assert sync.last_result["error"] is None and sync.last_result["added"] == 3
    assert sync.next_run_at == osync.next_run(sync, clock["now"])
    assert commits == ["rollback", "commit", "commit"]


@pytest.mark.asyncio
async def test_the_recheck_halves_its_batch_on_a_statement_timeout(monkeypatch) -> None:
    from app.services import ctem_scope_gate_store, organizations

    batches: list[int] = []

    async def recheck_held(_db: Any, *, organization_id: Any, since: Any, batch: int) -> dict[str, int]:
        batches.append(batch)
        if batch > 25:
            raise QueryCanceledError("canceling statement due to statement timeout")
        return {"watchlist": 3}

    async def get_primary(_db: Any) -> Any:
        return SimpleNamespace(id=uuid.uuid4())

    class _Nested:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_exc: Any) -> bool:
            return False

    class _Db:
        async def flush(self) -> None:
            return None

        def begin_nested(self) -> _Nested:
            return _Nested()

    monkeypatch.setattr(ctem_scope_gate_store, "recheck_held", recheck_held)
    monkeypatch.setattr(organizations, "get_primary", get_primary)
    counts = await osync._recheck_held_signals(_Db())
    assert batches == [100, 50, 25] and counts == {"watchlist": 3}


def _stored(external_id: str, pa_key: str) -> OrganizationPrimaryAssetLink:
    return OrganizationPrimaryAssetLink(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        primary_asset_key=pa_key,
        record_id=uuid.uuid4(),
        source="opencrq",
        external_id=external_id,
        roles=["stores"],
        inherited_needs={},
        removed=False,
    )


def test_a_partial_link_read_removes_nothing() -> None:
    stored = [_stored("l1", "payments"), _stored("l2", "billing")]
    plan = links.plan_links({"pa-1": "Payments", "pa-2": "Billing"}, [], {}, {}, stored, complete=False)
    assert plan.delete == []


def test_a_complete_read_removes_only_the_links_of_the_primary_assets_it_read() -> None:
    gone, other = _stored("l1", "payments"), _stored("l2", "billing")
    plan = links.plan_links(
        {"pa-1": "Payments"}, [], {}, {}, [gone, other], complete=True, profile_keys={"payments", "billing"}
    )
    # Billing is still a primary asset of the profile but was not read: its link stays.
    assert plan.delete == [gone.id]


def test_a_link_whose_primary_asset_left_the_profile_is_removed() -> None:
    orphan = _stored("l3", "retired-app")
    plan = links.plan_links({"pa-1": "Payments"}, [], {}, {}, [orphan], complete=True, profile_keys={"payments"})
    assert plan.delete == [orphan.id]
