"""The ``g5099galink01`` revision, executed against PostgreSQL.

The revision adds ``general_assistant_config.linked_agent_configured`` and
marks only the links that are unambiguously an administrator's: an agent other
than the CTEM Assistant. Loaded from its file: a migration must keep meaning
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
from app.models.general_assistant_config import GeneralAssistantConfig
from app.models.user import User
from tests.scratch_database import scratch_database

pytestmark = pytest.mark.integration

_REVISION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "g5099galink01_add_general_assistant_linked_agent_configured.py"
)
_TABLES = [User.__table__, Agent.__table__, GeneralAssistantConfig.__table__]


def _load_revision():
    spec = importlib.util.spec_from_file_location("g5099galink01", _REVISION)
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


async def _insert_agent(engine: AsyncEngine, slug: str) -> uuid.UUID:
    agent_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            Agent.__table__.insert().values(id=agent_id, name=slug, slug=slug, is_builtin=True)
        )
    return agent_id


async def _insert_settings(engine: AsyncEngine, linked_agent_id: uuid.UUID | None) -> uuid.UUID:
    row_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO general_assistant_config "
                "(id, linked_agent_id, system_prompt, handover_prompt, created_at, updated_at) "
                "VALUES (:id, :linked, '', '', now(), now())"
            ),
            {"id": row_id, "linked": linked_agent_id},
        )
    return row_id


async def _flags(engine: AsyncEngine, ids: dict[str, uuid.UUID]) -> dict[str, bool]:
    async with engine.connect() as conn:
        return {
            key: (
                await conn.execute(
                    text(
                        "SELECT linked_agent_configured FROM general_assistant_config "
                        "WHERE id = :id"
                    ),
                    {"id": row_id},
                )
            ).scalar_one()
            for key, row_id in ids.items()
        }


async def _column_exists(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'general_assistant_config' "
                    "AND column_name = 'linked_agent_configured'"
                )
            )
        ).first() is not None


@pytest.mark.asyncio
async def test_only_a_link_to_another_agent_is_marked_as_the_admins():
    async with scratch_database("galink", tables=_TABLES) as db:
        engine = db.engine
        # The table as an install created before the revision has it.
        async with engine.begin() as conn:
            await conn.execute(
                text("ALTER TABLE general_assistant_config DROP COLUMN linked_agent_configured")
            )
        ctem = await _insert_agent(engine, "ctem-assistant")
        other = await _insert_agent(engine, "my-own-agent")
        ids = {
            "ctem": await _insert_settings(engine, ctem),
            "other": await _insert_settings(engine, other),
            "none": await _insert_settings(engine, None),
        }

        await _migrate(engine, revision.upgrade)
        expected = {"ctem": False, "other": True, "none": False}
        assert await _flags(engine, ids) == expected

        # Idempotent: a second run is a no-op.
        await _migrate(engine, revision.upgrade)
        assert await _flags(engine, ids) == expected

        await _migrate(engine, revision.downgrade)
        assert await _column_exists(engine) is False
        await _migrate(engine, revision.downgrade)
        assert await _column_exists(engine) is False
