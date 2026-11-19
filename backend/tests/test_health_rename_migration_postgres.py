"""The ``h118health01`` revision carries every value, executed against PostgreSQL.

Slice S1 of #101 renames the schema of the former Concierge to Platform Health:
an agent or flow enrolled before the upgrade stays enrolled (and keeps its
content consent), a person's opt-out stays an opt-out, the settings JSON is
carried as stored and the ten tables keep their rows. The scratch schema is
built from the models (the new names), taken back to the former names with the
revision's own downgrade, then upgraded. The revision is loaded from its file:
a migration must keep meaning what it meant on the day it ran.
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

import app.models  # noqa: F401 - registers every table
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_REVISION = (
    Path(__file__).resolve().parents[1] / "alembic" / "versions" / "h118health01_rename_concierge_to_platform_health.py"
)


def _load_revision():
    spec = importlib.util.spec_from_file_location("h118health01", _REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


revision = _load_revision()

_SETTINGS = {"enabled": True, "scope_mode": "all", "monthly_budget_usd": 12.5}


async def _run_revision(engine: AsyncEngine, step) -> None:
    def _call(sync_conn) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            step()

    async with engine.begin() as conn:
        await conn.run_sync(_call)


async def _tables(engine: AsyncEngine, prefix: str) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name LIKE :p"
            ),
            {"p": f"{prefix}%"},
        )
        return set(rows.scalars())


async def _scalar(engine: AsyncEngine, sql: str, **params):
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


@pytest.mark.asyncio
async def test_the_rename_carries_enrolment_consent_opt_outs_and_settings():
    from app.models.agent import Agent
    from app.models.flow import Flow
    from app.models.platform_settings import PlatformSettings
    from app.models.user import User

    async with scratch_database(
        "hlthmig", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        engine = db.engine
        async with db.sessions() as s:
            out = User(email=f"out-{uuid.uuid4().hex[:6]}@example.com", hashed_password="x", full_name="out")
            kept = User(email=f"in-{uuid.uuid4().hex[:6]}@example.com", hashed_password="x", full_name="in")
            out.health_content_opt_out = True
            enrolled = Agent(
                name="Enrolled", slug=f"e-{uuid.uuid4().hex[:6]}", health_enrolled=True, health_role="experimenter"
            )
            private = Agent(name="Private", slug=f"p-{uuid.uuid4().hex[:6]}")
            flow = Flow(name="Enrolled flow", health_enrolled=True)
            s.add_all([out, kept, enrolled, private, flow, PlatformSettings(health_settings=_SETTINGS)])
            await s.commit()
            ids = {"out": out.id, "kept": kept.id, "enrolled": enrolled.id, "private": private.id, "flow": flow.id}

        # Back to the schema an upgrading install has: the former names.
        await _run_revision(engine, revision.downgrade)
        assert await _tables(engine, "health_") & {f"health_{t}" for t in revision._TABLES} == set()
        assert {f"concierge_{t}" for t in revision._TABLES} <= await _tables(engine, "concierge_")
        assert (
            await _scalar(engine, "SELECT concierge_improvement_opt_in FROM agents WHERE id = :i", i=ids["enrolled"])
            is True
        )

        await _run_revision(engine, revision.upgrade)

        assert {f"health_{t}" for t in revision._TABLES} <= await _tables(engine, "health_")
        assert await _tables(engine, "concierge_") == set()
        agent_row = "SELECT {col} FROM agents WHERE id = :i"
        assert await _scalar(engine, agent_row.format(col="health_enrolled"), i=ids["enrolled"]) is True
        assert await _scalar(engine, agent_row.format(col="health_content_opt_in"), i=ids["enrolled"]) is True
        assert await _scalar(engine, agent_row.format(col="health_role"), i=ids["enrolled"]) == "experimenter"
        # Never enrolled stays out: an upgrade enrols nothing.
        assert await _scalar(engine, agent_row.format(col="health_enrolled"), i=ids["private"]) is False
        assert await _scalar(engine, agent_row.format(col="health_content_opt_in"), i=ids["private"]) is False
        flow_row = "SELECT {col} FROM flows WHERE id = :i"
        assert await _scalar(engine, flow_row.format(col="health_enrolled"), i=ids["flow"]) is True
        assert await _scalar(engine, flow_row.format(col="health_content_opt_in"), i=ids["flow"]) is True
        user_row = "SELECT health_content_opt_out FROM users WHERE id = :i"
        assert await _scalar(engine, user_row, i=ids["out"]) is True
        assert await _scalar(engine, user_row, i=ids["kept"]) is False
        assert await _scalar(engine, "SELECT health_settings::jsonb FROM platform_settings LIMIT 1") == _SETTINGS

        # Re-run: every probe finds the new names, nothing moves.
        await _run_revision(engine, revision.upgrade)
        assert await _scalar(engine, agent_row.format(col="health_content_opt_in"), i=ids["enrolled"]) is True
