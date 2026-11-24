"""Episode callbacks and the run headlines they write, against a real (throwaway) PostgreSQL database.

``_reconcile_run_status`` and ``_compute_run_headlines`` after a callback, the timeout
sweep, the resume route, the outcome a callback is told, the window in which the run
poll waits for one, and the resume queued in the transaction that accepts it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text

import app.models  # noqa: F401 - register model metadata
from app.models.agent import Agent
from app.models.dataset import Dataset, DatasetEntry
from app.models.evaluation import (
    Evaluation,
    EvaluationEpisode,
    EvaluationResult,
    EvaluationRun,
)
from app.models.user import User
from app.services.evaluation.resume_secret import stored_trigger_config
from tests.evaluation_database import (
    _admin,
    _make_db,
    _stranded_fixture,
    _stub_audit_and_groups,  # noqa: F401 - autouse fixture
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# ── reconcile and run headlines ──


@pytest.mark.asyncio
async def test_a_judge_off_run_reconciled_through_a_callback_has_no_judge_headline():
    """A judge-off run reconciled through a callback gets no judge headline.

    ``evaluation_results.score`` is NOT NULL, so a judge-off entry stores
    ``0.0``; averaging those would give a 0.0 headline for a judge that never
    ran. 0.0 is a real result, the worst one, so it must stay distinct from NULL.
    """
    from app.services.evaluation_continuation import _reconcile_run_status

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(name="Eval-judgeoff", family="STIX_BUNDLE")
            s.add(evaluation)
            await s.flush()
            agent = Agent(user_id=admin.id, name="Agent-judgeoff")
            ds = Dataset(name="ds-judgeoff", family="STIX_BUNDLE")
            s.add_all([agent, ds])
            await s.flush()
            entry = DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a")
            s.add(entry)
            await s.flush()
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4.1",
                status="running",
                trigger_reason="manual",
                total_entries=1,
                started_at=datetime.now(UTC),
                # Judge OFF: ``judge_enabled`` is ``"judge" in selection``, so it
                # takes an explicit empty list; ``None`` means unset, which is
                # judge ON. The gate reads the run's own snapshot.
                scorer_selection=[],
            )
            s.add(run)
            await s.flush()
            # A judge-off entry still stores 0.0, because the column is NOT NULL.
            s.add(
                EvaluationResult(
                    run_id=run.id,
                    entry_id=entry.id,
                    agent_output="out",
                    score=0,
                    sub_scores={},
                    evaluator_reasoning="judge off",
                    duration_seconds=1,
                )
            )
            s.add(
                EvaluationEpisode(
                    run_id=run.id,
                    entry_id=entry.id,
                    correlation_id=f"eval:{run.id}:{entry.id}",
                    status="completed",
                    completed_at=datetime.now(UTC),
                )
            )
            await s.commit()
            run_id = run.id

        async with sm() as s:
            await _reconcile_run_status(s, run_id=run_id)
            await s.commit()

        async with sm() as s:
            after = (
                await s.execute(select(EvaluationRun).where(EvaluationRun.id == run_id))
            ).scalar_one()
            assert after.status == "completed"
            assert after.score is None, (
                f"judge-off run got a fabricated judge headline of {after.score!r} "
                "— the reconcile path must use the canonical, judge-gated writer"
            )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_headlines_refuse_to_hold_an_advisory_lock_past_their_own_commit():
    """``commit=False`` with the performance pass is refused before any I/O.

    ``_recompute_performance_headlines`` takes a transaction-scoped
    ``pg_advisory_xact_lock``. With ``commit=False`` it would be held for the
    caller's whole transaction, serialising every other pass on the evaluation:
    a symptom that shows only as latency under concurrency.
    """
    import app.services.evaluation_engine as ee

    class _ExplodingSession:
        """Any attribute access fails, so the test sees the guard precede I/O.

        ``None`` could not tell "the guard raised" from "it crashed on None", nor
        show the ordering.
        """

        def __getattr__(self, name):
            raise AssertionError(
                f"_compute_run_headlines touched the session (.{name}) before "
                "refusing commit=False without skip_performance_recompute"
            )

    with pytest.raises(ValueError, match="skip_performance_recompute"):
        await ee._compute_run_headlines(
            _ExplodingSession(),
            uuid.uuid4(),
            commit=False,
            skip_performance_recompute=False,
        )


@pytest.mark.asyncio
async def test_reconcile_does_not_rewrite_a_cancelled_run():
    """A reconcile never rewrites a cancelled run's status or reason.

    ``cancel_run`` leaves the episode non-terminal and ``resume_from_callback``
    still accepts a callback for it, so every episode-terminal path can reach a
    reconcile of a cancelled run.
    """
    from app.services.evaluation_continuation import _reconcile_run_status

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(name="Eval-cancel", family="STIX_BUNDLE")
            s.add(evaluation)
            await s.flush()
            agent = Agent(user_id=admin.id, name="Agent-cancel")
            ds = Dataset(name="ds-cancel", family="STIX_BUNDLE")
            s.add_all([agent, ds])
            await s.flush()
            entry = DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a")
            s.add(entry)
            await s.flush()
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4.1",
                status="cancelled",
                error="Cancelled by user",
                trigger_reason="manual",
                total_entries=1,
                started_at=datetime.now(UTC),
            )
            s.add(run)
            await s.flush()
            # A failed episode would otherwise drive the run to ``failed``.
            s.add(
                EvaluationEpisode(
                    run_id=run.id,
                    entry_id=entry.id,
                    correlation_id=f"eval:{run.id}:{entry.id}",
                    status="failed",
                    error="worker lost",
                    completed_at=datetime.now(UTC),
                )
            )
            await s.commit()
            run_id = run.id

        async with sm() as s:
            await _reconcile_run_status(
                s, run_id=run_id, fallback_error="Execution abandoned"
            )
            await s.commit()

        async with sm() as s:
            after = (
                await s.execute(select(EvaluationRun).where(EvaluationRun.id == run_id))
            ).scalar_one()
            assert after.status == "cancelled", (
                f"the user's cancellation was rewritten to {after.status!r}"
            )
            assert after.error == "Cancelled by user", (
                f"the cancellation reason was replaced with {after.error!r}"
            )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_the_timeout_sweep_does_not_overwrite_an_accepted_callback():
    """The timeout sweep never overwrites a callback accepted after its candidate read.

    The interleaving:

    1. the sweep reads the episode as ``waiting_callback``;
    2. ``resume_from_callback`` locks it, stores the payload, commits: the
       caller is told the callback was ACCEPTED;
    3. the sweep must re-check under its lock, not stamp ``timed_out``.

    Otherwise the run fails with the callback's payload in the row.
    """
    from app.services.evaluation_continuation import (
        CallbackOutcome,
        ConversationContinuationService,
        timeout_waiting_episodes,
    )

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(name="Eval-sweep", family="STIX_BUNDLE")
            s.add(evaluation)
            await s.flush()
            agent = Agent(user_id=admin.id, name="Agent-sweep")
            ds = Dataset(name="ds-sweep", family="STIX_BUNDLE")
            s.add_all([agent, ds])
            await s.flush()
            entry = DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a")
            s.add(entry)
            await s.flush()
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4.1",
                status="running",
                trigger_reason="manual",
                total_entries=1,
            )
            s.add(run)
            await s.flush()
            correlation_id = f"eval:{run.id}:{entry.id}"
            s.add(
                EvaluationEpisode(
                    run_id=run.id,
                    entry_id=entry.id,
                    correlation_id=correlation_id,
                    status="waiting_callback",
                    waiting_since=datetime.now(UTC) - timedelta(hours=2),
                    # Already overdue, so the sweep will pick it up.
                    callback_deadline=datetime.now(UTC) - timedelta(hours=1),
                )
            )
            await s.commit()
            episode_id = (
                await s.execute(
                    select(EvaluationEpisode.id).where(
                        EvaluationEpisode.correlation_id == correlation_id
                    )
                )
            ).scalar_one()

        # The callback lands in the GAP, between the sweep's batch read, which
        # locks the run but not the episode, and its locked re-read. Landing it
        # before would be excluded by the batch read and pass with the re-check
        # deleted. It does not wait on the sweep: a callback locks the episode,
        # never the run.
        async def _land_callback():
            async with sm() as s2:
                episode, outcome = (
                    await ConversationContinuationService.resume_from_callback(
                        s2,
                        correlation_id=correlation_id,
                        idempotency_key="accepted-key",
                        payload={"answer": "landed"},
                    )
                )
                # Compare to the enum, not truthiness: every StrEnum outcome is
                # truthy.
                assert episode is not None
                assert outcome is CallbackOutcome.ACCEPTED, (
                    f"the callback was not accepted ({outcome}) — this test's "
                    "premise is that it lands before the sweep"
                )
                await s2.commit()

        async with sm() as s:
            real_execute = s.execute
            calls = {"n": 0}

            async def _execute(stmt, *a, **k):
                result = await real_execute(stmt, *a, **k)
                calls["n"] += 1
                if calls["n"] == 1:
                    # Candidate read done; the callback wins the row now.
                    await _land_callback()
                return result

            s.execute = _execute  # type: ignore[method-assign]
            await timeout_waiting_episodes(s)

        async with sm() as s:
            after = (
                await s.execute(
                    select(EvaluationEpisode).where(EvaluationEpisode.id == episode_id)
                )
            ).scalar_one()
            assert after.status == "resumed", (
                f"the sweep overwrote an accepted callback (status={after.status}) "
                "— the status must be re-checked under the lock"
            )
            assert after.callback_payload == {"answer": "landed"}
    finally:
        await cleanup()


async def test_the_resume_route_persists_a_generation_token(monkeypatch):
    """The callback route persists ``idempotency_key`` in the resume payload.

    ``fail_abandoned_episode`` fails closed without it, so dropping the field
    silently disables the repair. The other tests build the payload by
    hand; this one drives the real route.
    """
    import hashlib
    import hmac
    import json

    import app.api.v1.evaluations as ev_api
    import app.services.orchestration_engine as orch
    from app.api.v1.schemas.evaluation import ResumeEvaluationEpisodeRequest

    captured: dict = {}

    async def _capture_create_execution(db, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    async def _noop_wake():
        return None

    # The route imports these INSIDE the handler: patch the source module.
    monkeypatch.setattr(orch, "create_execution", _capture_create_execution)
    monkeypatch.setattr(orch, "notify_orchestration_wake", _noop_wake)

    secret = "resume-secret"
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(
                name="Eval-resume",
                family="STIX_BUNDLE",
                trigger_config=stored_trigger_config({"resume_secret": secret}),
                timeout_seconds=600,
            )
            s.add(evaluation)
            await s.flush()
            agent = Agent(user_id=admin.id, name="Agent-resume")
            s.add(agent)
            ds = Dataset(name="ds-resume", family="STIX_BUNDLE")
            s.add_all([agent, ds])
            await s.flush()
            entry = DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a")
            s.add(entry)
            await s.flush()
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4.1",
                status="running",
                trigger_reason="manual",
                total_entries=1,
            )
            s.add(run)
            await s.flush()
            correlation_id = f"eval:{run.id}:{entry.id}"
            s.add(
                EvaluationEpisode(
                    run_id=run.id,
                    entry_id=entry.id,
                    correlation_id=correlation_id,
                    status="waiting_callback",
                    waiting_since=datetime.now(UTC),
                )
            )
            await s.commit()

        body_dict = {
            "correlation_id": correlation_id,
            "idempotency_key": "generation-token-1",
            "callback_payload": {"answer": "done"},
        }
        raw = json.dumps(body_dict).encode()
        signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()

        class _Request:
            async def body(self):
                return raw

        async with sm() as s:
            await ev_api.resume_evaluation_episode(
                body=ResumeEvaluationEpisodeRequest(**body_dict),
                request=_Request(),
                x_signature=signature,
                db=s,
            )

        assert captured, "the route did not enqueue a resume execution"
        payload = captured["payload"]
        assert payload["kind"] == "resume_episode"
        assert payload["idempotency_key"] == "generation-token-1", (
            "the generation token must reach the payload — without it "
            "fail_abandoned_episode fails closed and the run stays stranded"
        )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_penalized_mean_has_one_caller_writing_run_headlines():
    """Every ``_penalized_mean`` call lives in ``_compute_run_headlines``.

    A second copy of the headline formula drifted from the canonical one for
    three releases: no ``judge_on`` gate, no anomaly or judge-failure filtering,
    and ``0`` where the canonical writer writes ``None``. Scanning the call sites
    stops a further copy, even a harmless one the next person would copy.
    """
    import ast
    import pathlib

    import app.services.evaluation_engine as ee

    # The whole backend package, not just ``app/``: a caller under
    # ``scripts/`` or ``workers/`` composes headlines just as wrongly. Matching
    # is BY NAME, so an aliased import or a rebinding escapes it.
    #
    # The roots are derived from the tree, not listed, so a new
    # top-level package is scanned without anyone remembering to add it.
    #
    # Excluded: ``tests`` (a test may compute an expected value with the
    # helper), ``sandbox`` (not app code), and dot-dirs: rglob through a
    # ``.venv`` costs ~45s.
    backend = pathlib.Path(ee.__file__).parent.parent.parent
    not_source = {
        "tests",
        "sandbox",
        "__pycache__",
        "node_modules",
        "build",
        "dist",
        "htmlcov",
    }
    roots = sorted(
        d
        for d in backend.iterdir()
        if d.is_dir()
        and d.name not in not_source
        and not d.name.startswith(".")
        # Any virtualenv, whatever its name (``venv``, ``env``): every PEP 405
        # venv has a ``pyvenv.cfg``, and rglob through one costs ~45s.
        and not (d / "pyvenv.cfg").exists()
    )
    assert {d.name for d in roots} >= {"app", "alembic", "scripts"}, (
        f"expected source roots missing from {[d.name for d in roots]} — the "
        "scan would pass while covering less than it claims"
    )
    offenders: list[str] = []

    for root in roots:
        for path in root.rglob("*.py"):
            try:
                src = path.read_text(encoding="utf-8")
                if "_penalized_mean" not in src:
                    continue
                tree = ast.parse(src)
            except (SyntaxError, UnicodeDecodeError):
                # An unparseable file is not this test's to fail on.
                continue
            # Map every function's line span, so a call can be attributed.
            spans = [
                (n.name, n.lineno, n.end_lineno or n.lineno)
                for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)
            ]
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                if name != "_penalized_mean":
                    continue
                enclosing = [
                    n for n, lo, hi in spans if lo <= node.lineno <= hi
                ]
                if "_compute_run_headlines" not in enclosing:
                    offenders.append(
                        f"{path.name}:{node.lineno} inside {enclosing or ['<module>']}"
                    )

    assert not offenders, (
        "_penalized_mean composes run headlines and must be called only from "
        "_compute_run_headlines — a second copy can diverge from it. "
        f"Found: {offenders}"
    )


@pytest.mark.asyncio
async def test_headlines_never_write_a_run_of_another_evaluation():
    """The headline batch never writes a run of another evaluation.

    Every loaded run is scored with the caller's ``eval_row`` (penalty factor,
    weights, family metrics) and written back, so a foreign run id would be
    rescored with the wrong configuration. No caller passes one: the protection
    is a property of the query, scoped by evaluation_id, not of its callers.
    """
    from app.services.evaluation_engine import _compute_run_headlines

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            eval_a = Evaluation(name="Eval-A", family="STIX_BUNDLE", penalty_factor=0.5)
            eval_b = Evaluation(name="Eval-B", family="STIX_BUNDLE", penalty_factor=0.9)
            s.add_all([eval_a, eval_b])
            await s.flush()
            agent = Agent(user_id=admin.id, name="Agent-scope")
            ds = Dataset(name="ds-scope", family="STIX_BUNDLE")
            s.add_all([agent, ds])
            await s.flush()
            entry = DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a")
            s.add(entry)
            await s.flush()
            run_b = EvaluationRun(
                evaluation_id=eval_b.id,
                agent_id=agent.id,
                model_id="gpt-4.1",
                status="completed",
                trigger_reason="manual",
                total_entries=1,
                completed_entries=1,
                score=42.0,
            )
            s.add(run_b)
            await s.flush()
            s.add(
                EvaluationResult(
                    run_id=run_b.id,
                    entry_id=entry.id,
                    agent_output="out",
                    score=10,
                    sub_scores={},
                    evaluator_reasoning="r",
                    duration_seconds=1,
                )
            )
            await s.commit()
            eval_a_id, run_b_id = eval_a.id, run_b.id

        # Evaluation A's id, evaluation B's run. Nothing may be written.
        async with sm() as s:
            await _compute_run_headlines(s, eval_a_id, batch_run_ids=[str(run_b_id)])

        async with sm() as s:
            after = (
                await s.execute(
                    select(EvaluationRun).where(EvaluationRun.id == run_b_id)
                )
            ).scalar_one()
            assert after.score == 42.0, (
                f"a run of another evaluation was rescored to {after.score!r} "
                "with the caller's configuration"
            )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_headline_writes_roll_back_with_the_callers_transaction():
    """Headlines written with ``commit=False`` roll back with the caller.

    This keeps ``fail_orphaned_episodes`` atomic: episode repair, run failure
    and headline write all land or none do. The argument-guard test checks only
    the refused combination, so it cannot see a ``commit()`` here.
    """
    from app.services.evaluation_engine import _compute_run_headlines

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(
                name="Eval-rollback", family="STIX_BUNDLE", penalty_factor=0.5
            )
            s.add(evaluation)
            await s.flush()
            agent = Agent(user_id=admin.id, name="Agent-rollback")
            ds = Dataset(name="ds-rollback", family="STIX_BUNDLE")
            s.add_all([agent, ds])
            await s.flush()
            entry = DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a")
            s.add(entry)
            await s.flush()
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4.1",
                status="completed",
                trigger_reason="manual",
                total_entries=1,
                completed_entries=1,
                score=None,
            )
            s.add(run)
            await s.flush()
            s.add(
                EvaluationResult(
                    run_id=run.id,
                    entry_id=entry.id,
                    agent_output="out",
                    score=80,
                    sub_scores={},
                    evaluator_reasoning="r",
                    duration_seconds=1,
                )
            )
            await s.commit()
            eval_id, run_id = evaluation.id, run.id

        async with sm() as s:
            await _compute_run_headlines(
                s,
                eval_id,
                batch_run_ids=[str(run_id)],
                skip_performance_recompute=True,
                commit=False,
            )
            # Visible inside this transaction: the flush happened.
            pending = (
                await s.execute(
                    select(EvaluationRun.score).where(EvaluationRun.id == run_id)
                )
            ).scalar_one()
            assert pending is not None, (
                "commit=False must still FLUSH, so the caller's own reads see it"
            )
            await s.rollback()

        async with sm() as s:
            after = (
                await s.execute(
                    select(EvaluationRun.score).where(EvaluationRun.id == run_id)
                )
            ).scalar_one()
            assert after is None, (
                f"the headline write survived the caller's rollback (score={after!r}) "
                "— commit=False committed independently instead of flushing"
            )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_a_result_landing_after_a_run_went_terminal_still_reaches_the_headline():
    """A result scored after the run went terminal still reaches its headline.

    The terminal guard freezes status, not measurements. In this topology:

    1. the orphan repair fails the run and writes headlines over the results so far;
    2. the parked ``waiting_callback`` sibling resolves and
       ``_upsert_resumed_result`` persists a NEW result;
    3. reconcile must still run the canonical writer on the ``failed`` run.

    Otherwise the headline averages a subset of the results while
    ``completed_entries`` counts them all.
    """
    from app.services.evaluation_continuation import _reconcile_run_status

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(
                name="Eval-late", family="STIX_BUNDLE", penalty_factor=0.5
            )
            s.add(evaluation)
            await s.flush()
            agent = Agent(user_id=admin.id, name="Agent-late")
            ds = Dataset(name="ds-late", family="STIX_BUNDLE")
            s.add_all([agent, ds])
            await s.flush()
            e1 = DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a")
            e2 = DatasetEntry(dataset_id=ds.id, position=1, expected_answer="b")
            s.add_all([e1, e2])
            await s.flush()
            # Already terminal, as the orphan repair would have left it.
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4.1",
                status="failed",
                error="Execution abandoned",
                trigger_reason="manual",
                total_entries=2,
                completed_entries=1,
                started_at=datetime.now(UTC),
            )
            s.add(run)
            await s.flush()
            for entry, score, status in ((e1, 20, "failed"), (e2, 100, "completed")):
                s.add(
                    EvaluationResult(
                        run_id=run.id,
                        entry_id=entry.id,
                        agent_output="out",
                        score=score,
                        sub_scores={},
                        evaluator_reasoning="r",
                        duration_seconds=1,
                    )
                )
                s.add(
                    EvaluationEpisode(
                        run_id=run.id,
                        entry_id=entry.id,
                        correlation_id=f"eval:{run.id}:{entry.id}",
                        status=status,
                        completed_at=datetime.now(UTC),
                    )
                )
            # The headline as the first reconcile left it: the failed entry only.
            run.score = 20.0
            await s.commit()
            run_id = run.id

        # The late callback's reconcile.
        async with sm() as s:
            await _reconcile_run_status(s, run_id=run_id)
            await s.commit()

        async with sm() as s:
            after = (
                await s.execute(select(EvaluationRun).where(EvaluationRun.id == run_id))
            ).scalar_one()
            assert after.status == "failed", (
                f"the terminal status must still be frozen, got {after.status!r}"
            )
            assert after.error == "Execution abandoned"
            assert after.score is not None and after.score > 20.0, (
                f"the headline still averages a subset of the run's results "
                f"(score={after.score!r}); a result landing after the run went "
                "terminal must be recomputed in"
            )
    finally:
        await cleanup()


@pytest.mark.parametrize("failure", ["top_level", "python", "database"])
@pytest.mark.asyncio
async def test_a_headline_failure_deep_in_the_writer_is_still_contained(
    monkeypatch, caplog, failure
):
    """A failing headline recompute does not stop the run terminalising.

    Reconcile exists to get a run OUT of ``"running"``, and the canonical writer
    reads ``evaluation_metrics``, so its failure must not strand the run.
    Catching alone leaves the session needing a rollback and the caller's commit
    raising ``PendingRollbackError``; the SAVEPOINT keeps the episode repair.

    ``top_level`` fails the writer's first statement with a database error.
    ``python`` and ``database`` fail it deep inside, after its ORM selects ran:
    rolling back a savepoint EXPIRES every instance it touched, so reading
    ``run.id`` in the handler lazy-reloads on a failed session, raises, and the
    run is not terminalised. A failure on the first statement cannot show that.
    """
    import app.services.evaluation_continuation as ec
    import app.services.evaluation_engine as ee

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(name="Eval-deep", family="STIX_BUNDLE")
            s.add(evaluation)
            await s.flush()
            agent = Agent(user_id=admin.id, name="Agent-deep")
            ds = Dataset(name="ds-deep", family="STIX_BUNDLE")
            s.add_all([agent, ds])
            await s.flush()
            entry = DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a")
            s.add(entry)
            await s.flush()
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4.1",
                status="running",
                trigger_reason="manual",
                total_entries=1,
                completed_entries=0,
                started_at=datetime.now(UTC),
            )
            s.add(run)
            await s.flush()
            s.add(
                EvaluationEpisode(
                    run_id=run.id,
                    entry_id=entry.id,
                    correlation_id=f"eval:{run.id}:{entry.id}",
                    status="completed",
                    completed_at=datetime.now(UTC),
                )
            )
            await s.commit()
            run_id = run.id

        # A plain exception leaves the transaction healthy; a DB error aborts
        # it, and the savepoint rollback has to restore it enough for the
        # handler's ``refresh`` and the caller's commit to run. A stub that
        # merely raised at the top would pass under a bare try/except too.
        calls = 0

        async def _boom(db_arg, *a, **k):
            nonlocal calls
            calls += 1
            if failure != "python":
                await db_arg.execute(text("SELECT * FROM nope_not_a_table"))
            raise RuntimeError("quality metric composition failed")

        if failure == "top_level":
            monkeypatch.setattr(ee, "_compute_run_headlines", _boom)
        else:
            monkeypatch.setattr(ee, "compute_quality_headline", _boom)

        with caplog.at_level(
            logging.WARNING, logger="app.services.evaluation_continuation"
        ):
            async with sm() as s:
                await ec._reconcile_run_status(s, run_id=run_id)
                # The caller's commit still works: a bare try/except would break it.
                await s.commit()

        # Without these two the test passes on the happy path, injected failure
        # or not. The patch is by module attribute, so it also goes
        # silent if the call site moves to a local import or the helper is
        # renamed.
        assert calls, "the injected failure was never reached — nothing was tested"
        assert "Headline recompute failed for run" in caplog.text, (
            "the failure did not take the best-effort branch"
        )

        async with sm() as s:
            after = (
                await s.execute(select(EvaluationRun).where(EvaluationRun.id == run_id))
            ).scalar_one()
            assert after.status == "completed", (
                f"a headline failure escaped and left the run at {after.status!r}"
            )
            assert after.completed_at is not None
            # The savepoint's rollback keeps the progress counter: a
            # terminal run is never reconciled again.
            assert after.completed_entries == 1
    finally:
        await cleanup()


# ── callback outcomes, the poll window and the resume transaction ──


@pytest.mark.parametrize(
    "terminal_status,expected,why",
    [
        (
            "failed",
            "discarded",
            "closed before the callback arrived, with no callback ever "
            "applied — nobody handled it, so calling it a duplicate tells the "
            "caller the opposite of what happened",
        ),
        (
            "timed_out",
            "discarded",
            "same shape by a different route: the callback deadline passed, "
            "the episode was closed, and this answer is dropped",
        ),
        (
            "completed",
            "duplicate",
            "the episode finished on its own, so a late callback changes "
            "nothing and nothing was lost — a genuine duplicate",
        ),
    ],
)
async def test_a_callback_for_an_already_closed_episode_is_not_a_duplicate(
    terminal_status, expected, why
):
    """A callback for an episode closed without one is discarded, not a duplicate.

    A terminal episode says nothing about whether THIS caller's work was handled.
    If it COMPLETED, a late callback is harmless. If it closed with no callback
    applied, the payload is thrown away, and "duplicate" would tell the caller
    otherwise. Nothing stored says what closed the episode: three unrelated
    paths write ``failed``.
    """
    from app.services.evaluation_continuation import (
        CallbackOutcome,
        ConversationContinuationService,
    )

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(sm, episodes=("running",))
        async with sm() as s:
            episode = (
                await s.execute(
                    select(EvaluationEpisode).where(EvaluationEpisode.run_id == run_id)
                )
            ).scalar_one()
            correlation_id = episode.correlation_id
            # The repair wins the race: the episode is terminal before the
            # callback is even seen.
            episode.status = terminal_status
            episode.completed_at = datetime.now(UTC)
            await s.commit()

        async with sm() as s:
            _, outcome = await ConversationContinuationService.resume_from_callback(
                s,
                correlation_id=correlation_id,
                idempotency_key="late-key",
                payload={"final_output": "the answer the caller thinks landed"},
            )

        assert outcome is CallbackOutcome(expected), (
            f"an episode {terminal_status!r} reported {outcome!r} to its "
            f"caller — {why}"
        )
    finally:
        await cleanup()


async def test_a_repeat_of_the_same_callback_stays_a_duplicate():
    """A repeat of an applied callback stays a duplicate, even once the episode is
    terminal: that payload WAS handled.
    """
    from app.services.evaluation_continuation import (
        CallbackOutcome,
        ConversationContinuationService,
    )

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(sm, episodes=("waiting_callback",))
        async with sm() as s:
            correlation_id = (
                await s.execute(
                    select(EvaluationEpisode.correlation_id).where(
                        EvaluationEpisode.run_id == run_id
                    )
                )
            ).scalar_one()

        async with sm() as s:
            _, first = await ConversationContinuationService.resume_from_callback(
                s,
                correlation_id=correlation_id,
                idempotency_key="same-key",
                payload={"final_output": "answer"},
            )
            await s.commit()
        assert first is CallbackOutcome.ACCEPTED, f"first call was {first!r}"

        # The run is then closed underneath it: the episode is terminal when the
        # retry arrives.
        async with sm() as s:
            episode = (
                await s.execute(
                    select(EvaluationEpisode).where(EvaluationEpisode.run_id == run_id)
                )
            ).scalar_one()
            episode.status = "failed"
            await s.commit()

        async with sm() as s:
            _, second = await ConversationContinuationService.resume_from_callback(
                s,
                correlation_id=correlation_id,
                idempotency_key="same-key",
                payload={"final_output": "answer"},
            )

        assert second is CallbackOutcome.DUPLICATE, (
            f"a retry of an already-applied callback reported {second!r}; its "
            "payload WAS handled, so this is the one case 'duplicate' has "
            "always described correctly"
        )
    finally:
        await cleanup()


@pytest.mark.parametrize(
    "repair_first,expect_gone,why",
    [
        (
            True,
            True,
            "the repair closed the episode first, so this payload is dropped "
            "and the caller must be told so",
        ),
        (
            False,
            False,
            "the callback got there first, so it is accepted and the repair "
            "must leave it alone",
        ),
    ],
)
async def test_the_callback_endpoint_tells_its_caller_which_side_won(
    repair_first, expect_gone, why
):
    """The callback endpoint answers ``410 Gone`` when a repair won, 200 when the
    callback did.

    A caller treating 2xx as "delivered" learns nothing from a body field. Both
    orderings run, since either alone proves little. Driven through the route
    function (this module has no HTTP harness), with the HMAC computed as the
    endpoint computes it, so the signature path runs.
    """
    import hashlib
    import hmac
    import json

    from fastapi import HTTPException

    from app.api.v1.evaluations import resume_evaluation_episode
    from app.api.v1.schemas.evaluation import ResumeEvaluationEpisodeRequest
    from app.services.evaluation_continuation import (
        TERMINAL_EPISODE_STATUSES,
        timeout_waiting_episodes,
    )

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(sm, episodes=("waiting_callback",))
        secret = "s3cret"
        async with sm() as s:
            run = await s.get(EvaluationRun, run_id)
            evaluation = await s.get(Evaluation, run.evaluation_id)
            evaluation.trigger_config = stored_trigger_config({"resume_secret": secret})
            episode = (
                await s.execute(
                    select(EvaluationEpisode).where(EvaluationEpisode.run_id == run_id)
                )
            ).scalar_one()
            correlation_id = episode.correlation_id
            await s.commit()

        # Overdue in BOTH orderings, so the repair would take the episode either
        # way: callback-first must be protected by the callback, not by a
        # deadline not yet passed.
        async with sm() as s:
            episode = (
                await s.execute(
                    select(EvaluationEpisode).where(EvaluationEpisode.run_id == run_id)
                )
            ).scalar_one()
            episode.waiting_since = datetime.now(UTC) - timedelta(hours=2)
            episode.callback_deadline = datetime.now(UTC) - timedelta(hours=1)
            await s.commit()

        if repair_first:
            # Through the janitor that owns a ``waiting_callback`` episode, not a
            # hand-stamped row: a REPAIR gets there first.
            async with sm() as s:
                took = await timeout_waiting_episodes(s)
            assert took, (
                "the repair took no episode, so this ordering is not the one "
                "the test means to exercise"
            )

        payload = {
            "correlation_id": correlation_id,
            "idempotency_key": "k-1",
            "callback_payload": {"final_output": "the answer"},
        }
        raw = json.dumps(payload).encode()
        signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()

        class _Request:
            async def body(self):
                return raw

        async with sm() as s:
            call = resume_evaluation_episode(
                body=ResumeEvaluationEpisodeRequest(**payload),
                request=_Request(),
                x_signature=signature,
                db=s,
            )
            if expect_gone:
                with pytest.raises(HTTPException) as caught:
                    await call
                assert caught.value.status_code == 410, (
                    f"the losing callback got {caught.value.status_code}, not "
                    f"410 — {why}"
                )
                assert "not a duplicate" in caught.value.detail, (
                    f"the 410 does not distinguish abandonment from a "
                    f"duplicate: {caught.value.detail!r}"
                )
            else:
                result = await call
                assert result["accepted"] is True and result["duplicate"] is False, (
                    f"the winning callback was not accepted ({result}) — {why}"
                )

        if not expect_gone:
            # The other half of "callback first": the repair then finds nothing
            # to overwrite.
            async with sm() as s:
                took = await timeout_waiting_episodes(s)
            async with sm() as s:
                episode = (
                    await s.execute(
                        select(EvaluationEpisode).where(
                            EvaluationEpisode.run_id == run_id
                        )
                    )
                ).scalar_one()
            assert took == 0, (
                f"the repair took {took} episode(s) after the callback was "
                "accepted — it re-read the row and closed it anyway, which is "
                "the work-losing interleaving this ordering exists to rule "
                "out. The deadline IS overdue here, so this is the janitor "
                "declining a resumed episode, not declining an unexpired one"
            )
            # Protection is TWO-fold: ``resume_from_callback`` moves the status
            # off ``waiting_callback`` AND clears ``callback_deadline``, and the
            # janitor's predicate needs both. Only removing both fails here.
            assert episode.status not in TERMINAL_EPISODE_STATUSES, (
                f"the accepted callback's episode reads {episode.status!r} "
                "after the repair ran; its payload is now unreachable"
            )
    finally:
        await cleanup()


async def test_a_callback_that_reported_an_error_is_not_blamed_on_a_repair():
    """A callback whose own payload failed the episode is not reported ``ABANDONED``.

    ``process_resume_episode`` finalises an episode as ``failed`` when the
    callback's payload carries an error: a callback was applied. Only
    ``idempotency_key`` tells the cases apart: ``resume_from_callback`` is its
    sole writer, so its presence means a callback was applied here.
    """
    from app.services.evaluation_continuation import (
        CallbackOutcome,
        ConversationContinuationService,
        process_resume_episode,
    )

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(sm, episodes=("waiting_callback",))
        async with sm() as s:
            episode = (
                await s.execute(
                    select(EvaluationEpisode).where(EvaluationEpisode.run_id == run_id)
                )
            ).scalar_one()
            correlation_id, episode_id = episode.correlation_id, episode.id

        # A real callback, carrying a real error, processed the normal way.
        async with sm() as s:
            _, first = await ConversationContinuationService.resume_from_callback(
                s,
                correlation_id=correlation_id,
                idempotency_key="first-key",
                payload={"error": "the outside system could not answer"},
            )
            await s.commit()
        assert first is CallbackOutcome.ACCEPTED, f"setup: first call {first!r}"

        async with sm() as s:
            await process_resume_episode(s, episode_id=str(episode_id))

        async with sm() as s:
            after = await s.get(EvaluationEpisode, episode_id)
        assert after.status == "failed", (
            f"setup: the error callback left the episode {after.status!r}, not "
            "failed — this test no longer exercises the case it names"
        )

        async with sm() as s:
            _, second = await ConversationContinuationService.resume_from_callback(
                s,
                correlation_id=correlation_id,
                idempotency_key="second-key",
                payload={"final_output": "a later answer"},
            )

        assert second is CallbackOutcome.DUPLICATE, (
            f"reported {second!r}: no repair touched this episode — a callback "
            "was accepted and processed, and it reported an error. Calling it "
            "abandoned blames the platform for the outside system's answer"
        )
    finally:
        await cleanup()


async def test_an_unknown_correlation_id_returns_an_outcome_not_a_bool():
    """An unknown correlation id returns a CallbackOutcome, not a bool.

    Every path returns one of the three outcomes the annotation promises, so a
    direct caller of the service never gets a fourth, non-enum value.
    """
    from app.services.evaluation_continuation import (
        CallbackOutcome,
        ConversationContinuationService,
    )

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            episode, outcome = (
                await ConversationContinuationService.resume_from_callback(
                    s,
                    correlation_id="nothing-has-this-correlation-id",
                    idempotency_key="k",
                    payload={},
                )
            )
        assert episode is None
        assert isinstance(outcome, CallbackOutcome), (
            f"got {outcome!r} ({type(outcome).__name__}), not a CallbackOutcome"
        )
        assert outcome is CallbackOutcome.NOT_FOUND, f"got {outcome!r}"
    finally:
        await cleanup()


async def test_the_locked_reread_sees_a_repair_that_landed_after_the_preload():
    """The locked re-read sees a repair that landed after the preload.

    The endpoint loads the episode unlocked first, to reach the resume secret,
    which puts it in the identity map. Without ``populate_existing=True`` the
    ``FOR UPDATE`` re-read waits for the repair, then returns the stale
    ``status`` and ``idempotency_key``, and the callback is ACCEPTED into a
    closed episode. The interleaving is real: preload in the request session,
    repair committed from another, re-read on the first. No patching.
    """
    from app.services.evaluation_continuation import (
        CallbackOutcome,
        ConversationContinuationService,
        timeout_waiting_episodes,
    )

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(sm, episodes=("waiting_callback",))
        async with sm() as s:
            episode = (
                await s.execute(
                    select(EvaluationEpisode).where(EvaluationEpisode.run_id == run_id)
                )
            ).scalar_one()
            episode.waiting_since = datetime.now(UTC) - timedelta(hours=2)
            episode.callback_deadline = datetime.now(UTC) - timedelta(hours=1)
            correlation_id = episode.correlation_id
            await s.commit()

        async with sm() as request_session:
            # 1. What the endpoint does before it ever calls the service.
            preloaded = (
                await request_session.execute(
                    select(EvaluationEpisode).where(
                        EvaluationEpisode.correlation_id == correlation_id
                    )
                )
            ).scalar_one()
            assert preloaded.status == "waiting_callback", (
                f"setup: preloaded {preloaded.status!r} — the row must be open "
                "at this point for the race to exist"
            )

            # 2. A repair closes it, from a different session, and commits.
            async with sm() as repair_session:
                took = await timeout_waiting_episodes(repair_session)
            assert took == 1, f"setup: the repair took {took} episodes, expected 1"

            # 3. The locked re-read, on the session holding the stale instance.
            _, outcome = await ConversationContinuationService.resume_from_callback(
                request_session,
                correlation_id=correlation_id,
                idempotency_key="arrives-late",
                payload={"final_output": "an answer nobody will ever read"},
            )

        assert outcome is CallbackOutcome.DISCARDED, (
            f"reported {outcome!r}: the locked re-read returned the instance "
            "loaded before the repair, so the classification ran on values "
            "that were already false when it read them"
        )
    finally:
        await cleanup()


@pytest.mark.parametrize(
    "episode_status,deadline_offset,expected,why",
    [
        ("waiting_callback", timedelta(hours=1), True, "parked, deadline ahead"),
        # ``resumed`` is covered by
        # ``test_a_resumed_episode_holds_the_poll_only_while_its_resume_is_live``:
        # its answer turns on a live resume execution, not a deadline.
        (
            # Expired but NOT yet swept, so still pending: resume_from_callback
            # accepts any non-terminal episode whatever its deadline, and a
            # callback landing now recomputes the headlines. The sweep,
            # not the clock, closes this window.
            "waiting_callback",
            timedelta(hours=-1),
            True,
            "overdue but unswept: a callback arriving now is still accepted, so "
            "reporting not-pending would stop the poll just before it lands",
        ),
        (
            "completed",
            timedelta(hours=1),
            False,
            "terminal: nothing further can arrive for this episode",
        ),
        (
            # Unsweepable, so not pending: timeout_waiting_episodes needs a
            # deadline, and a poll held on a row nothing can close is unbounded.
            # pause_for_callback always sets one; the arm is fail-safe.
            "waiting_callback",
            None,
            False,
            "parked with no deadline at all: the sweep cannot close it, so "
            "polling on it would never end",
        ),
    ],
)
async def test_the_callback_window_is_open_only_while_one_can_still_land(
    episode_status, deadline_offset, expected, why
):
    """The callback window is open only while a callback can still land.

    A run advertising "pending" forever makes the page poll forever. The
    deadline bounds it: ``timeout_waiting_episodes`` terminalises the episode
    once it passes, whether or not the outside system answers.
    """
    from app.services.evaluation_continuation import is_callback_pending_for_run

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(sm, episodes=(episode_status,))
        async with sm() as s:
            episode = (
                await s.execute(
                    select(EvaluationEpisode).where(EvaluationEpisode.run_id == run_id)
                )
            ).scalar_one()
            episode.callback_deadline = (
                None if deadline_offset is None else datetime.now(UTC) + deadline_offset
            )
            # ``resume_from_callback`` always stamps this when it accepts. The
            # resumed arm reads it, and treats NULL as not pending: fail-safe,
            # not unbounded.
            episode.resumed_at = datetime.now(UTC)
            await s.commit()

        async with sm() as s:
            pending = await is_callback_pending_for_run(s, str(run_id))

        assert pending is expected, (
            f"an episode {episode_status!r} with deadline_offset="
            f"{deadline_offset} reported {pending} — {why}"
        )
    finally:
        await cleanup()


async def test_a_run_with_no_episode_at_all_does_not_hold_the_poll_open():
    """A run with no episode does not hold the poll open.

    Most runs never park. A flag defaulting true would make the page poll every
    terminal run forever.
    """
    from app.services.evaluation_continuation import is_callback_pending_for_run

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(sm)
        async with sm() as s:
            assert await is_callback_pending_for_run(s, str(run_id)) is False
    finally:
        await cleanup()


async def test_every_headline_field_a_callback_writes_is_refreshed():
    """Every field a callback writes is refreshed by ``get_run``'s re-read.

    ``get_run`` refreshes a HAND-LISTED set of fields when ``callback_pending``
    is false. The whole response is compared to one built from a clean session,
    so a field added to the callback write path and not to the re-read fails
    here, by name. Rebuilding the response instead would cost four queries on
    every terminal run-detail view.

    Not covered: result-derived aggregates (``avg_sub_scores``,
    ``reused_entries``, ``judge_failure_count``) go stale only if a result lands
    between ``_build_run_response`` and the pending check, a window inside the
    request this test does not reach.
    """
    from app.api.v1.evaluations import _build_run_response, get_run

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(sm, episodes=("completed",))
        async with sm() as s:
            run = await s.get(EvaluationRun, run_id)
            run.status = "completed"
            run.score = 10.0
            run.quality_score = 10.0
            run.f1_score = 0.1
            run.completed_entries = 1
            run.headline_details = {
                "quality_metric_keys": ["old"],
                "quality_contributions": {"old": 1.0},
                "metric_entry_counts": {"old": 1},
                "excluded": ["old"],
            }
            await s.commit()

        async with sm() as request_session:
            stale = (
                await request_session.execute(
                    select(EvaluationRun).where(EvaluationRun.id == run_id)
                )
            ).scalar_one()
            assert stale.score == 10.0, "setup: the run must start stale-able"

            # Everything a resolving callback rewrites, changed at once.
            async with sm() as other:
                landing = await other.get(EvaluationRun, run_id)
                landing.score = 90.0
                landing.quality_score = 90.0
                landing.f1_score = 0.9
                landing.completed_entries = 3
                landing.error = "recomputed"
                landing.headline_details = {
                    "quality_metric_keys": ["fresh"],
                    "quality_contributions": {"fresh": 2.0},
                    "metric_entry_counts": {"fresh": 3},
                    "excluded": ["fresh"],
                }
                # A RESULT as well, as ``_upsert_resumed_result`` writes when a
                # callback resolves, so the result-derived aggregates are
                # compared too.
                entry_id = (
                    await other.execute(
                        select(DatasetEntry.id)
                        .join(
                            EvaluationEpisode,
                            EvaluationEpisode.entry_id == DatasetEntry.id,
                        )
                        .where(EvaluationEpisode.run_id == run_id)
                    )
                ).scalar_one()
                other.add(
                    EvaluationResult(
                        run_id=run_id,
                        entry_id=entry_id,
                        agent_output="landed",
                        score=90,
                        sub_scores={"accuracy": 90},
                        evaluator_reasoning="r",
                        duration_seconds=1,
                    )
                )
                await other.commit()

            served = await get_run(
                run_id=str(run_id), user=None, db=request_session
            )

        # What the endpoint should serve, from a session that never saw the
        # stale row.
        async with sm() as clean:
            truth_run = (
                await clean.execute(
                    select(EvaluationRun).where(EvaluationRun.id == run_id)
                )
            ).scalar_one()
            truth = await _build_run_response(clean, truth_run)

        stale_fields = {
            key: (served.get(key), truth[key])
            for key in truth
            if key in served and served.get(key) != truth[key]
        }
        assert not stale_fields, (
            "the re-read left these fields at their pre-callback values, so "
            "its hand-listed set is incomplete: "
            + ", ".join(
                f"{k} served={v[0]!r} actual={v[1]!r}" for k, v in stale_fields.items()
            )
        )
    finally:
        await cleanup()


@pytest.mark.parametrize(
    "with_live_execution,expected,why",
    [
        (
            True,
            True,
            "a resume execution is still in flight, and the headline it will "
            "write is the whole reason to keep polling",
        ),
        (
            False,
            False,
            "no execution is coming: a callback carrying neither output nor "
            "error leaves process_resume_episode in its awaiting-continuation "
            "branch, so the episode stays resumed and its execution COMPLETES. "
            "A later callback could still rewrite the run, but nothing bounds "
            "that state: holding the poll means polling forever, and "
            "closing the episode breaks the continuation flow. The page "
            "self-corrects on focus or reload",
        ),
    ],
    ids=["resume-in-flight", "no-execution-coming"],
)
async def test_a_resumed_episode_holds_the_poll_only_while_its_resume_is_live(
    with_live_execution, expected, why
):
    """A ``resumed`` episode holds the poll only while its resume execution is live.

    Acceptance clears ``callback_deadline``, so ``resumed`` must count as pending
    while the headline write is imminent. Status alone is unbounded:
    nothing sweeps the awaiting-continuation shape. A non-terminal execution
    means the resume is still coming, and the engine bounds it: complete,
    dead-letter or deadline each end this arm.
    """
    from app.models.orchestration import OrchestrationExecution
    from app.services.evaluation_continuation import is_callback_pending_for_run

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(sm, episodes=("resumed",))
        async with sm() as s:
            episode = (
                await s.execute(
                    select(EvaluationEpisode).where(EvaluationEpisode.run_id == run_id)
                )
            ).scalar_one()
            # What acceptance leaves behind: resumed, deadline cleared.
            episode.callback_deadline = None
            episode.resumed_at = datetime.now(UTC)
            episode_id = episode.id
            if with_live_execution:
                s.add(
                    OrchestrationExecution(
                        adapter="evaluation",
                        state="pending",
                        payload={
                            "kind": "resume_episode",
                            "episode_id": str(episode_id),
                            "idempotency_key": "k",
                        },
                        attempt_count=0,
                    )
                )
            await s.commit()

        async with sm() as s:
            pending = await is_callback_pending_for_run(s, str(run_id))

        assert pending is expected, (
            f"a resumed episode with{'' if with_live_execution else 'out'} a "
            f"live resume execution reported {pending} — {why}"
        )
    finally:
        await cleanup()


async def test_accepting_a_callback_and_queueing_its_resume_are_one_transaction():
    """Accepting a callback and queueing its resume land together.

    Between two commits the episode would be ``resumed`` with no execution, so
    ``callback_pending`` reads false and the client stops polling before the
    resume rewrites the headlines. This covers the happy path; the end state is
    the same with split commits, so
    ``test_a_failed_enqueue_does_not_leave_a_callback_accepted`` pins the
    atomicity.
    """
    import hashlib
    import hmac
    import json

    from app.api.v1.evaluations import resume_evaluation_episode
    from app.api.v1.schemas.evaluation import ResumeEvaluationEpisodeRequest
    from app.models.orchestration import OrchestrationExecution
    from app.services.evaluation_continuation import is_callback_pending_for_run

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(sm, episodes=("waiting_callback",))
        secret = "s3cret"
        async with sm() as s:
            run = await s.get(EvaluationRun, run_id)
            evaluation = await s.get(Evaluation, run.evaluation_id)
            evaluation.trigger_config = stored_trigger_config({"resume_secret": secret})
            episode = (
                await s.execute(
                    select(EvaluationEpisode).where(EvaluationEpisode.run_id == run_id)
                )
            ).scalar_one()
            episode.callback_deadline = datetime.now(UTC) + timedelta(hours=1)
            correlation_id = episode.correlation_id
            await s.commit()

        payload = {
            "correlation_id": correlation_id,
            "idempotency_key": "k-1",
            "callback_payload": {"final_output": "the answer"},
        }
        raw = json.dumps(payload).encode()
        signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()

        class _Request:
            async def body(self):
                return raw

        async with sm() as s:
            result = await resume_evaluation_episode(
                body=ResumeEvaluationEpisodeRequest(**payload),
                request=_Request(),
                x_signature=signature,
                db=s,
            )
        assert result["accepted"] is True and result["duplicate"] is False

        async with sm() as s:
            episode = (
                await s.execute(
                    select(EvaluationEpisode).where(EvaluationEpisode.run_id == run_id)
                )
            ).scalar_one()
            queued = (
                await s.execute(
                    select(func.count(OrchestrationExecution.id)).where(
                        OrchestrationExecution.adapter == "evaluation",
                        OrchestrationExecution.payload.op("->>")("episode_id")
                        == str(episode.id),
                    )
                )
            ).scalar_one()
            pending = await is_callback_pending_for_run(s, str(run_id))

        assert episode.status == "resumed", f"setup: episode is {episode.status!r}"
        assert queued == 1, (
            "the callback was accepted but no resume execution exists — the "
            "answer was taken and then silently dropped, with nothing that "
            "repairs it"
        )
        assert pending is True, (
            "the run reports no pending callback while its episode is resumed "
            "and unprocessed, so the page stops polling before the headlines "
            "are rewritten"
        )
    finally:
        await cleanup()


async def test_a_failed_enqueue_does_not_leave_a_callback_accepted(monkeypatch):
    """A failed enqueue rolls back the callback's acceptance.

    The episode stays waiting, so the outside system can retry. A separate
    commit would leave it ``resumed`` with no resume queued and nothing to
    repair it: ``timeout_waiting_episodes`` takes only ``waiting_callback``, and
    ``fail_abandoned_episode`` needs an execution to dead-letter.
    """
    import hashlib
    import hmac
    import json

    from app.api.v1.evaluations import resume_evaluation_episode
    from app.api.v1.schemas.evaluation import ResumeEvaluationEpisodeRequest
    from app.services import orchestration_engine

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(sm, episodes=("waiting_callback",))
        secret = "s3cret"
        async with sm() as s:
            run = await s.get(EvaluationRun, run_id)
            evaluation = await s.get(Evaluation, run.evaluation_id)
            evaluation.trigger_config = stored_trigger_config({"resume_secret": secret})
            episode = (
                await s.execute(
                    select(EvaluationEpisode).where(EvaluationEpisode.run_id == run_id)
                )
            ).scalar_one()
            episode.callback_deadline = datetime.now(UTC) + timedelta(hours=1)
            correlation_id = episode.correlation_id
            await s.commit()

        async def _boom(*_a, **_k):
            raise RuntimeError("the orchestration insert failed")

        # The route imports it from the engine at call time.
        monkeypatch.setattr(orchestration_engine, "create_execution", _boom)

        payload = {
            "correlation_id": correlation_id,
            "idempotency_key": "k-1",
            "callback_payload": {"final_output": "the answer"},
        }
        raw = json.dumps(payload).encode()
        signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()

        class _Request:
            async def body(self):
                return raw

        with pytest.raises(RuntimeError):
            async with sm() as s:
                await resume_evaluation_episode(
                    body=ResumeEvaluationEpisodeRequest(**payload),
                    request=_Request(),
                    x_signature=signature,
                    db=s,
                )

        async with sm() as s:
            episode = (
                await s.execute(
                    select(EvaluationEpisode).where(EvaluationEpisode.run_id == run_id)
                )
            ).scalar_one()

        assert episode.status == "waiting_callback", (
            f"the episode reads {episode.status!r} after the enqueue failed: the "
            "callback was accepted with no resume queued and nothing to repair "
            "it, so the outside system's answer is lost and it was told 200"
        )
        assert episode.callback_deadline is not None, (
            "the deadline was cleared by an acceptance that should have rolled "
            "back, so timeout_waiting_episodes can no longer reach this episode "
            "either"
        )
    finally:
        await cleanup()


# ── No run lock across a model call, none waited on by the reconciler ──


@pytest.mark.parametrize(
    ("held_episodes", "limit"),
    [
        pytest.param(1, None, id="one-overdue-episode"),
        # The held run's overdue episodes are the oldest and fill the whole
        # batch: picked before locking, they would be the batch on every tick.
        pytest.param(2, 2, id="a-full-batch-of-overdue-episodes"),
    ],
)
async def test_the_timeout_sweep_skips_a_run_another_transaction_holds(held_episodes, limit):
    """The janitor never waits on a run another transaction locks, nor starves behind it.

    ``periodic()`` is awaited inline in the reconciler cycle every adapter shares:
    waiting there on a run held by a resume would stall every adapter's claims and
    the outbox. The held run is passed over before the batch is counted, so
    another run's overdue episode is timed out in the same pass, and the held
    run's on the first pass after it is released.
    """
    import asyncio

    from sqlalchemy import update

    from app.services.evaluation_continuation import timeout_waiting_episodes

    sm, cleanup = await _make_db()
    try:
        held_run, _, _ = await _stranded_fixture(sm, episodes=("waiting_callback",) * held_episodes)
        free_run, _, _ = await _stranded_fixture(sm, episodes=("waiting_callback",))
        async with sm() as s:
            for run_id, overdue_by in ((held_run, timedelta(hours=1)), (free_run, timedelta(minutes=1))):
                await s.execute(
                    update(EvaluationEpisode)
                    .where(EvaluationEpisode.run_id == run_id)
                    .values(callback_deadline=datetime.now(UTC) - overdue_by)
                )
            await s.commit()

        async with sm() as holder:
            await holder.execute(select(EvaluationRun.id).where(EvaluationRun.id == held_run).with_for_update())
            async with sm() as s:
                took = await asyncio.wait_for(timeout_waiting_episodes(s, limit=limit), 5)
                await s.rollback()
            await holder.rollback()

        async with sm() as s:
            statuses = (await s.execute(select(EvaluationEpisode.run_id, EvaluationEpisode.status))).all()
        assert took == 1, f"the sweep took {took} episode(s) while the held run had {held_episodes} overdue"
        assert [status for run_id, status in statuses if run_id == free_run] == ["timed_out"]
        assert [status for run_id, status in statuses if run_id == held_run] == ["waiting_callback"] * held_episodes

        async with sm() as s:
            assert await timeout_waiting_episodes(s, limit=limit) == held_episodes, (
                "the held run's episodes were not taken once it was released"
            )
    finally:
        await cleanup()


async def test_a_resume_holds_no_run_lock_while_the_judge_runs(monkeypatch):
    """A callback resume judges its answer with no lock on the run.

    Another session can lock the run while the judge runs: the janitor, a cancel
    and the emergency stop never wait on a model call. The answer is still
    judged, stored and its episode finalized.
    """
    from sqlalchemy.exc import DBAPIError

    from app.services import evaluation_engine as ee
    from app.services.evaluation_continuation import (
        ConversationContinuationService,
        process_resume_episode,
    )

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(sm, episodes=("waiting_callback",))
        async with sm() as s:
            episode = (
                await s.execute(select(EvaluationEpisode).where(EvaluationEpisode.run_id == run_id))
            ).scalar_one()
            episode_id = episode.id
            await ConversationContinuationService.resume_from_callback(
                s,
                correlation_id=episode.correlation_id,
                idempotency_key="the-key",
                payload={"final_output": "the answer"},
            )
            await s.commit()

        run_free_during_judge: list[bool] = []

        async def _judge(*_args, **_kwargs):
            async with sm() as other:
                try:
                    await other.execute(
                        select(EvaluationRun.id).where(EvaluationRun.id == run_id).with_for_update(nowait=True)
                    )
                    run_free_during_judge.append(True)
                except DBAPIError:
                    run_free_during_judge.append(False)
                await other.rollback()
            return {"score": 90, "sub_scores": {}, "evaluator_reasoning": "judged"}

        monkeypatch.setattr(ee, "score_result", _judge)
        async with sm() as s:
            await process_resume_episode(s, episode_id=str(episode_id))

        assert run_free_during_judge == [True], "the run was locked while the judge ran"
        async with sm() as s:
            after = await s.get(EvaluationEpisode, episode_id)
            result = (
                await s.execute(select(EvaluationResult).where(EvaluationResult.run_id == run_id))
            ).scalar_one()
        assert after.status == "completed"
        assert (result.agent_output, result.score, result.is_judge_failure) == ("the answer", 90, False)
    finally:
        await cleanup()


async def _a_resumed_episode(sm) -> tuple[uuid.UUID, uuid.UUID]:
    """A run whose one episode a callback resumed with an answer. Returns ``(run_id, episode_id)``."""
    from app.services.evaluation_continuation import ConversationContinuationService

    run_id, _, _ = await _stranded_fixture(sm, episodes=("waiting_callback",))
    async with sm() as s:
        episode = (await s.execute(select(EvaluationEpisode).where(EvaluationEpisode.run_id == run_id))).scalar_one()
        await ConversationContinuationService.resume_from_callback(
            s, correlation_id=episode.correlation_id, idempotency_key="the-key", payload={"final_output": "the answer"}
        )
        await s.commit()
        return run_id, episode.id


@pytest.mark.parametrize("change", [{"role": "user"}, {"is_active": False}], ids=["demoted", "deactivated"])
async def test_a_resume_whose_owner_may_no_longer_run_it_judges_nothing(monkeypatch, change):
    """The answer is judged as the evaluation's owner: an owner no longer an
    active admin closes the episode with the launch's reason, and no judge runs."""
    from app.services import evaluation_continuation as ec
    from app.services import evaluation_engine as ee
    from app.services.evaluation.launch import OWNER_CANNOT_RUN

    sm, cleanup = await _make_db()
    try:
        run_id, episode_id = await _a_resumed_episode(sm)
        async with sm() as s:
            owner = User(email="owner@x.io", full_name="Owner", hashed_password="x", role="admin")
            for column, value in change.items():
                setattr(owner, column, value)
            s.add(owner)
            await s.flush()
            run = await s.get(EvaluationRun, run_id)
            (await s.get(Evaluation, run.evaluation_id)).user_id = owner.id
            await s.commit()
        judged: list[str] = []

        async def _judge(*_args, **_kwargs):
            judged.append("judge")
            return {"score": 90, "sub_scores": {}, "evaluator_reasoning": "judged"}

        monkeypatch.setattr(ee, "score_result", _judge)
        async with sm() as s:
            await ec.process_resume_episode(s, episode_id=str(episode_id))

        async with sm() as s:
            episode = await s.get(EvaluationEpisode, episode_id)
        assert judged == []
        assert (episode.status, episode.error) == ("failed", OWNER_CANNOT_RUN)
    finally:
        await cleanup()


