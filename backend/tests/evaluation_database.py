"""The throwaway PostgreSQL database of the evaluation integration suites, and the
fixtures several of them seed it with.

Used by ``test_evaluations.py`` and the ``test_evaluation_*`` integration suites.
``_stub_audit_and_groups`` is autouse: a module importing it stubs the audit log and
the group lookup of ``app.api.v1.evaluations`` for every one of its tests.

Kept apart from ``evaluation_helpers``, which the metric-suite tests import: this
module loads the platform's models and API.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - register model metadata
from app.api.v1 import evaluations as evaluations_api
from app.core.database import json_serializer
from app.models.agent import Agent
from app.models.audit_log import AuditLog
from app.models.dataset import Dataset, DatasetEntry, EvaluationDataset
from app.models.dataset_version import DatasetVersion, DatasetVersionEntry
from app.models.evaluation import (
    Evaluation,
    EvaluationAgent,
    EvaluationEpisode,
    EvaluationPlaceboComparison,
    EvaluationResult,
    EvaluationResultTrace,
    EvaluationRun,
)
from app.models.evaluation_metric import EvaluationMetric
from app.models.evaluation_param import EvaluationRunParam
from app.models.flow import Flow
from app.models.group import Group, GroupMember
from app.models.orchestration import OrchestrationExecution
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

_TABLES = [
    User.__table__,
    # Read by ``export_evaluation`` through ``get_user_group_ids``, and the
    # import writes an audit row.
    Group.__table__,
    GroupMember.__table__,
    AuditLog.__table__,
    Agent.__table__,
    Evaluation.__table__,
    Flow.__table__,
    EvaluationAgent.__table__,
    EvaluationRun.__table__,
    # Deleting every run of an evaluation deletes its placebo comparisons.
    EvaluationPlaceboComparison.__table__,
    EvaluationResult.__table__,
    # ``get_run`` loads traces alongside results, so any test that drives the
    # endpoint with a result present needs this table to exist.
    EvaluationResultTrace.__table__,
    # execute_evaluation's abandoned-run reclaim tests episode state,
    # so the curated subset has to carry the table it reads.
    EvaluationEpisode.__table__,
    Dataset.__table__,
    DatasetEntry.__table__,
    DatasetVersion.__table__,
    DatasetVersionEntry.__table__,
    EvaluationDataset.__table__,
    EvaluationRunParam.__table__,
    # _reconcile_run_status drives the headline writer, which reads the
    # deterministic quality metrics. A test reaching a terminal run through an
    # episode would otherwise fail on a missing relation, not on its assertion.
    EvaluationMetric.__table__,
    # engine.complete/fail re-read this row FOR UPDATE to enforce the lease-loss
    # guarantee. Its failure mode is ORM identity-map staleness, which no fake
    # session can model.
    OrchestrationExecution.__table__,
]


async def _make_db():
    """An isolated database with the ``_TABLES`` subset (``tests/scratch_database.py``).

    Returns ``(sessionmaker, cleanup)``, the shape the tests here take: the database
    is dropped once ``cleanup`` is awaited. Skips if PostgreSQL is unavailable.
    """
    return await _enter(scratch_database("evaluations", tables=_TABLES))


async def _make_db_full():
    """``_make_db`` with every table and the ``vector`` extension.

    An end-to-end ``trigger_evaluation`` also touches the agent version snapshot
    tables and platform settings, which the curated ``_TABLES`` subset omits.
    """
    return await _enter(scratch_database("evaluations_full", tables=ALL_TABLES, extensions=("vector",)))


async def _enter(database):
    stack = contextlib.AsyncExitStack()
    db = await stack.enter_async_context(database)
    # The serializer the application engine installs (``core/database.py``), set
    # before the engine's first statement. Without it JSON columns are written by
    # a route production never uses, so a payload the real engine sanitises, a NUL
    # byte or a ``NaN``, fails here and passes there. Private attribute: remove once
    # ``scratch_database`` installs the serializer.
    db.engine.dialect._json_serializer = json_serializer
    return db.sessions, stack.aclose


@pytest.fixture(autouse=True)
def _stub_audit_and_groups(monkeypatch):
    async def _no_groups(_db, _uid):
        return []

    async def _no_audit(*_a, **_k):
        return None

    monkeypatch.setattr(evaluations_api, "get_user_group_ids", _no_groups)
    monkeypatch.setattr(evaluations_api, "log_audit", _no_audit)


async def _admin(session) -> User:
    admin = User(email="a@x.io", full_name="Admin", hashed_password="x", role="admin")
    session.add(admin)
    await session.flush()
    await session.commit()
    return admin


async def _linked_dataset(session, evaluation, *, family) -> Dataset:
    ds = Dataset(name="ds", family=family)
    session.add(ds)
    await session.flush()
    session.add(DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a"))
    session.add(EvaluationDataset(evaluation_id=evaluation.id, dataset_id=ds.id, position=0))
    await session.commit()
    return ds


async def _active_runs_fixture(sm, specs):
    """Build one evaluation with a run per (status, owner, **kwargs) spec."""
    async with sm() as s:
        admin = (
            await s.execute(select(User).where(User.email == "a@x.io"))
        ).scalar_one_or_none() or await _admin(s)
        evaluation = Evaluation(name="Eval", family="STIX_BUNDLE")
        s.add(evaluation)
        await s.flush()
        agent = Agent(user_id=admin.id, name="Agent")
        s.add(agent)
        await s.flush()
        runs = [
            EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4.1",
                status=status,
                trigger_reason="manual",
                owner_execution_id=owner,
                **kwargs,
            )
            for status, owner, kwargs in specs
        ]
        s.add_all(runs)
        await s.commit()
        return evaluation.id, [r.id for r in runs]


async def _stranded_fixture(
    sm,
    *,
    execution_state="completed",
    execution_age_seconds=3600,
    # Separate from the execution's age ON PURPOSE: with one value for both, a
    # sweep measuring from the RUN would pass every test.
    run_started_age_seconds=None,
    run_status="running",
    run_completed_at=None,
    owner=True,
    episodes=(),
    entries=3,
):
    """One evaluation, one run, one owning execution, aged as asked.

    Returns ``(run_id, execution_id, entry_ids)``. Defaults describe a stranded
    run; each test moves ONE thing away from that, so a failure names its cause.
    """
    from datetime import UTC, datetime, timedelta

    execution_id = uuid.uuid4()
    async with sm() as s:
        # Reused, not created: some tests need MORE THAN ONE stranded run, and
        # ``_admin`` inserts a fixed-email user (unique index).
        admin = (
            await s.execute(select(User).where(User.email == "a@x.io"))
        ).scalar_one_or_none() or await _admin(s)
        evaluation = Evaluation(name=f"Eval-{execution_id.hex[:8]}", family="STIX_BUNDLE")
        s.add(evaluation)
        await s.flush()
        agent = Agent(user_id=admin.id, name=f"Agent-{execution_id.hex[:8]}")
        ds = Dataset(name=f"ds-{execution_id.hex[:8]}", family="STIX_BUNDLE")
        s.add_all([agent, ds])
        await s.flush()
        entry_rows = [
            DatasetEntry(dataset_id=ds.id, position=i, expected_answer="a")
            for i in range(entries)
        ]
        s.add_all(entry_rows)
        await s.flush()

        aged = datetime.now(UTC) - timedelta(seconds=execution_age_seconds)
        s.add(
            OrchestrationExecution(
                id=execution_id,
                adapter="evaluation",
                state=execution_state,
                payload={},
                attempt_count=1,
                created_at=aged,
                updated_at=aged,
            )
        )
        run = EvaluationRun(
            evaluation_id=evaluation.id,
            agent_id=agent.id,
            model_id="gpt-4.1",
            status=run_status,
            trigger_reason="manual",
            total_entries=entries,
            completed_entries=0,
            completed_at=run_completed_at,
            owner_execution_id=execution_id if owner else None,
            started_at=(
                aged
                if run_started_age_seconds is None
                else datetime.now(UTC) - timedelta(seconds=run_started_age_seconds)
            ),
        )
        s.add(run)
        await s.flush()
        # One episode per ENTRY: ``(run_id, entry_id)`` is unique.
        assert len(episodes) <= len(entry_rows), (
            f"_stranded_fixture: {len(episodes)} episodes asked for but only "
            f"{len(entry_rows)} entries exist; raise `entries`"
        )
        for entry_row, status in zip(entry_rows, episodes, strict=False):
            s.add(
                EvaluationEpisode(
                    run_id=run.id,
                    entry_id=entry_row.id,
                    correlation_id=f"c:{uuid.uuid4()}",
                    status=status,
                )
            )
        await s.commit()
        return run.id, execution_id, [e.id for e in entry_rows]


async def _run_status(sm, run_id):
    async with sm() as s:
        row = await s.get(EvaluationRun, run_id)
        return row.status, row.error
