"""The ``v5710bound01`` revision, executed against PostgreSQL.

It adds the account-wide hourly turn limit and the reply recipient limit, and
gives the existing mailboxes the mailbox preset's turn limit - their
per-conversation limit never bounded one sender writing new emails. Loaded
from its file: a migration keeps meaning what it meant on the day it ran.
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

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.agent_account import AgentAccount
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_REVISION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / (
    "v5710bound01_agent_account_turn_and_recipient_limits.py"
)


def _load_revision():
    spec = importlib.util.spec_from_file_location("v5710bound01", _REVISION)
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


async def _columns(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name = 'agent_accounts' AND column_name IN ('max_turns_per_hour', 'max_reply_recipients')"
            )
        )
        return {row[0] for row in rows}


async def _limits(engine: AsyncEngine, ids: dict[str, uuid.UUID]) -> dict[str, tuple[int, int]]:
    async with engine.connect() as conn:
        out = {}
        for name, account_id in ids.items():
            row = (
                await conn.execute(
                    text("SELECT max_turns_per_hour, max_reply_recipients FROM agent_accounts WHERE id = :id"),
                    {"id": account_id},
                )
            ).one()
            out[name] = (row[0], row[1])
        return out


async def test_the_existing_mailboxes_get_an_hourly_turn_limit_and_every_account_a_recipient_limit():
    async with scratch_database(
        "acct_limits_migration", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        engine = scratch.engine
        async with scratch.sessions() as db:
            owner = User(email=f"o-{uuid.uuid4().hex[:6]}@example.com", hashed_password="x", full_name="Sam")
            db.add(owner)
            await db.flush()
            accounts = {
                provider: AgentAccount(user_id=owner.id, name=provider, provider=provider, rules=[])
                for provider in ("microsoft_outlook", "microsoft_teams")
            }
            db.add_all(accounts.values())
            await db.commit()
            ids = {provider: account.id for provider, account in accounts.items()}
        # The table as it was before the revision.
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE agent_accounts DROP COLUMN max_turns_per_hour"))
            await conn.execute(text("ALTER TABLE agent_accounts DROP COLUMN max_reply_recipients"))

        await _migrate(engine, revision.upgrade)
        assert await _limits(engine, ids) == {"microsoft_outlook": (60, 10), "microsoft_teams": (0, 10)}

        # A re-run changes nothing, a limit the owner set since included.
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE agent_accounts SET max_turns_per_hour = 0 WHERE id = :id"), {"id": ids["microsoft_outlook"]}
            )
        await _migrate(engine, revision.upgrade)
        assert await _limits(engine, ids) == {"microsoft_outlook": (0, 10), "microsoft_teams": (0, 10)}

        await _migrate(engine, revision.downgrade)
        assert await _columns(engine) == set()
        await _migrate(engine, revision.downgrade)
        assert await _columns(engine) == set()
