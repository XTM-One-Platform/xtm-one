"""What a run stamps on itself: judge provenance, score lineage and run mode.

``EvaluationRun.scorer_selection`` records whether the judge ran,
``metric_suite_version`` which metric suite produced the numbers, and
``execution_mode`` the mode the run ran in; a run recorded before that column reads
its mode, and its LLM Only fallbacks, from its results.
"""

from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text

import app.models  # noqa: F401 - register model metadata
from app.models.agent import Agent
from app.models.dataset import Dataset, DatasetEntry
from app.models.dataset_version import DatasetVersion, DatasetVersionEntry
from app.models.evaluation import Evaluation, EvaluationResult, EvaluationRun
from app.models.flow import Flow
from app.models.user import User
from app.services.evaluation.metric_package import metric_suite_version
from app.services.evaluation.run_mode import (
    FALLBACK_MODE,
    RunMode,
    _recorded_mode,
    load_run_detail_mode,
    load_run_modes,
    mode_from_results,
)
from tests.evaluation_helpers import run_on_postgres, seed_provider
from tests.scratch_database import ALL_TABLES, scratch_database

# ── Judge provenance: ``EvaluationRun.scorer_selection`` ──────────────────────────────
# Read from the mutable ``Evaluation.scorer_selection`` instead, enabling the judge
# would claim every past run was judged; a judge-off entry stores ``0.0`` (``score``
# is NOT NULL). A run's reading is its own, a genuine all-zero judged run still
# reports ``0``, and the stamp lands where entries are scored.
# ``NULL`` means the run predates the column and reads as judge-on.


JUDGE_ON: list[str] = ["judge"]
JUDGE_OFF: list[str] = []


# ── The snapshot ─────────────────────────────────────────────────────────


def test_the_snapshot_keeps_unset_apart_from_judge_off_and_is_a_copy():
    """``None`` (nothing selected: judge-on) and ``[]`` (no scorers: judge-off)
    survive the snapshot as distinct values, and the snapshot is a copy.

    Normalising ``None`` to ``[]`` would stamp a judged run as judge-off and blank
    out real judge scores; an alias would let an in-place edit to the
    evaluation's list rewrite the run's recorded provenance.
    """
    from app.services.evaluation.scorers.registry import judge_enabled
    from app.services.evaluation_engine import _snapshot_selection

    assert _snapshot_selection(None) is None
    assert judge_enabled(_snapshot_selection(None)) is True
    assert _snapshot_selection([]) == []
    assert judge_enabled(_snapshot_selection([])) is False

    live = ["judge"]
    snapshot = _snapshot_selection(live)
    live.clear()

    assert snapshot == ["judge"], "the snapshot followed a mutation of its source"


# ── Persistence (integration) ────────────────────────────────────────────


async def _make_db():
    """An isolated database with every table and the ``vector`` extension (``tests/scratch_database.py``).

    Returns ``(sessionmaker, cleanup)``, the shape the tests here take: the database
    is dropped once ``cleanup`` is awaited. Skips if PostgreSQL is unavailable.
    """
    stack = contextlib.AsyncExitStack()
    db = await stack.enter_async_context(scratch_database("runstamps", tables=ALL_TABLES, extensions=("vector",)))
    return db.sessions, stack.aclose


async def _seed_evaluation(session, *, live_selection: list[str] | None):
    agent = Agent(name="Agent", slug=f"a-{uuid.uuid4().hex[:6]}")
    evaluation = Evaluation(name="Eval", scorer_selection=live_selection)
    session.add_all([agent, evaluation])
    await session.flush()
    return agent, evaluation


