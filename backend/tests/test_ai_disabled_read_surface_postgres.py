"""The read surface and the no-loss guarantee, against real PostgreSQL.

T036/T037/T037a. Three things no unit test reaches:

* with real data present and AI disabled, AI **reads** still return their rows
  (FR-008) — the browsable surface the whole mode exists for;
* the refusal is the **same payload** through a non-interactive path as through
  a browser (FR-010), so an integration is told the same thing a user is;
* disabling AI **destroys nothing**, and re-enabling needs no manual repair
  (FR-025). Distinct from the toggle-cycle test in
  ``test_ai_toggle_postgres.py``, which asserts no *duplication* — this asserts
  no *loss*, and FR-025 had no task at all until ``/speckit-analyze`` found it.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours. The temporary database is the shared ``tests/scratch_database.py``,
on the same terms as ``test_ai_toggle_postgres.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.agent import Agent
from app.models.flow import Flow
from app.models.knowledge_base import KnowledgeBase
from app.models.platform_settings import PlatformSettings
from app.models.user import User
from app.services import ai_state
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database(prefix: str) -> AsyncIterator[async_sessionmaker]:
    """Isolated database with the full schema, dropped on exit."""
    async with scratch_database(
        prefix, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        yield db.sessions


async def _seed_ai_data(session: async_sessionmaker) -> dict[str, int]:
    """Existing AI content, as a platform that ran with AI on would have.

    Counted rather than merely created: SC-004 is a percentage, and "no
    exception was raised" is not evidence that rows survived.
    """
    async with session() as db:
        owner = User(
            email=f"owner-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",
            full_name="Owner",
            role="admin",
        )
        db.add(owner)
        await db.flush()

        db.add_all(
            [
                Agent(
                    name=f"agent-{i}",
                    slug=f"agent-{uuid.uuid4().hex[:8]}",
                    user_id=owner.id,
                    persona="p",
                )
                for i in range(3)
            ]
        )
        db.add_all(
            [
                KnowledgeBase(name=f"kb-{i}", user_id=owner.id)
                for i in range(2)
            ]
        )
        # One AI-bearing flow and one deterministic one, so the no-loss check
        # covers both sides of the US5 classification.
        db.add(
            Flow(
                name="ai-flow",
                user_id=owner.id,
                graph={"nodes": [{"id": "n0", "kind": "agent", "config": {}}]},
            )
        )
        db.add(
            Flow(
                name="deterministic-flow",
                user_id=owner.id,
                graph={
                    "nodes": [{"id": "n0", "kind": "action.notify", "config": {}}]
                },
            )
        )
        db.add(PlatformSettings())
        await db.commit()

    async with session() as db:
        return {
            "agents": (
                await db.execute(select(func.count()).select_from(Agent))
            ).scalar_one(),
            "knowledge_bases": (
                await db.execute(select(func.count()).select_from(KnowledgeBase))
            ).scalar_one(),
            "flows": (
                await db.execute(select(func.count()).select_from(Flow))
            ).scalar_one(),
        }


async def _counts(session: async_sessionmaker) -> dict[str, int]:
    async with session() as db:
        return {
            "agents": (
                await db.execute(select(func.count()).select_from(Agent))
            ).scalar_one(),
            "knowledge_bases": (
                await db.execute(select(func.count()).select_from(KnowledgeBase))
            ).scalar_one(),
            "flows": (
                await db.execute(select(func.count()).select_from(Flow))
            ).scalar_one(),
        }


@pytest.mark.asyncio
async def test_disabling_ai_destroys_nothing_and_needs_no_repair(monkeypatch):
    """FR-025 / T037a: no *loss*, as distinct from no *duplication*.

    Capture what exists, disable AI, confirm every row still reads, re-enable,
    confirm it is all still there. The re-enable half matters as much as the
    disable half: "enabling provisions the built-in content" must not mean
    "enabling rewrites what was already there".
    """
    async with _temp_database("ai_read_surface") as session:
        before = await _seed_ai_data(session)
        assert before == {"agents": 3, "knowledge_bases": 2, "flows": 2}

        async with session() as db:
            ps = (await db.execute(select(PlatformSettings))).scalar_one()

            # Disabled explicitly, the way the toggle writes it.
            ai_state.invalidate_cache()
            await ai_state.set_ai_enabled(db, False)
            await db.commit()
            assert await ai_state.is_ai_enabled(db) is False

        during = await _counts(session)
        assert during == before, "disabling AI removed rows"

        # Every row still *reads* — the browsable surface (FR-008).
        async with session() as db:
            agents = (await db.execute(select(Agent))).scalars().all()
            kbs = (await db.execute(select(KnowledgeBase))).scalars().all()
            flows = (await db.execute(select(Flow))).scalars().all()
            assert len(agents) == 3
            assert len(kbs) == 2
            assert {f.name for f in flows} == {"ai-flow", "deterministic-flow"}

        async with session() as db:
            ai_state.invalidate_cache()
            await ai_state.set_ai_enabled(db, True)
            await db.commit()
            assert await ai_state.is_ai_enabled(db) is True

        after = await _counts(session)
        assert after == before, "re-enabling AI changed the existing rows"
        _ = ps


@pytest.mark.asyncio
async def test_the_refusal_is_identical_however_it_is_reached():
    """FR-010 / T037: an API token and an integration get the browser's answer.

    The refusal is built in one place, so this asserts the *payload* rather than
    each caller — a vaguer error for the non-interactive path is precisely what
    FR-010 forbids, and a second copy of the dict is how that happens.
    """
    from app.services.agentic_gate import refuse_ai_disabled

    payloads = []
    for _ in range(2):
        with pytest.raises(HTTPException) as exc:
            refuse_ai_disabled()
        payloads.append((exc.value.status_code, exc.value.detail))

    assert payloads[0] == payloads[1]
    status_code, detail = payloads[0]
    assert status_code == 403
    assert detail["code"] == "ai_disabled"
    assert isinstance(detail.get("message"), str) and detail["message"]


@pytest.mark.asyncio
async def test_the_stored_override_survives_a_full_cycle():
    """The tri-state must come back as it went in (FR-003).

    ``None`` is not ``False``: a cycle that collapses it pins the platform to
    disabled and hides every later change to ``PLATFORM_MODE``. Asserted
    against the real column, because that collapse is a database-level default
    away.
    """
    async with _temp_database("ai_tristate") as session:
        async with session() as db:
            db.add(PlatformSettings())
            await db.commit()

        for value in (True, False, None):
            async with session() as db:
                ai_state.invalidate_cache()
                await ai_state.set_ai_enabled(db, value)
                await db.commit()

            async with session() as db:
                ps = (await db.execute(select(PlatformSettings))).scalar_one()
                assert ai_state.override_to_wire(ps.ai_disabled) is value, value


@pytest.mark.asyncio
async def test_null_follows_the_mode_and_an_explicit_choice_overrides_it(monkeypatch):
    """Quickstart Scenario 3 — the one whose failure has no visible symptom.

    Nothing in the interface looks wrong if this breaks: the platform simply
    stops honouring ``PLATFORM_MODE``, or honours it when an administrator has
    said otherwise. Four steps, in order:

    1. never touched -> stored ``NULL``
    2. ``NULL`` follows ``PLATFORM_MODE``, both ways
    3. an explicit choice **overrides** the mode
    4. reset restores ``NULL``, and following resumes

    Step 4 is the one that catches a client sending ``false`` for "reset": that
    pins the platform and hides every later change to the deployment default.
    """
    async with _temp_database("ai_scenario3") as session:
        async with session() as db:
            db.add(PlatformSettings())
            await db.commit()

        # 1. Untouched.
        async with session() as db:
            ps = (await db.execute(select(PlatformSettings))).scalar_one()
            assert ps.ai_disabled is None, "a fresh install must store NULL"

        # 2. NULL follows the mode, both directions.
        for mode, expected in (("ai_disabled", False), ("xtm_one", True)):
            monkeypatch.setattr(ai_state.settings, "platform_mode", mode)
            ai_state.invalidate_cache()
            async with session() as db:
                assert await ai_state.is_ai_enabled(db) is expected, mode

        # 3. An explicit choice wins, and keeps winning across a mode change.
        async with session() as db:
            await ai_state.set_ai_enabled(db, False)
            await db.commit()
        for mode in ("xtm_one", "ai_disabled", "copilot"):
            monkeypatch.setattr(ai_state.settings, "platform_mode", mode)
            ai_state.invalidate_cache()
            async with session() as db:
                assert await ai_state.is_ai_enabled(db) is False, (
                    f"the explicit choice must survive PLATFORM_MODE={mode}"
                )

        # 4. Reset — NULL, not False — and following resumes.
        async with session() as db:
            await ai_state.set_ai_enabled(db, None)
            await db.commit()
        async with session() as db:
            ps = (await db.execute(select(PlatformSettings))).scalar_one()
            assert ps.ai_disabled is None, "reset must clear, not write False"

        for mode, expected in (("xtm_one", True), ("ai_disabled", False)):
            monkeypatch.setattr(ai_state.settings, "platform_mode", mode)
            ai_state.invalidate_cache()
            async with session() as db:
                assert await ai_state.is_ai_enabled(db) is expected, (
                    f"after reset the platform must follow PLATFORM_MODE={mode}"
                )
