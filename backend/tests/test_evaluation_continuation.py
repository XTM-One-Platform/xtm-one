import asyncio
import re
import uuid
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import evaluation_continuation as ec


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _CountResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeSession:
    def __init__(self, execute_results):
        self._execute_results = list(execute_results)
        self.commits = 0

    async def execute(self, _statement):
        if not self._execute_results:
            raise AssertionError("Unexpected extra execute() call")
        return self._execute_results.pop(0)

    async def commit(self):
        self.commits += 1


def test_process_resume_episode_without_final_output_keeps_resumed(monkeypatch):
    episode = SimpleNamespace(
        id=uuid.uuid4(),
        status="resumed",
        idempotency_key="the-key",
        callback_payload={"any": "payload"},
        first_turn_output="hello",
        run_id=uuid.uuid4(),
        entry_id=uuid.uuid4(),
    )
    db = _FakeSession(
        [
            # Lock order is run -> episode: the run_id lookup and
            # the run's FOR UPDATE both precede the episode select.
            _ScalarResult(episode.run_id),
            _ScalarResult(None),
            _ScalarResult(episode),
            # The evaluation, for its owner check (an ownerless one here).
            _ScalarResult(None),
        ]
    )

    appended_steps = []

    async def _mock_append_step(_db, *, episode, step):
        appended_steps.append((episode.id, step))

    monkeypatch.setattr(
        ec.ConversationContinuationService,
        "append_step",
        _mock_append_step,
    )

    asyncio.run(
        ec.process_resume_episode(
            cast(AsyncSession, db), episode_id=str(episode.id)
        )
    )

    assert episode.status == "resumed"
    assert len(appended_steps) == 1
    assert "awaiting continuation" in appended_steps[0][1]["description"].lower()
    assert db.commits == 1


def test_process_resume_episode_with_final_output_updates_run_progress(monkeypatch):
    run_id = uuid.uuid4()
    entry_id = uuid.uuid4()
    episode = SimpleNamespace(
        id=uuid.uuid4(),
        status="resumed",
        idempotency_key="the-key",
        callback_payload={"final_output": "final answer"},
        first_turn_output="first answer",
        run_id=run_id,
        entry_id=entry_id,
    )
    eval_result = SimpleNamespace(agent_output="old output")
    run = SimpleNamespace(status="running", completed_entries=0)

    db = _FakeSession(
        [
            # Lock order is run -> episode: the run_id lookup and
            # the run's FOR UPDATE both precede the episode select, in the
            # transaction that stores the answer and in the one that finalizes
            # it after the judge.
            _ScalarResult(episode.run_id),
            _ScalarResult(None),
            _ScalarResult(episode),
            # The evaluation, for its owner check (an ownerless one here).
            _ScalarResult(None),
            _ScalarResult(None),
            _ScalarResult(episode),
        ]
    )

    async def _mock_append_step(_db, *, episode, step):
        return None

    async def _mock_finalize_episode(
        _db,
        *,
        episode,
        final_output,
        participants=None,
        first_turn_output=None,
        error=None,
        timed_out=False,
    ):
        episode.status = "completed"
        episode.final_output = final_output
        episode.first_turn_output = first_turn_output

    async def _mock_store_answer(_db, *, episode, final_output):
        eval_result.agent_output = final_output
        return uuid.uuid4()

    async def _mock_score_answer(_db, *, episode_id, result_id, generation):
        return None

    async def _mock_reconcile_run(_db, *, run_id, fallback_error=None):
        run.completed_entries = 3

    monkeypatch.setattr(
        ec.ConversationContinuationService,
        "append_step",
        _mock_append_step,
    )
    monkeypatch.setattr(
        ec.ConversationContinuationService,
        "finalize_episode",
        _mock_finalize_episode,
    )
    monkeypatch.setattr(ec, "_store_resumed_answer", _mock_store_answer)
    monkeypatch.setattr(ec, "_score_resumed_answer", _mock_score_answer)
    monkeypatch.setattr(ec, "_reconcile_run_status", _mock_reconcile_run)

    asyncio.run(
        ec.process_resume_episode(
            cast(AsyncSession, db), episode_id=str(episode.id)
        )
    )

    assert episode.status == "completed"
    assert eval_result.agent_output == "final answer"
    assert run.completed_entries == 3


