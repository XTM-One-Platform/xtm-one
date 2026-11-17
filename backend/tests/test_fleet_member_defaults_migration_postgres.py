"""The ``r9162mbrdflt1`` revision, executed against PostgreSQL (decision R-D1).

Per fleet, the most common member value becomes the fleet default and only
the differing rows keep an override. The test proves the effective value of
every row is identical before and after, then that the downgrade restores the
raw values. The revision is loaded from its file: a migration keeps meaning
what it meant on the day it ran.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models.agent import Agent
from app.models.fleet import Fleet, FleetMember
from app.models.user import User
from app.models.workspace import Workspace
from tests.scratch_database import scratch_database

pytestmark = pytest.mark.integration

_REVISION = next((Path(__file__).resolve().parents[1] / "alembic" / "versions").glob("r9162mbrdflt1_*.py"))
_TABLES = [User.__table__, Workspace.__table__, Agent.__table__, Fleet.__table__, FleetMember.__table__]


def _load_revision():
    spec = importlib.util.spec_from_file_location("r9162mbrdflt1", _REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


revision = _load_revision()


def _run(sync_conn, step) -> None:
    with Operations.context(MigrationContext.configure(sync_conn)):
        step()


async def _migrate(engine: AsyncEngine, step) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(_run, step)


async def _as_before_the_revision(engine: AsyncEngine) -> None:
    """The schema before the revision: NOT NULL member fields, no fleet defaults."""
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE fleets DROP CONSTRAINT IF EXISTS ck_fleets_default_context_inclusion"))
        await conn.execute(text("ALTER TABLE fleets DROP COLUMN default_context_inclusion"))
        await conn.execute(text("ALTER TABLE fleets DROP COLUMN default_collaboration_instruction"))
        await conn.execute(text("ALTER TABLE fleet_members ALTER COLUMN context_inclusion SET DEFAULT 'none'"))
        await conn.execute(text("ALTER TABLE fleet_members ALTER COLUMN context_inclusion SET NOT NULL"))
        await conn.execute(text("ALTER TABLE fleet_members ALTER COLUMN collaboration_instruction SET DEFAULT ''"))
        await conn.execute(text("ALTER TABLE fleet_members ALTER COLUMN collaboration_instruction SET NOT NULL"))


async def _seed(engine: AsyncEngine) -> dict[str, uuid.UUID]:
    """Three fleets: a clear majority, a tie, a single connected row."""
    user_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            User.__table__.insert().values(
                id=user_id, email=f"m-{user_id.hex[:6]}@example.com", full_name="m", hashed_password="x", role="user"
            )
        )

        async def _fleet(name: str) -> uuid.UUID:
            ws_id, fleet_id = uuid.uuid4(), uuid.uuid4()
            await conn.execute(
                Workspace.__table__.insert().values(id=ws_id, user_id=user_id, name=f"ws-{name}", slug=f"ws-{name}")
            )
            # Raw SQL: the ORM table names the columns the revision adds.
            await conn.execute(
                text(
                    "INSERT INTO fleets (id, user_id, workspace_id, name, slug, description, icon, color, "
                    "is_archived, is_builtin, is_company_managed, access_restricted, created_at, updated_at) "
                    "VALUES (:id, :u, :w, :n, :n, '', 'Network', '', false, false, false, false, now(), now())"
                ),
                {"id": fleet_id, "u": user_id, "w": ws_id, "n": name},
            )
            return fleet_id

        async def _agent() -> uuid.UUID:
            agent_id = uuid.uuid4()
            await conn.execute(
                Agent.__table__.insert().values(id=agent_id, user_id=user_id, name=f"a-{agent_id.hex[:6]}")
            )
            return agent_id

        async def _row(fleet_id: uuid.UUID, role: str, inclusion: str, instruction: str) -> None:
            await conn.execute(
                text(
                    "INSERT INTO fleet_members (id, fleet_id, agent_id, role, context_inclusion, "
                    "collaboration_instruction, is_exposed, role_label, created_at) "
                    "VALUES (:id, :f, :a, :r, :ci, :ins, false, '', now())"
                ),
                {"id": uuid.uuid4(), "f": fleet_id, "a": await _agent(), "r": role, "ci": inclusion, "ins": instruction},
            )

        majority = await _fleet("majority")
        for inclusion, instruction in [
            ("summary", "Ask for context"),
            ("summary", "Ask for context"),
            ("summary", "Quantify the finding"),
            ("full", "Ask for context"),
            ("none", ""),
        ]:
            await _row(majority, "connected", inclusion, instruction)
        await _row(majority, "member", "none", "")
        tie = await _fleet("tie")
        for inclusion, instruction in [("summary", "A"), ("full", "B")]:
            await _row(tie, "connected", inclusion, instruction)
        single = await _fleet("single")
        await _row(single, "connected", "full", "Only one")
    return {"majority": majority, "tie": tie, "single": single}


async def _effective(engine: AsyncEngine, *, with_defaults: bool) -> dict[uuid.UUID, tuple[str, str]]:
    """Every connected row's effective (context_inclusion, collaboration_instruction)."""
    sql = (
        "SELECT m.id, COALESCE(m.context_inclusion, f.default_context_inclusion), "
        "COALESCE(m.collaboration_instruction, f.default_collaboration_instruction) "
        "FROM fleet_members m JOIN fleets f ON f.id = m.fleet_id WHERE m.role = 'connected'"
        if with_defaults
        else "SELECT id, context_inclusion, collaboration_instruction FROM fleet_members WHERE role = 'connected'"
    )
    async with engine.connect() as conn:
        return {row[0]: (row[1], row[2]) for row in (await conn.execute(text(sql))).all()}