async def _seed_run(
    session,
    agent,
    evaluation,
    *,
    snapshot: list[str] | None,
    entry_scores: list[float],
    set_snapshot: bool = True,
):
    """One completed run whose entries carry ``entry_scores`` in ``score``.

    ``score`` is NOT NULL, so a judge-off run stores ``0.0`` per entry.
    """
    run = EvaluationRun(
        evaluation_id=evaluation.id,
        agent_id=agent.id,
        model_id="gpt-4o",
        trigger_reason="manual",
        status="completed",
    )
    if set_snapshot:
        run.scorer_selection = snapshot
    session.add(run)
    await session.flush()

    for value in entry_scores:
        session.add(EvaluationResult(run_id=run.id, agent_output="", score=value))
    await session.flush()
    return run


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_judge_off_run_stays_na_when_the_judge_is_re_enabled():
    """A judge-off run keeps reporting N/A after the evaluation re-enables the
    judge and its headlines are recomputed.
    """
    from app.services.evaluation_engine import _compute_run_headlines

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            # The run happened while the judge was off...
            agent, evaluation = await _seed_evaluation(s, live_selection=JUDGE_OFF)
            run = await _seed_run(
                s, agent, evaluation, snapshot=JUDGE_OFF, entry_scores=[0.0, 0.0, 0.0]
            )
            await s.commit()
            assert run.score is None, "precondition: nothing was judged"

            # ...and the judge is switched back on afterwards.
            evaluation.scorer_selection = JUDGE_ON
            await s.commit()
            await _compute_run_headlines(s, evaluation.id)
            await s.refresh(run)

        assert run.score is None, (
            "re-enabling the judge rewrote a past judge-off run as 0 — the "
            "worst possible score, asserted for a run that was never graded"
        )
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_genuine_all_zero_judged_run_still_reports_zero():
    """A judged run whose entries all scored 0 reports ``0``, not N/A.

    By value it matches a judge-off run; only the recorded provenance may decide.
    """
    from app.services.evaluation_engine import _compute_run_headlines

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            agent, evaluation = await _seed_evaluation(s, live_selection=JUDGE_ON)
            run = await _seed_run(
                s, agent, evaluation, snapshot=JUDGE_ON, entry_scores=[0.0, 0.0, 0.0]
            )
            await s.commit()
            await _compute_run_headlines(s, evaluation.id)
            await s.refresh(run)

        assert run.score == 0.0, (
            "a real, judged, all-zero run was collapsed into N/A — 0 is a "
            "legitimate result and the worst one, not an absence of result"
        )
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_run_predating_the_column_reads_as_judge_on():
    """A run with a NULL selection reads as judge-on and keeps its score: judge-off
    did not exist before the column.
    """
    from app.services.evaluation_engine import _compute_run_headlines

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            agent, evaluation = await _seed_evaluation(s, live_selection=None)
            run = await _seed_run(
                s,
                agent,
                evaluation,
                snapshot=None,
                entry_scores=[80.0, 90.0],
                set_snapshot=False,
            )
            await s.commit()
            assert run.scorer_selection is None, "precondition: no snapshot"

            await _compute_run_headlines(s, evaluation.id)
            await s.refresh(run)

        assert run.score is not None, (
            "a pre-feature run lost its judge score; NULL was misread as "
            "judge-off, which was not representable when the run executed"
        )
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_runs_of_one_evaluation_keep_their_own_reading():
    """Two runs of one evaluation with different judge provenance each get their own
    answer from a single recompute.
    """
    from app.services.evaluation_engine import _compute_run_headlines

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            agent, evaluation = await _seed_evaluation(s, live_selection=JUDGE_ON)
            judged = await _seed_run(
                s, agent, evaluation, snapshot=JUDGE_ON, entry_scores=[70.0, 90.0]
            )
            unjudged = await _seed_run(
                s, agent, evaluation, snapshot=JUDGE_OFF, entry_scores=[0.0, 0.0]
            )
            await s.commit()
            await _compute_run_headlines(s, evaluation.id)
            await s.refresh(judged)
            await s.refresh(unjudged)

        assert judged.score is not None, "the judged run lost its score"
        assert unjudged.score is None, (
            "both runs collapsed to one answer; the gate is still evaluated "
            "per evaluation rather than per run"
        )
    finally:
        await cleanup()


# ── Where the stamp happens (a real run) ─────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selection", "capture_fails"),
    [
        pytest.param(JUDGE_OFF, False, id="judge_off"),
        # Params are diagnostic and the scoring lineage is not: the capture runs
        # in a SAVEPOINT, so its failure cannot discard the pending stamps. NULL
        # would then read as judge-on, exactly on a judge-off run.
        pytest.param(JUDGE_OFF, True, id="judge_off_with_a_failed_param_capture"),
        pytest.param(JUDGE_ON, False, id="judge_on"),
    ],
)
async def test_a_run_stores_the_selection_it_was_scored_with(monkeypatch, selection, capture_fails):
    run, results = await run_on_postgres(
        monkeypatch, scorer_selection=selection, answers=["Paris."], capture_fails=capture_fails
    )

    assert run.status == "completed"
    assert len(results) == 1
    assert run.scorer_selection == selection, (
        "the run does not record the judge selection it was scored with; NULL "
        "reads as judge-on forever"
    )
    assert run.metric_suite_version is not None


# ── Score lineage: ``EvaluationRun.metric_suite_version`` ─────────────────────────────
# The seam reports the suite's own declaration; the version is stamped where the
# suite runs, not where results are re-aggregated; ``NULL`` on a run scored before
# the column existed survives re-aggregation.


# ── The reported value ───────────────────────────────────────────────────


def test_seam_reports_the_suites_own_declaration():
    from app.evaluation_metrics import __version__ as declared

    assert metric_suite_version() == declared
    # A blank/absent declaration must read as unknown, never as an empty string
    # masquerading as a recorded version.
    assert metric_suite_version(), "the suite declares a non-empty version"