def test_finalize_episode_timed_out_sets_error():
    episode = SimpleNamespace(
        status="waiting_callback",
        error=None,
        first_turn_output="first",
        final_output=None,
        participants=[],
        completed_at=None,
    )

    class _DB:
        async def flush(self):
            return None

    asyncio.run(
        ec.ConversationContinuationService.finalize_episode(
            cast(AsyncSession, _DB()),
            episode=episode,
            first_turn_output="first",
            final_output=None,
            timed_out=True,
            error="Callback deadline exceeded",
        )
    )

    assert episode.status == "timed_out"
    assert episode.error == "Callback deadline exceeded"


def test_timeout_waiting_episodes_reconciles_runs(monkeypatch):
    episode = SimpleNamespace(
        id=uuid.uuid4(),
        status="waiting_callback",
        run_id=uuid.uuid4(),
        first_turn_output="hello",
        final_output=None,
    )

    class _Scalars:
        def __init__(self, values):
            self._values = values

        def all(self):
            return self._values

    class _EpisodeResult:
        def scalars(self):
            return _Scalars([episode])

    class _RowsResult:
        """The batch read, which locks the runs: id rows, not entities."""

        def all(self):
            return [SimpleNamespace(id=episode.id)]

    class _DB:
        def __init__(self):
            self.commits = 0
            # The sweep runs two statements: the batch, which locks its RUNS,
            # then the locked episode re-read.
            self._script = [_RowsResult(), _EpisodeResult()]

        async def execute(self, _statement):
            return self._script.pop(0) if self._script else _EpisodeResult()

        async def commit(self):
            self.commits += 1

    db = _DB()
    reconcile_calls = []

    async def _mock_finalize_episode(
        _db,
        *,
        episode,
        final_output,
        participants=None,
        first_turn_output=None,
        error=None,
        timed_out=False,
    ):
        episode.status = "timed_out"
        episode.error = error

    async def _mock_reconcile(_db, *, run_id, fallback_error=None):
        reconcile_calls.append((run_id, fallback_error))

    monkeypatch.setattr(
        ec.ConversationContinuationService,
        "finalize_episode",
        _mock_finalize_episode,
    )
    monkeypatch.setattr(ec, "_reconcile_run_status", _mock_reconcile)

    timed_out = asyncio.run(ec.timeout_waiting_episodes(cast(AsyncSession, db)))

    assert timed_out == 1
    assert db.commits == 1
    assert len(reconcile_calls) == 1
    assert reconcile_calls[0][0] == episode.run_id