async def _cancel_the_run(sm, run_id: uuid.UUID) -> None:
    """What cancelling a run writes: its status, then its open episodes closed."""
    from app.services.evaluation_continuation import close_episodes_for_cancelled_run

    async with sm() as other:
        run = await other.get(EvaluationRun, run_id)
        run.status, run.error = "cancelled", "Cancelled by user"
        await close_episodes_for_cancelled_run(other, run_id=run_id)
        await other.commit()


async def _states(sm, run_id: uuid.UUID, episode_id: uuid.UUID) -> tuple[str, str]:
    async with sm() as s:
        return (await s.get(EvaluationRun, run_id)).status, (await s.get(EvaluationEpisode, episode_id)).status


async def test_a_resume_cancelled_before_its_answer_is_judged_calls_no_judge(monkeypatch):
    """A cancel landing between the store and the judge stops the judge call:
    nothing is billed on the stopped run, and its closed episode stays closed."""
    from app.services import evaluation_continuation as ec
    from app.services import evaluation_engine as ee

    sm, cleanup = await _make_db()
    try:
        run_id, episode_id = await _a_resumed_episode(sm)
        judged: list[str] = []

        async def _judge(*_args, **_kwargs):
            judged.append("judge")
            return {"score": 90, "sub_scores": {}, "evaluator_reasoning": "judged"}

        score = ec._score_resumed_answer

        async def _cancelled_first(db, **kwargs):
            await _cancel_the_run(sm, run_id)
            await score(db, **kwargs)

        monkeypatch.setattr(ee, "score_result", _judge)
        monkeypatch.setattr(ec, "_score_resumed_answer", _cancelled_first)
        async with sm() as s:
            await ec.process_resume_episode(s, episode_id=str(episode_id))

        assert judged == [], "the judge was called on a run cancelled before its answer was judged"
        assert await _states(sm, run_id, episode_id) == ("cancelled", "failed")
    finally:
        await cleanup()


