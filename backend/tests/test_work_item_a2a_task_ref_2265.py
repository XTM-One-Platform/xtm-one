"""A work item waits for the end of an outbound A2A task (#2265).

Unit level: the ``a2a_task`` reference normaliser (no repository, a task id,
release_on completed or terminal, terminal by default), its label and satisfied
states, and the pure recording of a delegation's end. PostgreSQL level (marked
``integration``): an item blocked on a running task stays blocked, is released
when the task completes, a ``completed`` wait is handed back with the outcome
when the task fails, and a task id the user has no delegation for is refused at
the write.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from app.services.work_item_a2a_refs import a2a_task_ids, apply_outcomes, end_of
from app.services.work_item_blocked_refs import (
    REF_KINDS,
    BlockedRefError,
    all_satisfied,
    normalize_blocked_on_refs,
    ref_label,
)

NOW = datetime(2026, 11, 25, 17, 30, tzinfo=UTC)


def test_the_kind_is_listed() -> None:
    assert "a2a_task" in REF_KINDS


def test_an_a2a_task_reference_needs_no_repository_and_defaults_to_terminal() -> None:
    [ref] = normalize_blocked_on_refs([{"kind": "a2a_task", "task": " task-1 "}])
    assert ref == {"kind": "a2a_task", "task": "task-1", "release_on": "terminal"}
    assert ref_label(ref) == "A2A task task-1"


@pytest.mark.parametrize(
    "entry", [{"kind": "a2a_task"}, {"kind": "a2a_task", "task": "a b"}, {"kind": "a2a_task", "task": 3}]
)
def test_an_a2a_task_reference_names_its_task(entry: dict) -> None:
    with pytest.raises(BlockedRefError, match="collect_task_id"):
        normalize_blocked_on_refs([entry])


def test_an_a2a_task_is_released_on_completed_or_terminal_only() -> None:
    with pytest.raises(BlockedRefError, match="completed or terminal"):
        normalize_blocked_on_refs([{"kind": "a2a_task", "task": "t", "release_on": "merged"}])
    with pytest.raises(BlockedRefError, match='needs kind "a2a_task"'):
        normalize_blocked_on_refs([{"repo": "o/n", "kind": "pull_request", "number": 1, "release_on": "completed"}])


def test_the_same_task_twice_is_one_reference() -> None:
    refs = normalize_blocked_on_refs([{"kind": "a2a_task", "task": "t"}, {"kind": "a2a_task", "task": "t"}])
    assert len(refs) == 1


def test_a_running_task_leaves_the_reference_open() -> None:
    refs = normalize_blocked_on_refs([{"kind": "a2a_task", "task": "t"}])
    out, released = apply_outcomes(refs, {"t": None}, NOW)
    assert released == [] and not all_satisfied(out)
    assert a2a_task_ids(out) == ["t"]


def test_a_completed_task_meets_both_waits() -> None:
    for release_on in ("completed", "terminal"):
        refs = normalize_blocked_on_refs([{"kind": "a2a_task", "task": "t", "release_on": release_on}])
        out, released = apply_outcomes(refs, {"t": "completed"}, NOW)
        assert all_satisfied(out)
        assert out[0]["state"] == "completed" and out[0]["outcome"] == "completed"
        assert released == ["A2A task t completed"]


def test_a_failed_task_ends_the_wait_and_names_the_outcome() -> None:
    refs = normalize_blocked_on_refs([{"kind": "a2a_task", "task": "t", "release_on": "completed"}])
    out, released = apply_outcomes(refs, {"t": "partner_failed"}, NOW)
    assert all_satisfied(out)
    assert out[0]["state"] == "ended"
    assert "ended partner_failed, not completed" in released[0]
    assert a2a_task_ids(out) == []


def test_the_end_of_an_outcome() -> None:
    assert end_of(None) is None
    assert end_of("completed") == "completed"
    assert end_of("cancelled") == "ended"


def test_the_recorded_end_is_kept_on_a_rewrite() -> None:
    [ref] = normalize_blocked_on_refs(
        [{"kind": "a2a_task", "task": "t", "state": "ended", "at": NOW.isoformat(), "outcome": "timeout"}]
    )
    assert ref["state"] == "ended" and ref["outcome"] == "timeout"


# -- Against PostgreSQL ---------------------------------------------------------------


@asynccontextmanager
async def _database() -> AsyncIterator:
    import app.models  # noqa: F401 - every table, for ALL_TABLES
    from tests.scratch_database import ALL_TABLES, scratch_database

    async with scratch_database("a2aref", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _seed(sessions):
    from app.models.a2a import A2ADelegation
    from app.models.agent import Agent
    from app.models.integration import Integration
    from app.models.user import User
    from tests.work_item_owner import add_owner

    user = User(id=uuid.uuid4(), email=f"a2a-{uuid.uuid4().hex[:8]}@test.local", full_name="M", hashed_password="x")
    async with sessions() as db:
        db.add(user)
        await db.flush()
        agent = Agent(id=uuid.uuid4(), name="Caller", user_id=user.id)
        partner = Integration(id=uuid.uuid4(), provider="oauth_a2a", name=f"p-{uuid.uuid4().hex[:6]}", user_id=user.id)
        db.add_all([agent, partner])
        await db.flush()
        rows = {}
        for name in ("ok", "bad"):
            row = A2ADelegation(
                id=uuid.uuid4(),
                integration_id=partner.id,
                agent_id=agent.id,
                user_id=user.id,
                partner_task_id=f"task-{name}-{uuid.uuid4().hex[:6]}",
                protocol_version="0.3",
                signature_state="unsigned",
            )
            db.add(row)
            rows[name] = row
        await db.commit()
    await add_owner(sessions, user.id)
    return user, rows


async def _end(sessions, row_id, outcome: str) -> None:
    from app.models.a2a import A2ADelegation

    async with sessions() as db:
        row = await db.get(A2ADelegation, row_id)
        row.outcome = outcome
        row.terminal_at = datetime.now(UTC)
        await db.commit()


@pytest.mark.integration
async def test_an_item_waits_for_its_a2a_task_and_is_released_at_its_end():
    from app.services import work_item as wi_service
    from app.services.company_managed_writes import person_writer
    from app.services.work_item_a2a_refs import release_ended_a2a_tasks
    from tests.work_item_owner import owner_of

    writer = person_writer(is_admin=False)
    async with _database() as sessions:
        user, rows = await _seed(sessions)

        async def blocked(task: str, release_on: str) -> dict:
            return await wi_service.create_work_item(
                user_id=str(user.id),
                title=f"Waits for {task}",
                description="the brief",
                owner_agent_id=owner_of(user.id),
                blocked_on_refs=[{"kind": "a2a_task", "task": task, "release_on": release_on}],
                writer=writer,
            )

        ok = await blocked(rows["ok"].partner_task_id, "completed")
        bad = await blocked(rows["bad"].partner_task_id, "completed")
        assert ok["status"] == "blocked"

        assert await release_ended_a2a_tasks() == 0
        assert (await wi_service.get_work_item(ok["id"], user_id=str(user.id)))["status"] == "blocked"

        await _end(sessions, rows["ok"].id, "completed")
        await _end(sessions, rows["bad"].id, "partner_failed")
        assert await release_ended_a2a_tasks() == 2

        released = await wi_service.get_work_item(ok["id"], user_id=str(user.id))
        assert released["status"] == "open"
        assert "completed" in released["description"].split("\n", 1)[0]
        handed_back = await wi_service.get_work_item(bad["id"], user_id=str(user.id))
        assert handed_back["status"] == "open"
        assert "partner_failed, not completed" in handed_back["description"].split("\n", 1)[0]

        with pytest.raises(BlockedRefError, match="no outbound A2A delegation"):
            await blocked("task-nobody-sent", "terminal")
