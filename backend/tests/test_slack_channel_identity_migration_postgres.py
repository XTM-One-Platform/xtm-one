"""The ``s5811slackid01`` revision, executed against PostgreSQL.

The Slack adapter used to hand the router the channel id as the sender's id,
so ``slack:<channel id>`` was auto-linked to the first sender of a channel who
resolved by email. The revision removes every such key, and only those: a
member id an administrator linked and the other channels' keys stay. It is
loaded from its file: a migration must keep meaning what it meant on the day
it ran.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models.user import User
from tests.scratch_database import scratch_database

pytestmark = pytest.mark.integration

_REVISION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "s5811slackid01_forget_slack_identities_naming_a_channel.py"
)


def _load_revision():
    spec = importlib.util.spec_from_file_location("s5811slackid01", _REVISION)
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


async def _insert(engine: AsyncEngine, identities: dict | None) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            User.__table__.insert().values(
                id=user_id,
                email=f"{user_id.hex[:8]}@example.com",
                full_name="Someone",
                channel_identities=identities,
            )
        )
    return user_id


async def _identities(engine: AsyncEngine) -> dict[uuid.UUID, dict | None]:
    async with engine.connect() as conn:
        rows = await conn.execute(select(User.__table__.c.id, User.__table__.c.channel_identities))
        return {row.id: row.channel_identities for row in rows}


@pytest.mark.asyncio
async def test_only_the_slack_keys_naming_a_channel_are_forgotten():
    async with scratch_database("slackident", tables=[User.__table__]) as db:
        engine = db.engine
        users = {
            "public-channel": await _insert(engine, {"slack:C0PUBLIC": True}),
            "mixed": await _insert(
                engine,
                {
                    "slack:G0PRIVATE": True,
                    "slack:D0DIRECT": True,
                    "slack:U0MEMBER": True,
                    "slack:W0GRIDMEMBER": True,
                    "whatsapp:+33600000000": True,
                    "teams:29:1abc": True,
                },
            ),
            "member-only": await _insert(engine, {"slack:U0OTHER": True}),
            "none": await _insert(engine, None),
            "empty": await _insert(engine, {}),
        }

        await _migrate(engine, revision.upgrade)

        after = await _identities(engine)
        assert after == {
            users["public-channel"]: {},
            users["mixed"]: {
                "slack:U0MEMBER": True,
                "slack:W0GRIDMEMBER": True,
                "whatsapp:+33600000000": True,
                "teams:29:1abc": True,
            },
            users["member-only"]: {"slack:U0OTHER": True},
            users["none"]: None,
            users["empty"]: {},
        }

        # Re-runnable, and the downgrade has nothing to restore.
        await _migrate(engine, revision.upgrade)
        assert await _identities(engine) == after
        await _migrate(engine, revision.downgrade)
        assert await _identities(engine) == after


@pytest.mark.asyncio
async def test_more_holders_than_one_batch_are_all_cleaned(monkeypatch):
    monkeypatch.setattr(revision, "_BATCH", 2)
    async with scratch_database("slackidentbatch", tables=[User.__table__]) as db:
        engine = db.engine
        for index in range(5):
            await _insert(engine, {f"slack:C0{index}": True, f"slack:U0{index}": True})

        await _migrate(engine, revision.upgrade)

        assert sorted(tuple(keys.items()) for keys in (await _identities(engine)).values()) == [
            ((f"slack:U0{index}", True),) for index in range(5)
        ]
