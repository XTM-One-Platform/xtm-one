"""The re-land migration must not leave a stranded gated pause unanswerable.

A platform stamped before the revert release runs ``d4und0173`` and
``e5rel3298`` in one startup: ``pending_approvals`` is dropped, taking the
proposals with it, then re-created empty. A run paused at the gate then has
nothing left to decide, and ``respond_to_human_input`` refuses free text for it
— so the re-land fails those rows instead. A classic ``request_human_input``
wait is still answerable and must survive untouched, which is the half of this
that a blanket "fail every awaiting_human row" would get wrong.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models as _models  # noqa: F401 — side-effect: register all models
from tests.test_approval_upgrade_migration import (
    _seed_pre_upgrade_fleet,
    _temporary_database,
)

pytestmark = pytest.mark.integration

# What ``d4und0173`` took away and this migration puts back.
_DROPPED = ("pending_approvals", "approval_decisions", "triggered_by_user_id")


@pytest.fixture
async def db_url():
    """Same disposable database as the backfill tests — DDL under test."""
    async with _temporary_database() as url:
        yield url


def _run_reland_body(sync_conn) -> None:
    """Run ``e5rel3298.upgrade()`` against a ``create_all`` schema.

    Imported directly rather than driven through the Alembic runner, as in
    ``test_approval_upgrade_migration``: there is no revision chain here, and what
    needs exercising is the body's own guards.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "e5rel3298_reland_per_tool_approval_schema.py"
    )
    spec = importlib.util.spec_from_file_location("reland_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    ctx = MigrationContext.configure(sync_conn)
    with Operations.context(ctx):
        module.upgrade()


async def test_stranded_gated_pause_fails_and_free_text_wait_survives(db_url, monkeypatch):
    monkeypatch.delenv("TOOL_APPROVALS_BACKFILL_AGENTS", raising=False)
    engine = create_async_engine(db_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    gated_id = uuid.uuid4()
    asked_id = uuid.uuid4()
    try:
        async with sessionmaker() as session:
            from app.models.assignment import AssignmentRun

            (agent_id,) = await _seed_pre_upgrade_fleet(session, count=1)
            session.add_all(
                [
                    AssignmentRun(
                        id=gated_id,
                        agent_id=agent_id,
                        status="awaiting_human",
                        human_input_question="Approval required before running: send_email",
                        pending_approvals=[
                            {"tool_call_id": "call_1", "tool_name": "send_email"}
                        ],
                    ),
                    AssignmentRun(
                        id=asked_id,
                        agent_id=agent_id,
                        status="awaiting_human",
                        human_input_question="Which mailbox should I use?",
                    ),
                ]
            )
            await session.commit()

        async with engine.begin() as conn:
            for column in _DROPPED:
                await conn.execute(
                    text(f"ALTER TABLE assignment_runs DROP COLUMN {column}")
                )

        async with engine.begin() as conn:
            await conn.run_sync(_run_reland_body)

        async with sessionmaker() as session:
            statuses = dict(
                (
                    await session.execute(text("SELECT id, status FROM assignment_runs"))
                ).all()
            )
            assert statuses[gated_id] == "failed", (
                "a gated pause whose proposals the revert destroyed stays "
                "awaiting_human forever — no endpoint accepts an answer for it"
            )
            assert statuses[asked_id] == "awaiting_human", (
                "a classic request_human_input wait is still answerable with "
                "free text and must not be failed"
            )
            cause = await session.scalar(
                text("SELECT failure_cause FROM assignment_runs WHERE id = :id"),
                {"id": gated_id},
            )
            assert cause == "engine_abandoned"
    finally:
        await engine.dispose()
