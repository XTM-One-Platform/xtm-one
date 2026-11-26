"""The lifecycle of IMPLICIT worksets against a real PostgreSQL.

An implicit workset is the corpus the runtime derives for an agent's chat or an
assignment, never one a flow node names. It lives as long as its agent, so it
gets what an explicit corpus gets from its run or its builder node: an idle
expiry renewed on use, a rolling automatic-summarisation budget instead of a
lifetime ceiling, rotation once resolved or sealed, and ``workset_resolve``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 — registers every mapper on Base.metadata
from app.core.config import settings
from app.models.user import User
from app.models.workset import Workset, WorksetItem
from app.services.builtin_tools import build_workset_tools
from app.services.workset import (
    add_item,
    budget_spent,
    resolve,
    resolve_workset,
    sweep_worksets,
)
from app.services.workset_read import coverage_of
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("wsimplicit", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(settings, "workset_implicit_idle_days", 30)
    monkeypatch.setattr(settings, "workset_implicit_budget_window_hours", 24)

    async def _no_enqueue(*args, **kwargs):
        del args, kwargs
        return True

    monkeypatch.setattr("app.services.kb_helpers.enqueue_document_processing", _no_enqueue)
    monkeypatch.setattr("app.services.workset_clustering.enqueue_clustering", _no_enqueue)


async def _seed_user(session) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"wsi-{uuid.uuid4().hex[:8]}@test.local",
        full_name="Implicit Workset Test",
        hashed_password="x",
    )
    session.add(user)
    await session.flush()
    return user


async def _call(executor, name, **arguments) -> str:
    from app.tools.base import ToolCall

    result = await executor.execute_tool(ToolCall(id="t", name=name, arguments=arguments))
    return str(result.content)


async def _rows(session, user_id) -> list[Workset]:
    return list(
        (await session.execute(select(Workset).where(Workset.user_id == user_id).order_by(Workset.created_at)))
        .scalars()
        .all()
    )


# ── idle expiry ──────────────────────────────────────────────────────────────


async def test_an_implicit_workset_carries_an_idle_expiry_renewed_on_use():
    async with _temp_database() as factory, factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="assignment-1", scope="global", implicit=True)
        await session.commit()
        assert ws.implicit is True
        assert ws.expires_at is not None
        first_expiry = ws.expires_at
        assert timedelta(days=29) < first_expiry - datetime.now(UTC) <= timedelta(days=30)

        ws.expires_at = datetime.now(UTC) + timedelta(days=1)
        await session.commit()
        again = await resolve_workset(session, user, key="assignment-1", scope="global", implicit=True)
        assert again.id == ws.id
        assert again.expires_at - datetime.now(UTC) > timedelta(days=29)


async def test_the_sweep_purges_an_idle_implicit_workset_and_spares_a_flow_corpus():
    async with _temp_database() as factory:
        async with factory() as session:
            user = await _seed_user(session)
            idle = await resolve_workset(session, user, key="agent-idle", scope="global", implicit=True)
            await add_item(session, idle, user, title="R", content="A" * 200, summary="s", priority=10)
            idle.expires_at = datetime.now(UTC) - timedelta(hours=1)
            flow = await resolve_workset(session, user, key="recurring", scope="flow", scope_ref="flow-1")
            await session.commit()
            idle_id, flow_id = idle.id, flow.id
            assert flow.expires_at is None

        purged = await sweep_worksets(resolved_after=timedelta(days=7))
        assert purged == {"resolved": 0, "expired": 1}
        async with factory() as session:
            assert await session.get(Workset, idle_id) is None
            assert await session.get(Workset, flow_id) is not None


async def test_an_idle_expiry_of_zero_keeps_an_implicit_workset(monkeypatch):
    monkeypatch.setattr(settings, "workset_implicit_idle_days", 0)
    async with _temp_database() as factory, factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="assignment-2", scope="global", implicit=True)
        assert ws.expires_at is None


# ── rolling budget, answered at write time ───────────────────────────────────


async def test_a_spent_implicit_budget_stores_the_next_item_with_a_warning():
    # #2206: a to_workset diversion cannot retry or pass a summary, so a refusal
    # dropped a tool result the caller never saw. The item is stored, the
    # warning says the budget is spent, when it comes back and how to finish it.
    async with _temp_database() as factory, factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="assignment-3", scope="global", implicit=True, max_auto_calls=1)
        ws.auto_calls_used = 1
        ws.auto_calls_window_started_at = datetime.now(UTC)
        await session.commit()

        outcome = await add_item(session, ws, user, title="New", content="A" * 300, external_ref="n-1")
        assert outcome.ok
        assert outcome.action != "refused"
        assert outcome.warning is not None
        assert "budget is spent" in outcome.warning
        assert "replenishes at" in outcome.warning
        assert "workset_set_summary" in outcome.warning
        await session.commit()
        stored = (await session.execute(select(WorksetItem).where(WorksetItem.workset_id == ws.id))).scalars().all()
        assert [item.external_ref for item in stored] == ["n-1"], "the content is kept, never dropped"

        # One that needs no automatic call is still accepted.
        own = await add_item(
            session, ws, user, title="Own", content="B" * 300, external_ref="n-2", summary="Done.", priority=40
        )
        assert own.ok

        coverage = await coverage_of(session, ws)
        assert coverage.auto_budget_exhausted is True
        assert coverage.auto_budget_resets_at is not None
        assert "replenishes at" in coverage.describe()


async def test_the_rolling_budget_is_replenished_after_its_window():
    async with _temp_database() as factory, factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="assignment-4", scope="global", implicit=True, max_auto_calls=1)
        ws.auto_calls_used = 1
        ws.auto_calls_window_started_at = datetime.now(UTC) - timedelta(hours=25)
        await session.commit()

        assert budget_spent(ws) is False
        outcome = await add_item(session, ws, user, title="Next day", content="A" * 300, external_ref="d-2")
        assert outcome.ok
        assert outcome.warning is None
        await session.commit()
        await session.refresh(ws)
        assert ws.auto_calls_used == 0
        assert ws.auto_calls_window_started_at > datetime.now(UTC) - timedelta(minutes=5)


async def test_the_pass_charges_a_fresh_window_once_the_old_one_ran_out():
    from app.services.workset_processing import _charge_call

    async with _temp_database() as factory, factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="assignment-5", scope="global", implicit=True, max_auto_calls=1)
        ws.auto_calls_used = 1
        ws.auto_calls_window_started_at = datetime.now(UTC) - timedelta(days=2)
        await session.commit()

        assert await _charge_call(session, ws) is None
        assert ws.auto_calls_used == 1
        refused = await _charge_call(session, ws)
        assert refused is not None and "replenishes at" in refused


async def test_an_explicit_workset_keeps_its_lifetime_ceiling_and_warns_at_write_time():
    async with _temp_database() as factory, factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="flow-corpus", scope="flow", scope_ref="f-1", max_auto_calls=1)
        ws.auto_calls_used = 1
        ws.auto_calls_window_started_at = datetime.now(UTC) - timedelta(days=30)
        await session.commit()

        assert budget_spent(ws) is True
        outcome = await add_item(session, ws, user, title="Late", content="A" * 300, external_ref="l-1")
        assert outcome.ok, "an explicit corpus keeps what its caller adds"
        assert outcome.warning and "budget is spent" in outcome.warning
        coverage = await coverage_of(session, ws)
        assert coverage.auto_budget_resets_at is None


# ── rotation ─────────────────────────────────────────────────────────────────


async def test_a_resolved_implicit_workset_rotates_on_the_next_ingest():
    async with _temp_database() as factory, factory() as session:
        user = await _seed_user(session)
        old = await resolve_workset(session, user, key="agent-7", scope="global", implicit=True)
        await add_item(session, old, user, title="Old", content="A" * 300, summary="s", priority=1)
        await resolve(session, old)
        await session.commit()
        old_id = old.id

        fresh = await resolve_workset(session, user, key="agent-7", scope="global", implicit=True, ingest=True)
        await session.commit()
        assert fresh.id != old_id
        assert fresh.status == "active"
        assert fresh.key == "agent-7"
        retired = await session.get(Workset, old_id)
        assert retired.key.startswith("agent-7~")
        assert retired.status == "resolved"

        # A read on the implicit key reads the fresh generation, and a write of
        # a summary (no ingest) never rotates.
        same = await resolve_workset(session, user, key="agent-7", scope="global", implicit=True)
        assert same.id == fresh.id


async def test_a_sealed_implicit_workset_rotates_on_ingest_only():
    async with _temp_database() as factory, factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="assignment-8", scope="global", implicit=True)
        ws.sealed_at = datetime.now(UTC)
        await session.commit()
        sealed_id = ws.id

        summary_write = await resolve_workset(session, user, key="assignment-8", scope="global", implicit=True)
        assert summary_write.id == sealed_id
        fresh = await resolve_workset(session, user, key="assignment-8", scope="global", implicit=True, ingest=True)
        await session.commit()
        assert fresh.id != sealed_id
        retired = await session.get(Workset, sealed_id)
        assert retired.status == "resolved" and retired.resolved_at is not None


async def test_an_explicit_resolved_workset_is_never_rotated():
    async with _temp_database() as factory, factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="named", scope="global")
        await resolve(session, ws)
        await session.commit()
        again = await resolve_workset(session, user, key="named", scope="global")
        assert again.id == ws.id


# ── workset_resolve ──────────────────────────────────────────────────────────


def test_workset_resolve_is_offered_on_an_implicit_corpus_only():
    implicit_defs, implicit_exec = build_workset_tools(None, "u", None, implicit=True)
    explicit_defs, _ = build_workset_tools(None, "u", None)
    assert "workset_resolve" in {d.name for d in implicit_defs}
    assert "workset_resolve" not in {d.name for d in explicit_defs}
    assert implicit_exec.can_handle("workset_resolve") if hasattr(implicit_exec, "can_handle") else True


async def test_workset_resolve_closes_the_agents_corpus_and_the_next_write_opens_a_new_one():
    async with _temp_database() as factory:
        async with factory() as session:
            user = await _seed_user(session)
            await session.commit()
            user_id = user.id

        _defs, executor = build_workset_tools(
            None, str(user_id), None, workset_key="assignment-9", scope="global", implicit=True
        )
        added = await _call(
            executor, "workset_add_item", title="First", content="A" * 300, external_ref="a", summary="s", priority=5
        )
        assert "Added" in added
        resolved = await _call(executor, "workset_resolve", note="weekly digest sent")
        assert "Resolved" in resolved

        async with factory() as session:
            (first,) = await _rows(session, user_id)
            assert first.status == "resolved"
            first_id = first.id

        # The next run's write lands in a new generation.
        again = await _call(
            executor, "workset_add_item", title="Second", content="B" * 300, external_ref="b", summary="s", priority=5
        )
        assert "Added" in again
        async with factory() as session:
            rows = await _rows(session, user_id)
            assert len(rows) == 2
            live = next(r for r in rows if r.id != first_id)
            assert live.key == "assignment-9" and live.status == "active"
            items = (
                (await session.execute(select(WorksetItem).where(WorksetItem.workset_id == live.id))).scalars().all()
            )
            assert [i.title for i in items] == ["Second"]
