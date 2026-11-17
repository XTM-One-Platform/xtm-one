"""The investigation read endpoints against a real database.

One property here cannot be tested any other way. `GET /investigations/{id}`
caps a run's steps at `MAX_STEPS`; the list endpoint did not, so it issued one
`IN (...)` over up to `limit` runs and loaded *every* step row for all of them.
The list was therefore strictly weaker than the detail view it feeds, and the
only bound on the payload was how many rows happened to exist.

The cap is a window function — the right shape, because a flat total cap would
starve whichever runs sort last — and a window function is exactly the kind of
SQL a stub session cannot answer. Hence Postgres.
"""

from __future__ import annotations

import os
import urllib.parse
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401 — registers every mapper
from app.api.v1.investigations import (
    MAX_STEPS,
    get_investigation,
    list_investigations,
)
from app.core.database import Base
from app.models.investigation import Investigation, InvestigationStep
from app.models.user import User

pytestmark = pytest.mark.integration


def _base_url() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://copilot:copilot@localhost:5432/copilot",
    )


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    parsed = urllib.parse.urlparse(_base_url())
    admin_url = parsed._replace(path="/postgres").geturl()
    db_name = f"invapi_{uuid.uuid4().hex[:10]}"
    test_url = parsed._replace(path=f"/{db_name}").geturl()

    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    except (DBAPIError, OperationalError, ProgrammingError) as exc:
        orig = getattr(exc, "orig", None)
        pgcode = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
        if pgcode == "42501":
            pytest.skip("DATABASE_URL user requires CREATEDB privilege")
        raise
    except Exception as exc:  # pragma: no cover — no PG in this environment
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    finally:
        await admin_engine.dispose()

    engine = create_async_engine(test_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        cleanup = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with cleanup.connect() as conn:
                await conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :db AND pid <> pg_backend_pid()"
                    ),
                    {"db": db_name},
                )
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        finally:
            await cleanup.dispose()


async def _seed_user(session) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"analyst-{uuid.uuid4().hex[:8]}@example.test",
        hashed_password="x",
        full_name="Analyst",
        role="user",
    )
    session.add(user)
    await session.flush()
    return user


async def _seed_run(session, user, *, steps: int, subject: str) -> Investigation:
    run = Investigation(
        id=uuid.uuid4(),
        user_id=user.id,
        subject_type="unknown",
        subject_value=subject,
        source_pack_slugs=["web-general"],
        status="completed",
        revision=1,
        iterations_used=steps,
        max_iterations=steps or 1,
    )
    session.add(run)
    await session.flush()
    for position in range(steps):
        session.add(
            InvestigationStep(
                id=uuid.uuid4(),
                investigation_id=run.id,
                position=position,
                source_name=f"source-{position}",
                status="completed",
                findings_count=1,
                evidence=[],
            )
        )
    await session.flush()
    return run


@pytest.mark.asyncio
async def test_the_list_caps_each_runs_steps_like_the_detail_endpoint():
    """Two runs, each with more steps than the cap. Every run must come back
    trimmed to `MAX_STEPS` — not the first one, not the total across them."""
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user = await _seed_user(session)
        await _seed_run(session, user, steps=MAX_STEPS + 7, subject="one")
        await _seed_run(session, user, steps=MAX_STEPS + 3, subject="two")
        await session.commit()

        runs = await list_investigations(
            conversation_id=None, limit=20, user=user, db=session
        )

        assert len(runs) == 2
        for run in runs:
            assert len(run.steps) == MAX_STEPS, (
                f"{run.subject_value!r} returned {len(run.steps)} steps; the "
                "list must cap per run, exactly as the detail endpoint does"
            )


@pytest.mark.asyncio
async def test_no_run_is_starved_by_the_ones_before_it():
    """The reason for a window function rather than a flat total cap: with a
    total, whichever runs sort last come back with no steps at all — a block
    that renders empty for a run that has plenty."""
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user = await _seed_user(session)
        await _seed_run(session, user, steps=MAX_STEPS, subject="greedy")
        await _seed_run(session, user, steps=4, subject="modest")
        await session.commit()

        runs = await list_investigations(
            conversation_id=None, limit=20, user=user, db=session
        )
        by_subject = {r.subject_value: r for r in runs}
        assert len(by_subject["modest"].steps) == 4, (
            "a small run must not lose its steps to a large one"
        )


@pytest.mark.asyncio
async def test_the_capped_slice_is_the_same_one_the_detail_endpoint_returns():
    """Same rows, same order — the block hydrates from one and refreshes from
    the other, so a different slice would make steps appear or move."""
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user = await _seed_user(session)
        run = await _seed_run(session, user, steps=MAX_STEPS + 11, subject="both")
        await session.commit()

        listed = await list_investigations(
            conversation_id=None, limit=20, user=user, db=session
        )
        detail = await get_investigation(str(run.id), user=user, db=session)

        assert [s.step_id for s in listed[0].steps] == [
            s.step_id for s in detail.steps
        ]
        assert [s.position for s in detail.steps] == list(range(MAX_STEPS))


@pytest.mark.asyncio
async def test_an_ordinary_run_is_returned_whole():
    """The cap must be invisible below it: a real run is a handful of steps and
    must come back complete, in display order."""
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user = await _seed_user(session)
        await _seed_run(session, user, steps=6, subject="normal")
        await session.commit()

        runs = await list_investigations(
            conversation_id=None, limit=20, user=user, db=session
        )
        assert [s.position for s in runs[0].steps] == [0, 1, 2, 3, 4, 5]


