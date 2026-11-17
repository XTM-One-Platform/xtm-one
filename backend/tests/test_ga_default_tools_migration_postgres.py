"""The ``g5209gadefault`` revision, executed against PostgreSQL.

The revision adds the platform builder and web fetch to the General Assistant
rows that exist at upgrade. It is loaded from its file: a migration must keep
meaning what it meant on the day it ran.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models.general_assistant_config import GeneralAssistantConfig
from tests.scratch_database import LOCK_WAITERS_IN_THIS_DATABASE, scratch_database

pytestmark = pytest.mark.integration

_REVISION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "g5209gadefault_add_ga_default_platform_builder.py"
)


def _load_revision():
    spec = importlib.util.spec_from_file_location("g5209gadefault", _REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


revision = _load_revision()


def _upgrade_and_read_row_locks(sync_conn) -> int:
    """Run the revision, then count the row locks it holds on the table."""
    with Operations.context(MigrationContext.configure(sync_conn)):
        revision.upgrade()
    return sync_conn.execute(
        text(
            "SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
            "WHERE l.pid = pg_backend_pid() AND l.mode = 'RowShareLock' "
            "AND c.relname = 'general_assistant_config'"
        )
    ).scalar_one()


async def _upgrade(engine: AsyncEngine) -> int:
    async with engine.begin() as conn:
        return await conn.run_sync(_upgrade_and_read_row_locks)


async def _insert(engine: AsyncEngine, stored: str | None) -> uuid.UUID:
    row_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            GeneralAssistantConfig.__table__.insert().values(
                id=row_id,
                system_prompt="You are the assistant.",
                handover_prompt="",
                created_at=text("now()"),
                updated_at=text("now()"),
            )
        )
        await conn.execute(
            text("UPDATE general_assistant_config SET builtin_tool_ids = CAST(:v AS json) WHERE id = :id"),
            {"v": stored, "id": row_id},
        )
    return row_id


async def _stored(engine: AsyncEngine, row_id: uuid.UUID) -> str | None:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT CAST(builtin_tool_ids AS text) FROM general_assistant_config WHERE id = :id"),
                {"id": row_id},
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_the_tools_are_appended_once_and_anything_else_is_left_as_it_is(caplog):
    caplog.set_level(logging.WARNING, logger=revision.logger.name)
    async with scratch_database("gadefault", tables=[GeneralAssistantConfig.__table__]) as db:
        engine = db.engine
        rows = {
            "missing-both": await _insert(engine, '["generate_file"]'),
            "missing-one": await _insert(engine, '["web_fetch", "browser"]'),
            "sql-null": await _insert(engine, None),
            "both-present": await _insert(engine, '["agent_builder", "web_fetch"]'),
            "not-a-list": await _insert(engine, '{"not": "a list"}'),
            # A json value PostgreSQL stores but cannot cast to jsonb.
            "lone-surrogate": await _insert(engine, '["\\ud800"]'),
        }

        await _upgrade(engine)

        values = {key: await _stored(engine, row_id) for key, row_id in rows.items()}
        assert json.loads(values["missing-both"]) == ["generate_file", "agent_builder", "web_fetch"]
        assert json.loads(values["missing-one"]) == ["web_fetch", "browser", "agent_builder"]
        assert json.loads(values["sql-null"]) == ["agent_builder", "web_fetch"]
        assert values["both-present"] == '["agent_builder", "web_fetch"]'
        assert values["not-a-list"] == '{"not": "a list"}'
        assert json.loads(values["lone-surrogate"]) == ["\ud800", "agent_builder", "web_fetch"]
        # The operator is told once, naming the switch that bounds an admin's reach.
        [notice] = [r for r in caplog.records if r.name == revision.logger.name]
        assert "AGENT_TOOLS_STRICT_ADMIN_ACCESS" in notice.getMessage()

        # A re-run finds nothing to write, takes no row lock and says nothing.
        caplog.clear()
        assert await _upgrade(engine) == 0
        assert {key: await _stored(engine, row_id) for key, row_id in rows.items()} == values
        assert not [r for r in caplog.records if r.name == revision.logger.name]


@pytest.mark.asyncio
async def test_an_administrators_save_during_the_upgrade_is_the_list_the_tools_join():
    """A rolling deploy: an admin's save is in flight when the revision runs.
    The revision waits for it under its lock timeout and appends to what the
    admin saved, never to the list it read before."""
    async with scratch_database("gadefault", tables=[GeneralAssistantConfig.__table__]) as db:
        engine = db.engine
        row_id = await _insert(engine, '["generate_file"]')

        async with engine.connect() as admin, engine.connect() as observer:
            await admin.begin()
            await admin.execute(
                text("UPDATE general_assistant_config SET builtin_tool_ids = CAST(:v AS json) WHERE id = :id"),
                {"v": '["browser"]', "id": row_id},
            )
            upgrade = asyncio.create_task(_upgrade(engine))
            for _ in range(150):
                waiting = (await observer.execute(LOCK_WAITERS_IN_THIS_DATABASE)).scalar_one()
                await observer.rollback()
                if waiting:
                    break
                await asyncio.sleep(0.02)
            assert waiting == 1, "the revision waits for the admin's row lock"
            await admin.commit()
            await upgrade

        assert json.loads(await _stored(engine, row_id)) == ["browser", "agent_builder", "web_fetch"]