async def test_a_resume_cancelled_while_the_judge_runs_leaves_the_episode_closed(monkeypatch):
    """A cancel landing during the judge call decides the outcome: the finalize
    step finds the episode closed and leaves it, so the run stays cancelled."""
    from app.services import evaluation_engine as ee
    from app.services.evaluation_continuation import process_resume_episode

    sm, cleanup = await _make_db()
    try:
        run_id, episode_id = await _a_resumed_episode(sm)

        async def _judge(*_args, **_kwargs):
            await _cancel_the_run(sm, run_id)
            return {"score": 90, "sub_scores": {}, "evaluator_reasoning": "judged"}

        monkeypatch.setattr(ee, "score_result", _judge)
        async with sm() as s:
            await process_resume_episode(s, episode_id=str(episode_id))

        assert await _states(sm, run_id, episode_id) == ("cancelled", "failed")
    finally:
        await cleanup()


async def test_a_newer_callback_landing_while_the_judge_runs_wins(monkeypatch):
    """A callback with a new key accepted during the judge call replaces the answer:
    the first resume drops its verdict and leaves the episode to the newer
    callback's resume, which applies and scores the newer answer."""
    from app.services import evaluation_engine as ee
    from app.services.evaluation_continuation import (
        CallbackOutcome,
        ConversationContinuationService,
        process_resume_episode,
    )

    sm, cleanup = await _make_db()
    try:
        run_id, episode_id = await _a_resumed_episode(sm)
        async with sm() as s:
            correlation_id = (await s.get(EvaluationEpisode, episode_id)).correlation_id
        outcomes: list[CallbackOutcome] = []
        verdicts = iter([10, 90])

        async def _judge(*_args, **_kwargs):
            if not outcomes:  # the newer callback lands during the first judge call
                async with sm() as other:
                    _, outcome = await ConversationContinuationService.resume_from_callback(
                        other, correlation_id=correlation_id, idempotency_key="newer-key", payload={"final_output": "second"}
                    )
                    await other.commit()
                    outcomes.append(outcome)
            return {"score": next(verdicts), "sub_scores": {}, "evaluator_reasoning": "judged"}

        monkeypatch.setattr(ee, "score_result", _judge)
        async with sm() as s:
            await process_resume_episode(s, episode_id=str(episode_id))

        assert outcomes == [CallbackOutcome.ACCEPTED]
        async with sm() as s:
            episode = await s.get(EvaluationEpisode, episode_id)
            result = (await s.execute(select(EvaluationResult).where(EvaluationResult.run_id == run_id))).scalar_one()
        assert episode.status == "resumed", "the first resume finalized the episode over the newer callback"
        assert result.score != 10, "the first answer's verdict landed after a newer answer replaced it"

        # The newer callback's own resume execution.
        async with sm() as s:
            await process_resume_episode(s, episode_id=str(episode_id))
        async with sm() as s:
            episode = await s.get(EvaluationEpisode, episode_id)
            result = (await s.execute(select(EvaluationResult).where(EvaluationResult.run_id == run_id))).scalar_one()
        assert episode.status == "completed"
        assert (result.agent_output, result.score, result.is_judge_failure) == ("second", 90, False)
    finally:
        await cleanup()