@pytest.mark.asyncio
async def test_the_backfill_keeps_every_effective_value_and_only_the_differing_rows_override():
    async with scratch_database("mbrdflt", tables=_TABLES) as db:
        engine = db.engine
        await _as_before_the_revision(engine)
        fleets = await _seed(engine)
        before = await _effective(engine, with_defaults=False)

        await _migrate(engine, revision.upgrade)

        after = await _effective(engine, with_defaults=True)
        assert after == before, "an effective member value changed in the migration"
        async with engine.connect() as conn:
            defaults = {
                row[0]: (row[1], row[2])
                for row in (
                    await conn.execute(
                        text("SELECT id, default_context_inclusion, default_collaboration_instruction FROM fleets")
                    )
                ).all()
            }
            overrides = (
                await conn.execute(
                    text(
                        "SELECT count(*) FILTER (WHERE context_inclusion IS NOT NULL), "
                        "count(*) FILTER (WHERE collaboration_instruction IS NOT NULL) "
                        "FROM fleet_members WHERE fleet_id = :f AND role = 'connected'"
                    ),
                    {"f": fleets["majority"]},
                )
            ).one()
            home = (
                await conn.execute(
                    text("SELECT context_inclusion, collaboration_instruction FROM fleet_members WHERE role = 'member'")
                )
            ).one()
        # Majority: summary (3 of 5) and "Ask for context" (3 of 5) become the defaults.
        assert defaults[fleets["majority"]] == ("summary", "Ask for context")
        assert tuple(overrides) == (2, 2)
        # A tie and a single row reduce to nothing: every row keeps its value.
        assert defaults[fleets["tie"]] == ("none", "")
        assert defaults[fleets["single"]] == ("none", "")
        assert tuple(home) == (None, None)

        await _migrate(engine, revision.downgrade)
        assert await _effective(engine, with_defaults=False) == before


def test_the_reduction_needs_a_unique_value_shared_by_two_rows():
    assert revision._reduce([], "none") == "none"
    assert revision._reduce(["full"], "none") == "none"
    assert revision._reduce(["full", "summary"], "none") == "none"
    assert revision._reduce(["full", "full", "summary"], "none") == "full"
    assert revision._reduce(["a", "a", "b", "b"], "") == ""