# ── Where the stamp happens (integration) ────────────────────────────────


async def _seed_run_lineage(session, *, status: str, version: str | None = None) -> EvaluationRun:
    agent = Agent(name="A", slug=f"a-{uuid.uuid4().hex[:6]}")
    evaluation = Evaluation(name="E")
    session.add_all([agent, evaluation])
    await session.flush()
    run = EvaluationRun(
        evaluation_id=evaluation.id,
        agent_id=agent.id,
        model_id="m",
        status=status,
        trigger_reason="manual",
        metric_suite_version=version,
    )
    session.add(run)
    await session.flush()
    return run


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_executed_run_records_the_suite_version():
    """Executing a run stamps the version of the suite that scores its entries."""
    from app.services.evaluation_engine import execute_evaluation_run

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            run = await _seed_run_lineage(s, status="pending")
            await seed_provider(s, [run.model_id])
            await s.commit()
            await execute_evaluation_run(s, str(run.id))
        async with sm() as s:
            stored = await s.get(EvaluationRun, run.id)
        assert stored.status == "completed", stored.error
        assert stored.metric_suite_version == metric_suite_version()
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("recorded", [None, "0.0.1"], ids=["pre_lineage_null", "older_version"])
async def test_re_aggregation_keeps_the_recorded_version(recorded):
    """Re-aggregation must not claim a provenance the stored numbers lack.

    ``_compute_run_headlines`` recomputes run aggregates from **stored** entry metrics
    without the suite, so it keeps the version the run was scored with. ``NULL`` on a
    run scored before the column existed stays ``NULL``: backfilling it would assert a
    lineage never observed.
    """
    from app.services.evaluation_engine import _compute_run_headlines

    assert recorded != metric_suite_version()
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            run = await _seed_run_lineage(s, status="completed", version=recorded)
            s.add(EvaluationResult(run_id=run.id, agent_output="", score=80.0))
            await s.commit()
            await _compute_run_headlines(s, run.evaluation_id)
        async with sm() as s:
            stored = await s.get(EvaluationRun, run.id)
        assert stored.score == 80.0, "precondition: the headline pass ran over the run"
        assert stored.metric_suite_version == recorded
    finally:
        await cleanup()


# ── Run mode and the entries that fell back to LLM Only ───────────────────────────────
# The rule is pure and tested first; the reader is tested against PostgreSQL, since
# the ``->>`` read, its guard and the reuse join are SQL.


# ── The rule ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("recorded", "expected"),
    [
        ([], None),
        ([None, None], None),
        (["llm_only", "llm_only"], "llm_only"),
        (["full_loop", "full_loop"], "full_loop"),
        (["full_loop", FALLBACK_MODE], "full_loop"),
        ([FALLBACK_MODE], "full_loop"),
        (["skip_llm"], "skip_llm"),
        # A stored mode that is neither value was run as LLM Only.
        (["full-loop", None], "llm_only"),
    ],
)
def test_a_run_recorded_before_the_column_takes_the_mode_its_results_recorded(recorded, expected):
    assert mode_from_results(recorded) == expected


def test_the_stored_text_is_read_as_postgresql_reads_execution_mode():
    assert _recorded_mode('{"execution_mode": "llm_only_fallback", "x": "\\ud800"}') == FALLBACK_MODE
    assert _recorded_mode('{"execution_mode": 3}') is None
    assert _recorded_mode("[1, 2]") is None
    assert _recorded_mode("not json") is None
    assert _recorded_mode(None) is None


class _NoQueries:
    async def execute(self, *_args, **_kwargs):
        raise AssertionError("a stamped run is read from its column alone")


@pytest.mark.asyncio
async def test_runs_recorded_since_the_column_are_read_without_a_query():
    """Only an unstamped run can hold a fallback, so only those read their results."""
    stamped = [
        SimpleNamespace(id=uuid.uuid4(), execution_mode=mode) for mode in ("llm_only", "full_loop", "skip_llm")
    ]

    modes = await load_run_modes(_NoQueries(), stamped)

    assert [modes[run.id] for run in stamped] == [RunMode(run.execution_mode, 0) for run in stamped]
    assert await load_run_modes(_NoQueries(), []) == {}
    assert await load_run_detail_mode(_NoQueries(), stamped[0]) == (RunMode("llm_only", 0), set())


# ── The reader ───────────────────────────────────────────────────────────

_TABLES = [
    User.__table__,
    Agent.__table__,
    Evaluation.__table__,
    Flow.__table__,
    EvaluationRun.__table__,
    # Referenced by ``evaluation_results``' foreign keys.
    Dataset.__table__,
    DatasetEntry.__table__,
    DatasetVersion.__table__,
    DatasetVersionEntry.__table__,
    EvaluationResult.__table__,
]


