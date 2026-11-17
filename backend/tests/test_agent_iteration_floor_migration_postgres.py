"""The ``i5940iter01`` revision, executed against PostgreSQL.

The revision moves the platform iteration default from 25 to 300 and raises
every cap people configured below 100 to 100. It is loaded from its file: a
migration must keep meaning what it meant on the day it ran.
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
from app.models.general_assistant_config import GeneralAssistantConfig
from app.models.user import User
from tests.scratch_database import scratch_database

pytestmark = pytest.mark.integration

_REVISION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "i5940iter01_raise_agent_iteration_budgets.py"
)
_TABLES = [User.__table__, Agent.__table__, GeneralAssistantConfig.__table__]


def _load_revision():
    spec = importlib.util.spec_from_file_location("i5940iter01", _REVISION)
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


async def _column_default(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'general_assistant_config' "
                    "AND column_name = 'max_iterations'"
                )
            )
        ).scalar_one()


async def _insert_settings(engine: AsyncEngine, max_iterations: int) -> uuid.UUID:
    row_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            GeneralAssistantConfig.__table__.insert().values(
                id=row_id,
                max_iterations=max_iterations,
                system_prompt="You are the assistant.",
                handover_prompt="",
                created_at=text("now()"),
                updated_at=text("now()"),
            )
        )
    return row_id


async def _insert_agent(
    engine: AsyncEngine, max_iterations: int | None, *, is_builtin: bool = False
) -> uuid.UUID:
    agent_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            Agent.__table__.insert().values(
                id=agent_id,
                name=f"agent-{agent_id.hex[:6]}",
                max_iterations=max_iterations,
                is_builtin=is_builtin,
            )
        )
    return agent_id


async def _caps(engine: AsyncEngine, table: str, ids: dict[str, uuid.UUID]) -> dict[str, int | None]:
    async with engine.connect() as conn:
        return {
            key: (
                await conn.execute(
                    text(f"SELECT max_iterations FROM {table} WHERE id = :id"),  # noqa: S608
                    {"id": row_id},
                )
            ).scalar_one()
            for key, row_id in ids.items()
        }


@pytest.mark.asyncio
async def test_the_default_moves_to_300_and_every_configured_cap_to_at_least_100():
    async with scratch_database("iterfloor", tables=_TABLES) as db:
        engine = db.engine
        # The column as an install created before the revision has it.
        async with engine.begin() as conn:
            await conn.execute(
                text("ALTER TABLE general_assistant_config ALTER COLUMN max_iterations SET DEFAULT 25")
            )
        settings_rows = {
            "old-default": await _insert_settings(engine, 25),
            "below-floor": await _insert_settings(engine, 40),
            "admin-choice": await _insert_settings(engine, 150),
        }
        agents = {
            "below-floor": await _insert_agent(engine, 20),
            "single-step": await _insert_agent(engine, 1),
            "inherits": await _insert_agent(engine, None),
            "at-floor": await _insert_agent(engine, 100),
            "above-floor": await _insert_agent(engine, 250),
            "builtin": await _insert_agent(engine, 5, is_builtin=True),
        }

        await _migrate(engine, revision.upgrade)

        assert await _column_default(engine) == "300"
        assert await _caps(engine, "general_assistant_config", settings_rows) == {
            "old-default": 300,
            "below-floor": 100,
            "admin-choice": 150,
        }
        expected_agents = {
            "below-floor": 100,
            "single-step": 100,
            "inherits": None,
            "at-floor": 100,
            "above-floor": 250,
            # The seed owns a built-in's cap and rewrites it on every boot.
            "builtin": 5,
        }
        assert await _caps(engine, "agents", agents) == expected_agents

        # A re-run finds nothing below the floor and changes nothing.
        await _migrate(engine, revision.upgrade)
        assert await _column_default(engine) == "300"
        assert await _caps(engine, "agents", agents) == expected_agents

        # The downgrade restores the column default; raised caps stay valid.
        await _migrate(engine, revision.downgrade)
        assert await _column_default(engine) == "25"
        assert await _caps(engine, "agents", agents) == expected_agents