async def _land_a_newer_callback(sm, episode_id: uuid.UUID, final_output: str = "second") -> None:
    """A callback with a new key for the episode, accepted and committed."""
    from app.services.evaluation_continuation import ConversationContinuationService

    async with sm() as other:
        correlation_id = (await other.get(EvaluationEpisode, episode_id)).correlation_id
        await ConversationContinuationService.resume_from_callback(
            other, correlation_id=correlation_id, idempotency_key="newer-key", payload={"final_output": final_output}
        )
        await other.commit()


async def test_a_newer_callback_landing_before_the_judge_call_spares_it(monkeypatch):
    """A newer callback accepted between the store and the judge makes the first resume
    skip its judge call; the newer callback's resume judges its own answer."""
    from app.services import evaluation_continuation as ec
    from app.services import evaluation_engine as ee

    sm, cleanup = await _make_db()
    try:
        run_id, episode_id = await _a_resumed_episode(sm)
        judged: list[str] = []

        async def _judge(*_args, **_kwargs):
            judged.append("judge")
            return {"score": 90, "sub_scores": {}, "evaluator_reasoning": "judged"}

        score = ec._score_resumed_answer

        async def _newer_first(db, **kwargs):
            await _land_a_newer_callback(sm, episode_id)
            await score(db, **kwargs)

        monkeypatch.setattr(ee, "score_result", _judge)
        monkeypatch.setattr(ec, "_score_resumed_answer", _newer_first)
        async with sm() as s:
            await ec.process_resume_episode(s, episode_id=str(episode_id))
        assert judged == [], "the first resume judged an answer a newer callback had replaced"
        assert (await _states(sm, run_id, episode_id))[1] == "resumed"

        monkeypatch.setattr(ec, "_score_resumed_answer", score)
        async with sm() as s:
            await ec.process_resume_episode(s, episode_id=str(episode_id))
        async with sm() as s:
            result = (await s.execute(select(EvaluationResult).where(EvaluationResult.run_id == run_id))).scalar_one()
        assert judged == ["judge"]
        assert (await _states(sm, run_id, episode_id))[1] == "completed"
        assert (result.agent_output, result.score) == ("second", 90)
    finally:
        await cleanup()