async def _evaluation(session):
    user = User(email=f"u-{uuid.uuid4().hex[:8]}@x.io", full_name="A", hashed_password="x", role="admin")
    session.add(user)
    await session.flush()
    agent = Agent(user_id=user.id, name="A", slug=f"a-{uuid.uuid4().hex[:6]}")
    evaluation = Evaluation(name="Eval")
    session.add_all([agent, evaluation])
    await session.flush()
    return evaluation, agent


async def _run(session, evaluation, agent, *, column=None, results=()):
    """A run and its results; each result is ``(recorded mode or None, reused_from or None)``."""
    run = EvaluationRun(
        evaluation_id=evaluation.id,
        agent_id=agent.id,
        model_id="m",
        status="completed",
        trigger_reason="manual",
        execution_mode=column,
    )
    session.add(run)
    await session.flush()
    rows = []
    for recorded, reused_from in results:
        result = EvaluationResult(
            run_id=run.id,
            agent_output="Error: boom" if recorded is None else "answer",
            score=0.0,
            execution_details=None if recorded is None else {"execution_mode": recorded},
            is_reused=reused_from is not None,
            reused_from_result_id=reused_from,
        )
        session.add(result)
        rows.append(result)
    await session.flush()
    return run, rows


@pytest.mark.integration
@pytest.mark.asyncio
async def test_each_run_reads_its_column_or_its_results_and_counts_its_fallbacks():
    async with scratch_database("runmode", tables=_TABLES) as db, db.sessions() as s:
        evaluation, agent = await _evaluation(s)
        stamped, _ = await _run(s, evaluation, agent, column="llm_only")
        legacy_llm_only, _ = await _run(s, evaluation, agent, results=[("llm_only", None), ("llm_only", None)])
        legacy_fallback, fallback_rows = await _run(
            s, evaluation, agent, results=[("full_loop", None), (FALLBACK_MODE, None), ("full_loop", None)]
        )
        only_failed, _ = await _run(s, evaluation, agent, results=[(None, None), (None, None)])
        # A later Full Loop run, also unstamped, reused the fallback's
        # answer and recorded it as its own. A stamped run never reuses a fallback.
        reusing, reused_rows = await _run(
            s, evaluation, agent, results=[("full_loop", fallback_rows[1].id), ("full_loop", None)]
        )
        await s.commit()

        modes = await load_run_modes(s, [stamped, legacy_llm_only, legacy_fallback, only_failed, reusing])

        assert modes[stamped.id] == RunMode("llm_only", 0)
        assert modes[legacy_llm_only.id] == RunMode("llm_only", 0)
        assert modes[legacy_fallback.id] == RunMode("full_loop", 1)
        assert modes[only_failed.id] == RunMode(None, 0)
        assert modes[reusing.id] == RunMode("full_loop", 1)

        # The run page reads the same, and which entries fell back, in one read.
        assert await load_run_detail_mode(s, legacy_fallback) == (modes[legacy_fallback.id], {fallback_rows[1].id})
        assert await load_run_detail_mode(s, reusing) == (modes[reusing.id], {reused_rows[0].id})
        assert await load_run_detail_mode(s, only_failed) == (modes[only_failed.id], set())
        assert await load_run_detail_mode(s, stamped) == (modes[stamped.id], set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_row_postgresql_cannot_parse_is_read_from_its_text_without_aborting_the_page():
    async with scratch_database("runmode", tables=_TABLES) as db, db.sessions() as s:
        evaluation, agent = await _evaluation(s)
        legacy, rows = await _run(s, evaluation, agent, results=[("full_loop", None), ("full_loop", None)])
        other, _ = await _run(s, evaluation, agent, results=[("llm_only", None)])
        # Its answer reused by a later run: the source is the row PostgreSQL cannot parse.
        reusing, reused = await _run(s, evaluation, agent, results=[("full_loop", rows[1].id)])
        await s.commit()
        # Written around the ORM, whose serializer would repair the lone surrogate.
        await s.execute(
            text("UPDATE evaluation_results SET execution_details = CAST(:v AS json) WHERE id = :id"),
            {"v": '{"execution_mode": "llm_only_fallback", "x": "\\ud800"}', "id": rows[1].id},
        )
        await s.commit()

        modes = await load_run_modes(s, [legacy, other, reusing])

        assert modes[legacy.id] == RunMode("full_loop", 1)
        assert modes[other.id] == RunMode("llm_only", 0)
        assert modes[reusing.id] == RunMode("full_loop", 1)
        assert await load_run_detail_mode(s, legacy) == (modes[legacy.id], {rows[1].id})
        assert await load_run_detail_mode(s, reusing) == (modes[reusing.id], {reused[0].id})