def test_every_episode_write_path_locks_its_run_first():
    """Structural guard on the module's lock-order invariant.

    The rule -- run first, then its episodes -- breaks easily and silently:

    * ``process_resume_episode`` and ``fail_abandoned_episode`` once locked the
      episode first and reached the run only via ``_reconcile_run_status``;
    * ``execute_evaluation_run`` (the live loop, outside this module) inverts it,
      which is why the orphan repair uses ``SKIP LOCKED``;
    * an episode lock can be IMPLICIT -- an ``UPDATE`` flushed by
      ``finalize_episode``, not a ``with_for_update`` -- which an audit of lock
      call sites walks straight past.

    That last one is why this test reads the source rather than running the
    code: a write takes the row lock just as surely as an explicit lock, so no
    statement that touches an episode may appear before the run is locked.
    """
    import ast
    import pathlib

    src = pathlib.Path(ec.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    funcs = {
        n.name: n
        for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)
    }

    def _statements(fn):
        seen, out = set(), []
        for node in ast.walk(fn):
            if not isinstance(node, ast.stmt) or node is fn:
                continue
            # Skip docstrings and any bare string literal: this module's
            # docstrings NAME the functions and locks they describe, and
            # matching on prose would make the test read its own explanation.
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                continue
            if node.lineno not in seen:
                seen.add(node.lineno)
                out.append((node.lineno, ast.get_source_segment(src, node) or ""))
        return [seg for _, seg in sorted(out)]

    def _locks_run(seg):
        return "EvaluationRun" in seg and "with_for_update" in seg

    def _writes_episode(seg):
        """Any statement that takes an EvaluationEpisode row lock.

        A bulk ``update(EvaluationEpisode).values(...)`` counts: it is
        neither a helper call nor a ``with_for_update``, but it locks the row
        exactly as hard as both. A read of episodes that locks only their runs
        (``with_for_update(of=EvaluationRun, ...)``) takes no episode lock.
        """
        if "finalize_episode" in seg:
            return True
        if "EvaluationEpisode" not in seg:
            return False
        seg = re.sub(r"with_for_update\(\s*of=EvaluationRun\b[^)]*\)", "", seg)
        return any(
            marker in seg
            for marker in ("with_for_update", "update(", ".values(", "delete(")
        )

    # Every module-level entry point that finalizes an episode. A NEW one that
    # is not listed here fails the coverage assertion below rather than
    # silently escaping the rule.
    guarded = {
        # Closes cancelled runs' open entries. Locks the runs itself
        # rather than trusting its caller: ``cancel_run`` reads the run without
        # a lock, so leaving the order to it would invert this against every
        # path that takes run-then-episode.
        "close_episodes_for_cancelled_runs",
        "process_resume_episode",
        "fail_orphaned_episodes",
        "fail_abandoned_episode",
        "timeout_waiting_episodes",
        # The sweep locks the RUN in its locked re-read and only then reaches
        # for that run's episodes, so it complies with the rule; the
        # ordering assertion below is what checks it.
        "sweep_stranded_runs",
    }
    # Discovery uses the SAME predicate as the ordering check below. Using a
    # narrower one here would let a function whose only episode write is a bulk
    # UPDATE escape ``guarded`` entirely, so it would never even be checked.
    discovered = {
        name
        for name, fn in funcs.items()
        if not name.startswith("_")
        and any(_writes_episode(seg) for seg in _statements(fn))
    }
    assert discovered == guarded, (
        "an episode-writing entry point appeared or vanished; classify it "
        f"against the lock-order rule. expected={sorted(guarded)} "
        f"found={sorted(discovered)}"
    )

    for name in sorted(guarded):
        segments = _statements(funcs[name])
        first_run = next(
            (i for i, seg in enumerate(segments) if _locks_run(seg)), None
        )
        first_episode = next(
            (i for i, seg in enumerate(segments) if _writes_episode(seg)), None
        )
        assert first_run is not None, (
            f"{name} writes an episode but never locks its run — it will "
            "deadlock against every path that does"
        )
        assert first_episode is not None and first_run < first_episode, (
            f"{name} touches an episode before locking its run (run at "
            f"{first_run}, episode at {first_episode}). Lock the run first; "
            "an UPDATE takes the row lock just as with_for_update does."
        )


def test_timeout_waiting_episodes_reconciles_each_run_once(monkeypatch):
    """Two overdue episodes on ONE run reconcile that run exactly once.

    With a single episode, per-episode and per-run reconciliation are
    indistinguishable, so moving the call back inside the finalize loop would
    go unnoticed. The assertion inside the reconcile mock pins the other half:
    reconciliation runs only once EVERY episode of the run is terminal.
    """
    run_id = uuid.uuid4()
    episodes = [
        SimpleNamespace(
            id=uuid.uuid4(),
            status="waiting_callback",
            run_id=run_id,
            first_turn_output="a",
            final_output=None,
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            status="waiting_callback",
            run_id=run_id,
            first_turn_output="b",
            final_output=None,
        ),
    ]

    class _Scalars:
        def all(self):
            return episodes

    class _EpisodeResult:
        def scalars(self):
            return _Scalars()

    class _RowsResult:
        def all(self):
            return [SimpleNamespace(id=e.id) for e in episodes]

    class _DB:
        def __init__(self):
            self.commits = 0
            self._script = [_RowsResult(), _EpisodeResult()]

        async def execute(self, _statement):
            return self._script.pop(0) if self._script else _EpisodeResult()

        async def commit(self):
            self.commits += 1

    db = _DB()
    reconcile_calls: list[uuid.UUID] = []

    async def _mock_finalize(
        _db,
        *,
        episode,
        final_output,
        participants=None,
        first_turn_output=None,
        error=None,
        timed_out=False,
    ):
        episode.status = "timed_out"

    async def _mock_reconcile(_db, *, run_id, fallback_error=None):
        assert all(e.status == "timed_out" for e in episodes), (
            "reconcile ran before every episode of the run was finalized"
        )
        reconcile_calls.append(run_id)

    monkeypatch.setattr(
        ec.ConversationContinuationService, "finalize_episode", _mock_finalize
    )
    monkeypatch.setattr(ec, "_reconcile_run_status", _mock_reconcile)

    assert asyncio.run(ec.timeout_waiting_episodes(cast(AsyncSession, db))) == 2
    assert reconcile_calls == [run_id], (
        f"expected one reconcile for the run, got {len(reconcile_calls)}"
    )
    assert db.commits == 1