async def test_a_judge_failure_after_a_newer_callback_scored_keeps_the_newer_verdict(monkeypatch):
    """The first resume's judge call fails after a newer callback's resume stored and
    scored its answer: the failure is not written over the newer verdict."""
    from app.services import evaluation_engine as ee
    from app.services.evaluation_continuation import process_resume_episode

    sm, cleanup = await _make_db()
    try:
        run_id, episode_id = await _a_resumed_episode(sm)
        calls: list[str] = []

        async def _judge(*_args, **_kwargs):
            calls.append("judge")
            if len(calls) == 1:  # the first resume's call: the newer callback runs to the end, then this one fails
                await _land_a_newer_callback(sm, episode_id)
                async with sm() as newer:
                    await process_resume_episode(newer, episode_id=str(episode_id))
                raise TimeoutError("the judge timed out")
            return {"score": 90, "sub_scores": {}, "evaluator_reasoning": "the newer verdict"}

        monkeypatch.setattr(ee, "score_result", _judge)
        async with sm() as s:
            await process_resume_episode(s, episode_id=str(episode_id))

        async with sm() as s:
            result = (await s.execute(select(EvaluationResult).where(EvaluationResult.run_id == run_id))).scalar_one()
        assert calls == ["judge", "judge"]
        assert (await _states(sm, run_id, episode_id))[1] == "completed"
        assert (result.agent_output, result.score, result.evaluator_reasoning) == ("second", 90, "the newer verdict")
    finally:
        await cleanup()
