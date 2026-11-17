"""A best-effort write on the stale-reference failure path never loses the run.

The engine records a stale-reference failure on the run's own session after
two best-effort statements: it links the files the agent produced to the run
and reads the integrations' names for the error. PostgreSQL aborts the whole
transaction on a failed statement, so swallowing the exception is not enough:
without a savepoint every later statement - the run's ``failed`` status, its
error, the final commit - fails too, and the run is never recorded. A mock
session cannot show that state, so it is run here against PostgreSQL. Same
scratch-database recipe as ``test_a2a_cancel_child_runs_postgres.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.integrations.stale_references import StaleReference, StaleReferenceCollector
from app.llm.base import FileRef
from app.models.assignment import AssignmentRun
from app.models.file_attachment import FileAttachment
from app.tools.base import FileContext
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("staleref", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _seed(session) -> tuple[AssignmentRun, FileAttachment]:
    from app.models.agent import Agent
    from app.models.user import User

    user = User(
        id=uuid.uuid4(),
        email=f"staleref-{uuid.uuid4().hex[:8]}@test.local",
        full_name="Stale Reference Test",
        hashed_password="x",
    )
    session.add(user)
    await session.flush()
    agent = Agent(id=uuid.uuid4(), name="Notion Sync", user_id=user.id)
    session.add(agent)
    await session.flush()
    run = AssignmentRun(
        id=uuid.uuid4(),
        assignment_id=None,
        agent_id=agent.id,
        user_id=user.id,
        status="running",
        started_at=datetime.now(UTC),
    )
    attachment = FileAttachment(
        id=uuid.uuid4(),
        user_id=user.id,
        filename="prod-tracker.csv",
        content_type="text/csv",
        size=42,
        s3_object_name=f"files/{uuid.uuid4().hex}.csv",
        source="agent_generated",
    )
    session.add_all([run, attachment])
    await session.commit()
    return run, attachment


def _file_context(attachment: FileAttachment) -> FileContext:
    context = FileContext()
    context.add(
        FileRef(
            file_id=str(attachment.id),
            filename=attachment.filename,
            content_type=attachment.content_type,
            size=attachment.size,
            s3_object_name=attachment.s3_object_name,
        )
    )
    return context


async def _persisted_status(sessionmaker, run_id) -> tuple[str, str | None]:
    async with sessionmaker() as fresh:
        row = (
            await fresh.execute(
                select(AssignmentRun.status, AssignmentRun.error).where(AssignmentRun.id == run_id)
            )
        ).one()
    return row[0], row[1]


async def test_a_failed_file_link_does_not_abort_the_runs_failure():
    """The link statement fails (here: a foreign-key violation) - the run's
    failure written after it on the same session must still commit."""
    from app.services.assignment_engine import _link_run_output_files

    async with _temp_database() as sessionmaker:
        async with sessionmaker() as setup:
            run, attachment = await _seed(setup)

        async with sessionmaker() as db:
            row = (await db.execute(select(AssignmentRun).where(AssignmentRun.id == run.id))).scalar_one()
            row.result_summary = "Synced 42 rows; the prod tracker could not be updated."
            # A run id no row carries: the UPDATE violates the foreign key.
            ghost = SimpleNamespace(id=uuid.uuid4())
            await _link_run_output_files(db, ghost, _file_context(attachment), [])
            row.status = "failed"
            row.error = "Stale integration reference"
            await db.commit()

        assert await _persisted_status(sessionmaker, run.id) == (
            "failed",
            "Stale integration reference",
        )


async def test_a_failed_integration_name_lookup_does_not_abort_the_runs_failure(monkeypatch):
    """The name lookup is best-effort too: a failed read must not poison the run's session."""
    from app.services import assignment_engine

    async with _temp_database() as sessionmaker:
        async with sessionmaker() as setup:
            run, _attachment = await _seed(setup)

        async with sessionmaker() as db:
            row = (await db.execute(select(AssignmentRun).where(AssignmentRun.id == run.id))).scalar_one()
            collector = StaleReferenceCollector()
            collector.add(
                StaleReference(
                    provider="notion",
                    provider_label="Notion",
                    integration_id=str(uuid.uuid4()),
                    resource_type="database",
                    resource_id="26f8fce1-7f2a-8042-ac18-000b8646904c",
                    failing_runs=3,
                )
            )
            # The integrations table is gone: the read fails in the database.
            async with db.begin_nested():
                await db.execute(text("ALTER TABLE integrations RENAME TO integrations_away"))
            error = await assignment_engine._stale_reference_error(db, collector)
            row.status = "failed"
            row.error = str(error)
            await db.commit()

        status, persisted_error = await _persisted_status(sessionmaker, run.id)
        assert status == "failed"
        assert persisted_error and "26f8fce1-7f2a-8042-ac18-000b8646904c" in persisted_error