def test_timeout_waiting_episodes_skips_locked_rows(monkeypatch):
    """Every candidate locked -> no work, no commit.

    ``skip_locked`` makes this path common. It returns while still holding the
    run locks taken just above, so the commit contract — the caller releases
    them — is load-bearing.
    """

    class _Scalars:
        def all(self):
            return []

    class _EmptyEpisodes:
        def scalars(self):
            return _Scalars()

    class _RowsResult:
        def all(self):
            return [SimpleNamespace(id=uuid.uuid4())]

    class _DB:
        def __init__(self):
            self.commits = 0
            # the batch (runs locked) -> locked episode re-read (all skipped)
            self._script = [_RowsResult(), _EmptyEpisodes()]

        async def execute(self, _statement):
            return self._script.pop(0) if self._script else _EmptyEpisodes()

        async def commit(self):
            self.commits += 1

    db = _DB()
    reconcile_calls: list[uuid.UUID] = []

    async def _mock_reconcile(_db, *, run_id, fallback_error=None):
        reconcile_calls.append(run_id)

    monkeypatch.setattr(ec, "_reconcile_run_status", _mock_reconcile)

    assert asyncio.run(ec.timeout_waiting_episodes(cast(AsyncSession, db))) == 0
    assert db.commits == 0, (
        "the skip-all path must leave the transaction — and its run locks — "
        "to the caller"
    )
    assert reconcile_calls == []


def test_the_stranded_sweep_orders_and_skips_on_every_lock_it_takes():
    """The multi-replica safety the concurrency test cannot reach.

    Dropping the `ORDER BY id` does not fail the concurrency test (measured:
    three runs with the ordering removed all passed). Two coroutines on one
    event loop, over four rows, cannot reproduce a cross-replica lock-ordering
    deadlock; the contenders serialise on the pool long before ordering matters.

    So this asserts the property structurally, the same technique the
    lock-order and ownership rules use on this surface. It does **not** prove
    two replicas cannot deadlock. It proves the two clauses that make that true
    cannot be deleted silently, since neither has a behavioural test that would
    notice.
    """
    import ast
    import inspect

    from app.services import evaluation_continuation as mod

    src = inspect.getsource(mod)
    fn = next(
        n
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "sweep_stranded_runs"
    )
    locking = [
        seg
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "with_for_update"
        and (seg := ast.get_source_segment(src, node.func.value) or "")
    ]
    assert locking, "the sweep takes no row locks at all — has it been rewritten?"

    unordered = [s for s in locking if "order_by" not in s]
    assert not unordered, (
        "a locking statement in sweep_stranded_runs has no ORDER BY. Two "
        "replicas acquiring the same rows in different orders deadlock, and no "
        "test in this suite can reproduce that — the ordering is the whole "
        f"defence. Offending: {unordered}"
    )

    episode_locks = [
        ast.get_source_segment(src, node) or ""
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "with_for_update"
        and "EvaluationEpisode" in (ast.get_source_segment(src, node) or "")
    ]
    assert episode_locks and all("skip_locked" in s for s in episode_locks), (
        "the sweep waits on an episode row. It reaches that lock while already "
        "holding run locks, so blocking there head-of-line blocks the live "
        f"engine's end-of-entry write — a liveness regression: {episode_locks}"
    )


def test_the_run_dispatch_reads_its_row_under_a_lock():
    """Structural, because the interleaving it guards cannot be reproduced.

    ``execute_evaluation_run`` checks ``status != "pending"`` and then writes
    ``"running"``. Unlocked, those are two decisions: a cancellation committing
    between them resurrects the run — the guard has already passed on a pending
    row, and the write puts a CANCELLED run back into ``"running"`` under an
    execution that was itself cancelled.

    The window is between a SELECT returning and the UPDATE committing, with no
    awaitable boundary to hook, so a behavioural test would have to suspend the
    worker mid-transaction; and with the lock in place a concurrent cancel
    simply blocks, so there is nothing to observe either. Asserted structurally
    instead, the same choice and for the same reason as
    ``test_the_stranded_sweep_orders_and_skips_on_every_lock_it_takes``.

    MUTATION CHECK: dropping ``with_for_update()`` from that select fails this.
    """
    import ast
    import pathlib

    from app.services import evaluation_engine

    src = pathlib.Path(evaluation_engine.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "execute_evaluation_run"
    )
    # The first statement in the function that loads the run row.
    loads = [
        seg
        for node in ast.walk(fn)
        if isinstance(node, ast.stmt)
        and "select(EvaluationRun)" in (seg := ast.get_source_segment(src, node) or "")
    ]
    assert loads, (
        "execute_evaluation_run no longer selects EvaluationRun; this guard is "
        "pointed at code that moved"
    )
    assert any("with_for_update" in seg for seg in loads), (
        "execute_evaluation_run reads its run WITHOUT a row lock, so the "
        "'status must be pending' check and the transition to 'running' are two "
        "decisions rather than one. A cancellation committing between them puts "
        "a cancelled run back into 'running' under a cancelled execution"
    )