# ── The counts describe the run, not the page ───────────────────────────────
#
# Gating `all_succeeded` on truncation stopped the green banner and left the
# sibling fields describing the sample: a run whose only `error` sits past
# `MAX_STEPS` reported `failed: 0`, and `by_status` summed to the page while
# `total` said the run. A zero is a claim too.


async def _seed_run_with(session, user, *, completed: int, failing: int):
    run = await _seed_run(session, user, steps=0, subject="capped")
    from app.models.investigation import InvestigationStep

    pos = 0
    for _ in range(completed):
        session.add(
            InvestigationStep(
                id=uuid.uuid4(), investigation_id=run.id, position=pos,
                source_name=f"ok-{pos}", status="completed", findings_count=1,

            )
        )
        pos += 1
    for _ in range(failing):
        session.add(
            InvestigationStep(
                id=uuid.uuid4(), investigation_id=run.id, position=pos,
                source_name=f"bad-{pos}", status="error", findings_count=0,

            )
        )
        pos += 1
    await session.flush()
    return run


@pytest.mark.asyncio
async def test_a_failure_past_the_cap_is_still_counted():
    """The failure sits at position `MAX_STEPS`, so no read returns it — and
    the summary must still say it happened."""
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user = await _seed_user(session)
        run = await _seed_run_with(session, user, completed=MAX_STEPS, failing=1)
        await session.commit()

        payload = await get_investigation(str(run.id), user=user, db=session)

        assert len(payload.steps) == MAX_STEPS, "the page is still capped"
        assert payload.steps_truncated is True
        assert payload.summary.total == MAX_STEPS + 1
        assert payload.summary.failed == 1, (
            "a failure past the cap was reported as zero"
        )
        assert payload.summary.all_succeeded is False
        assert sum(payload.summary.by_status.values()) == MAX_STEPS + 1, (
            "by_status must sum to the run, not to the page"
        )


@pytest.mark.asyncio
async def test_the_list_counts_the_run_too():
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user = await _seed_user(session)
        await _seed_run_with(session, user, completed=MAX_STEPS, failing=2)
        await session.commit()

        listed = await list_investigations(
            conversation_id=None, limit=20, user=user, db=session
        )
        summary = listed[0].summary
        assert summary.total == MAX_STEPS + 2
        assert summary.failed == 2
        assert sum(summary.by_status.values()) == MAX_STEPS + 2


@pytest.mark.asyncio
async def test_a_step_appended_between_the_two_reads_is_not_truncation():
    """`get_investigation` reads the steps and the aggregate in two statements,
    and the block polls DURING a run: the orchestrator commits after every
    step, so a lead landing between them makes the total larger than the page
    on a run nowhere near the cap. Nothing was withheld, so nothing is
    truncated — and this asserts the endpoint WIRES the cap through, which
    calling the serializer directly cannot prove."""
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user = await _seed_user(session)
        run = await _seed_run_with(session, user, completed=3, failing=0)
        await session.commit()

        # Simulate the race: one more step commits after the page was read.
        real_execute = session.execute
        appended = {"done": False}

        async def racing_execute(statement, *args, **kwargs):
            result = await real_execute(statement, *args, **kwargs)
            sql = str(statement).lower()
            # Fire on the PAGE query specifically — the one that selects step
            # rows — not on the run lookup before it or the aggregate after.
            is_page = "investigation_steps" in sql and "count(" not in sql
            if is_page and not appended["done"]:
                appended["done"] = True
                session.add(
                    InvestigationStep(
                        id=uuid.uuid4(), investigation_id=run.id, position=3,
                        source_name="lead-3", status="completed", findings_count=1,
                    )
                )
                await session.flush()
            return result

        session.execute = racing_execute
        try:
            payload = await get_investigation(str(run.id), user=user, db=session)
        finally:
            session.execute = real_execute

        assert len(payload.steps) == 3
        assert payload.summary.total == 4, "the aggregate saw the new step"
        assert payload.steps_truncated is False, (
            "a three-step page cannot have been cut by a 500-step cap"
        )


@pytest.mark.asyncio
async def test_an_ordinary_run_reports_exactly_what_it_has():
    """The aggregation must be invisible below the cap."""
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user = await _seed_user(session)
        run = await _seed_run_with(session, user, completed=4, failing=1)
        await session.commit()

        payload = await get_investigation(str(run.id), user=user, db=session)
        assert payload.steps_truncated is False
        assert payload.summary.total == 5
        assert payload.summary.failed == 1
        assert payload.summary.all_succeeded is False
        assert payload.summary.by_status == {"completed": 4, "error": 1}


@pytest.mark.asyncio
async def test_both_endpoints_say_where_the_report_stands(monkeypatch):
    """On the writer's word: `writing` while its marker is set, `none` after."""
    from app.services import investigation_report_status as report_status

    marked: set[str] = set()

    class _Markers:
        async def mget(self, keys):
            return ["1" if key in marked else None for key in keys]

    async def _client():
        return _Markers()

    monkeypatch.setattr(report_status, "_redis", _client)
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user = await _seed_user(session)
        pending = await _seed_run(session, user, steps=1, subject="pending")
        written = await _seed_run(session, user, steps=1, subject="written")
        written.report = "# Intelligence report"
        await session.commit()

        marked.add(report_status.report_writing_key(pending.id))
        detail = await get_investigation(str(pending.id), user=user, db=session)
        assert detail.report_status == "writing"
        listed = {r.subject_value: r.report_status for r in await list_investigations(
            conversation_id=None, limit=20, user=user, db=session
        )}
        assert listed == {"pending": "writing", "written": "written"}

        marked.clear()
        detail = await get_investigation(str(pending.id), user=user, db=session)
        assert detail.report_status == "none"