# ── The verdict a resumed callback stores, on PostgreSQL ──


async def _resume_on_postgres(monkeypatch, judge, *, existing=None, before_resume=None, metric_keys=None):
    """A resumed answer stored and scored for one entry of a real run, judged by *judge*.

    The dataset has instructions, and the run was scored on a snapshot of the entry whose
    expected answer the live entry no longer has. *existing* are the fields of the result
    the first turn stored, against that snapshot, or ``None`` when there is none yet.
    ``before_resume(session)`` runs just before the resume. The stored result's metric
    keys are added to *metric_keys*. Returns the stored result. Skips if PostgreSQL is
    unavailable.
    """
    from sqlalchemy import select

    from app.models.agent import Agent
    from app.models.dataset import Dataset, DatasetEntry
    from app.models.dataset_version import DatasetVersion, DatasetVersionEntry
    from app.models.evaluation import Evaluation, EvaluationEpisode, EvaluationResult, EvaluationRun
    from tests.scratch_database import ALL_TABLES, scratch_database

    monkeypatch.setattr("app.services.evaluation_engine.score_result", judge)
    async with scratch_database("resume", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as s:
            agent = Agent(name="Agent", slug=f"a-{uuid.uuid4().hex[:6]}")
            evaluation = Evaluation(name="Eval", evaluator_model="judge-model")
            dataset = Dataset(name="ds", evaluation_instructions="Accept the city name alone.")
            s.add_all([agent, evaluation, dataset])
            await s.flush()
            entry = DatasetEntry(
                dataset_id=dataset.id, position=0, input_prompt="Capital of France?", expected_answer="the live answer"
            )
            version = DatasetVersion(dataset_id=dataset.id, version_number=1, content_hash="h", entry_count=1)
            s.add_all([entry, version])
            await s.flush()
            snapshot = DatasetVersionEntry(
                version_id=version.id, entry_id=entry.id, position=0, input_prompt="Capital of France?", expected_answer="Paris"
            )
            run = EvaluationRun(
                evaluation_id=evaluation.id, agent_id=agent.id, model_id="gpt-4o", trigger_reason="manual", status="running"
            )
            s.add_all([snapshot, run])
            await s.flush()
            if existing is not None:
                fields = {
                    "agent_output": "first turn",
                    "score": 80.0,
                    "sub_scores": {},
                    "evaluator_reasoning": "r",
                    "is_judge_failure": False,
                    "duration_seconds": 1,
                    **existing,
                }
                s.add(EvaluationResult(run_id=run.id, entry_id=entry.id, version_entry_id=snapshot.id, **fields))
            episode_row = EvaluationEpisode(
                run_id=run.id, entry_id=entry.id, correlation_id=uuid.uuid4().hex, status="resumed"
            )
            s.add(episode_row)
            await s.commit()
            episode = SimpleNamespace(id=episode_row.id, run_id=run.id, entry_id=entry.id, first_turn_details=None)

        if before_resume is not None:
            async with db.sessions() as s:
                await before_resume(s)
                await s.commit()

        async with db.sessions() as s:
            result_id = await ec._store_resumed_answer(s, episode=episode, final_output="Paris")
            await s.commit()
            await ec._score_resumed_answer(s, episode_id=episode.id, result_id=result_id, generation=None)
            await s.commit()

        async with db.sessions() as s:
            from app.models.evaluation_metric import EvaluationMetric

            stored = (
                await s.execute(select(EvaluationResult).where(EvaluationResult.run_id == episode.run_id))
            ).scalar_one()
            if metric_keys is not None:
                keys = await s.execute(select(EvaluationMetric.key).where(EvaluationMetric.result_id == stored.id))
                metric_keys.update(keys.scalars())
            return stored


def _verdict(**over):
    async def _judge(*_a, **_k):
        return {"score": 70.0, "sub_scores": {}, "evaluator_reasoning": "ok", "is_judge_failure": False, **over}

    return _judge


async def _unreachable_judge(*_a, **_k):
    raise RuntimeError("judge unreachable")


def _recording(seen: list[dict]):
    """A judge that notes what it was asked to score against, with a fixed verdict."""

    async def _judge(_db, _output, entry, _model, **kwargs):
        seen.append(
            {
                "expected": entry.expected_answer,
                "instructions": kwargs.get("evaluation_instructions"),
                "tool_calls": [t.get("tool_name") for t in kwargs.get("tool_calls") or []],
                "available_tools": kwargs.get("available_tools"),
            }
        )
        return await _verdict(judge_raw_response="the resumed verdict")()

    return _judge


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "available_tools"),
    [({"available_tools": ["search"]}, ["search"]), ({}, None)],
    ids=["stored_list", "no_stored_list"],
)
async def test_a_resumed_callback_is_judged_on_what_the_first_turn_was_scored_on(monkeypatch, stored, available_tools):
    """The snapshot the run was scored on, the dataset's instructions, the tool calls of the
    stored timeline and the stored tool list, else none: never a free 100."""
    seen: list[dict] = []

    details = {"traces": [{"type": "tool", "tool_name": "search", "duration_ms": 5}], **stored}
    await _resume_on_postgres(monkeypatch, _recording(seen), existing={"execution_details": details})

    assert seen == [
        {
            "expected": "Paris",
            "instructions": "Accept the city name alone.",
            "tool_calls": ["search"],
            "available_tools": available_tools,
        }
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_judge_failure_from_a_callback_is_not_counted_as_a_score(monkeypatch):
    """The callback path sets the flag the headlines filter on: writing the score
    alone would average a judge that reported failure in as a real result."""
    stored = await _resume_on_postgres(monkeypatch, _verdict(score=0.0, is_judge_failure=True), existing={})

    assert stored.is_judge_failure is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_failed_judge_call_does_not_erase_an_earlier_verdict(monkeypatch):
    """A judge call that raised keeps the previous score, and with it its classification:
    the synthesised fallback carries no verdict to overwrite it with."""
    stored = await _resume_on_postgres(
        monkeypatch, _unreachable_judge, existing={"score": 91.0, "is_judge_failure": True}
    )

    assert (stored.score, stored.is_judge_failure) == (91.0, True)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_result_created_by_a_callback_carries_the_judges_verdict(monkeypatch):
    """A resumed callback can arrive before any result row exists."""
    stored = await _resume_on_postgres(monkeypatch, _verdict(score=0.0, is_judge_failure=True))

    assert stored.is_judge_failure is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_row_created_with_no_verdict_is_kept_out_of_the_headline(monkeypatch):
    """A new row whose judge call raised holds a zero nobody measured: excluded, as there is
    no earlier verdict to preserve."""
    stored = await _resume_on_postgres(monkeypatch, _unreachable_judge)

    assert (stored.score, stored.is_judge_failure) == (0.0, True)


def _judge_steps(result):
    return [
        (t.get("type"), t.get("raw_response"))
        for t in (result.execution_details or {}).get("traces") or []
        if t.get("type") in ("judge", "judge_skipped")
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_resumed_callback_records_the_judge_pass_it_ran(monkeypatch):
    """Judge Time, the timeline's judge step and the ``judge_seconds`` row describe the
    callback's verdict, not the first turn's."""
    from app.services.evaluation.metrics import JUDGE_SECONDS_KEY

    keys: set[str] = set()
    first_turn = {"traces": [{"type": "judge_skipped", "description": "awaiting the callback"}]}
    stored = await _resume_on_postgres(
        monkeypatch, _recording([]), existing={"execution_details": first_turn}, metric_keys=keys
    )

    assert _judge_steps(stored) == [("judge", "the resumed verdict")]
    assert "judge_duration_ms" in stored.execution_details
    assert JUDGE_SECONDS_KEY in keys


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_callback_with_no_stored_result_is_judged_on_the_live_entry(monkeypatch):
    """Nothing recorded yet: the live entry and the dataset's instructions, no tool list
    (unknown, never a free 100) and no tool calls, and the new row records its pass."""
    seen: list[dict] = []

    stored = await _resume_on_postgres(monkeypatch, _recording(seen))

    assert seen == [
        {"expected": "the live answer", "instructions": "Accept the city name alone.", "tool_calls": [], "available_tools": None}
    ]
    assert _judge_steps(stored) == [("judge", "the resumed verdict")]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_lookup_failing_in_the_database_takes_the_judges_fallback(monkeypatch):
    """A lookup the database refuses leaves the transaction usable: the resume stores the
    callback's answer and keeps the earlier verdict, as when the judge raises."""
    from sqlalchemy import text

    async def _drop_the_snapshots(session):
        await session.execute(text("DROP TABLE dataset_version_entries CASCADE"))

    stored = await _resume_on_postgres(
        monkeypatch, _recording([]), existing={"score": 91.0}, before_resume=_drop_the_snapshots
    )

    assert (stored.agent_output, stored.score) == ("Paris", 91.0)
    assert stored.evaluator_reasoning == "Resume callback received; judge scoring failed."


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_resumed_answer_drops_the_token_f1_of_the_answer_it_replaced(monkeypatch):
    """The callback's answer replaces the stored one, so the stored one's retired token
    F1 no longer describes the result."""
    stored = await _resume_on_postgres(monkeypatch, _verdict(), existing={"f1_score": 60.0})

    assert (stored.agent_output, stored.f1_score) == ("Paris", None)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_resume_whose_scoring_fails_still_drops_the_replaced_answers_token_f1(monkeypatch):
    """The savepoint undoes a failed scoring, never the answer's replacement, nor the
    F1 that went with the replaced answer."""
    stored = await _resume_on_postgres(
        monkeypatch, _unreachable_judge, existing={"score": 91.0, "f1_score": 60.0}
    )

    assert (stored.agent_output, stored.score, stored.f1_score) == ("Paris", 91.0, None)


@pytest.mark.integration
async def test_the_run_reconcile_reads_the_run_as_committed_not_as_its_session_last_saw_it():
    """A resume's last transaction reconciles a run its session loaded in an earlier one,
    and ``expire_on_commit`` is off: the locked read must return the run as
    committed since. A cancel landing in between stays a cancel, where the session's old
    ``running`` copy, all entries done, would have marked it completed."""
    from datetime import UTC, datetime

    from sqlalchemy import update

    from app.models.agent import Agent
    from app.models.dataset import Dataset, DatasetEntry
    from app.models.evaluation import Evaluation, EvaluationEpisode, EvaluationRun
    from tests.scratch_database import ALL_TABLES, scratch_database

    async with scratch_database("reconcile", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as s:
            agent = Agent(name="Agent", slug=f"a-{uuid.uuid4().hex[:6]}")
            evaluation = Evaluation(name="Eval", evaluator_model="judge-model")
            dataset = Dataset(name="ds")
            s.add_all([agent, evaluation, dataset])
            await s.flush()
            entry = DatasetEntry(dataset_id=dataset.id, position=0, input_prompt="q", expected_answer="a")
            run = EvaluationRun(
                evaluation_id=evaluation.id, agent_id=agent.id, model_id="gpt-4o", trigger_reason="manual",
                status="running", total_entries=1, started_at=datetime.now(UTC),
            )
            s.add_all([entry, run])
            await s.flush()
            s.add(EvaluationEpisode(run_id=run.id, entry_id=entry.id, correlation_id=uuid.uuid4().hex, status="completed"))
            await s.commit()
            run_id = run.id

        async with db.sessions() as resume:
            # Held, as the resume holds the run it loaded: the identity map keeps
            # objects weakly, and a dropped one would be loaded afresh anyway.
            seen = await resume.get(EvaluationRun, run_id)
            assert seen.status == "running"
            await resume.commit()

            async with db.sessions() as cancel:
                await cancel.execute(
                    update(EvaluationRun)
                    .where(EvaluationRun.id == run_id)
                    .values(status="cancelled", error="Cancelled by user", completed_at=datetime.now(UTC))
                )
                await cancel.commit()

            await ec._reconcile_run_status(resume, run_id=run_id)
            await resume.commit()
            assert seen.status == "cancelled"

        async with db.sessions() as s:
            row = await s.get(EvaluationRun, run_id)
            assert (row.status, row.error) == ("cancelled", "Cancelled by user")
