"""Shared support for the evaluation tests: filesystem anchors, typed
``detail`` readers, and the engine doubles several test files share.

The tests read golden corpora, fixture corpora and the library's own source
(several guards parse it with ``ast``). The anchors derive from ``__file__``, not
the working directory, because ``backend/app/evaluation_metrics/`` and
``backend/tests/`` do not share a root. One module makes a layout change one edit.

The import-time check at the bottom matters: ``glob`` over a missing directory
yields nothing and raises nothing, so a guard built on a stale anchor passes while
asserting about no files. That has happened twice, so a bad anchor raises here.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from app.evaluation_metrics.quality import ScoreResult

# ``backend/tests/evaluation_helpers.py`` -> parents[1] is ``backend``.
BACKEND_DIR = Path(__file__).resolve().parents[1]

#: The application package, walked by the seam guard for imports that bypass
#: the ``metric_package`` boundary.
APP_DIR = BACKEND_DIR / "app"

#: The metric suite package itself.
PACKAGE_DIR = APP_DIR / "evaluation_metrics"

#: The flat test tree, ``backend/tests/``.
FLAT_TESTS_DIR = BACKEND_DIR / "tests"

#: Golden and corpus fixtures, flat, with the family in the filename
#: (``prose_results.json``).
FIXTURES_DIR = FLAT_TESTS_DIR / "fixtures" / "metric_suite"

# Turns a silent vacuous pass into an error naming the stale anchor.
for _name, _path in (
    ("APP_DIR", APP_DIR),
    ("BACKEND_DIR", BACKEND_DIR),
    ("FIXTURES_DIR", FIXTURES_DIR),
    ("FLAT_TESTS_DIR", FLAT_TESTS_DIR),
    ("PACKAGE_DIR", PACKAGE_DIR),
):
    if not _path.is_dir():
        msg = (
            f"{_name} resolves to {_path}, which is not a directory. An anchor in "
            f"this module is stale -- repair it here rather than at the call site."
        )
        raise RuntimeError(msg)

__all__ = [
    "APP_DIR",
    "BACKEND_DIR",
    "FIXTURES_DIR",
    "FLAT_TESTS_DIR",
    "PACKAGE_DIR",
    "HeadlinesFake",
    "detail_list",
    "detail_map",
    "patch_evaluation_run",
    "run_on_postgres",
    "seed_provider",
]


# --- typed ``ScoreResult.detail`` readers -------------------------------------
#
# ``detail`` is a ``FrozenMapping`` of JSON values, so indexing yields ``object``.
# These narrow once, here, instead of scattering ``cast`` or ``type: ignore``
# through every assertion.


def detail_list(result: ScoreResult, key: str) -> list[str]:
    """Return a ``detail`` list field as a list of strings."""
    return [str(item) for item in cast("list[Any]", result.detail[key])]


def detail_map(result: ScoreResult, key: str) -> dict[str, str]:
    """Return a ``detail`` mapping field as a string-to-string dict."""
    raw = cast("dict[Any, Any]", result.detail[key])
    return {str(name): str(value) for name, value in raw.items()}


# --- engine doubles -------------------------------------------------------------
#
# ``app`` services are imported inside the functions: the metric suite tests import
# this module too and never load the engine.


class HeadlinesFake:
    """Stands in for ``evaluation_engine._compute_run_headlines``.

    Takes the real parameters by name with the real defaults, records each call's
    arguments in ``calls`` when the call is made (an unawaited call counts), and
    its coroutine returns ``returns`` or raises ``raises``.
    """

    def __init__(self, *, returns: Any = None, raises: BaseException | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.returns = returns
        self.raises = raises
        _check_headlines_signature()

    def __call__(
        self,
        db: Any,
        evaluation_id: Any,
        batch_run_ids: list[str] | None = None,
        *,
        require_completed: bool = True,
        skip_performance_recompute: bool = False,
        commit: bool = True,
    ) -> Awaitable[Any]:
        self.calls.append(
            {
                "db": db,
                "evaluation_id": evaluation_id,
                "batch_run_ids": batch_run_ids,
                "require_completed": require_completed,
                "skip_performance_recompute": skip_performance_recompute,
                "commit": commit,
            }
        )
        return self._outcome()

    async def _outcome(self) -> Any:
        if self.raises is not None:
            raise self.raises
        return self.returns


def _check_headlines_signature() -> None:
    """Raise when ``HeadlinesFake`` no longer takes the real function's parameters.

    Compared only while the engine attribute is still the real function.
    """
    from app.services import evaluation_engine

    real = evaluation_engine._compute_run_headlines
    if getattr(real, "__module__", None) != evaluation_engine.__name__:
        return

    def _shape(func: Callable[..., Any]) -> list[tuple[str, Any, Any]]:
        params = inspect.signature(func).parameters.values()
        return [(p.name, p.kind, p.default) for p in params if p.name != "self"]

    if _shape(HeadlinesFake.__call__) != _shape(real):
        msg = (
            "HeadlinesFake no longer matches evaluation_engine._compute_run_headlines: "
            "update its __call__ signature in tests/evaluation_helpers.py."
        )
        raise AssertionError(msg)


async def _start_episode(_db, *, run_id, entry_id, correlation_id=None):
    return SimpleNamespace(
        run_id=run_id, entry_id=entry_id, correlation_id=correlation_id, status="running", steps=[]
    )


async def _append_step(_db, *, episode, step):
    episode.steps.append(step)


async def _finalize_episode(
    _db,
    *,
    episode,
    final_output,
    participants=None,
    first_turn_output=None,
    error=None,
    timed_out=False,
):
    return None


def patch_evaluation_run(
    monkeypatch: Any,
    *,
    full_loop: Callable[..., Awaitable[Any]],
    score: Callable[..., Awaitable[Any]],
    price_rows: Sequence[dict[str, object]] | None = None,
    start_episode: Callable[..., Awaitable[Any]] = _start_episode,
    append_step: Callable[..., Awaitable[Any]] = _append_step,
    finalize_episode: Callable[..., Awaitable[Any]] = _finalize_episode,
) -> HeadlinesFake:
    """Replace what a fake-session ``execute_evaluation_run`` cannot run for real.

    The agent loop (*full_loop*), the judge (*score*), the price lookup (returns
    *price_rows*), the check that a provider answers the run's model (one does),
    the headline pass and the episode writes. The episode fakes default to an
    in-memory episode. Returns the headline fake.
    """
    from app.services import evaluation_engine as ee

    async def _price_rows(_db, _model_id):
        return list(price_rows or [])

    async def _served(_db, _model_id, _owner_id):
        return True

    headlines = HeadlinesFake()
    monkeypatch.setattr(ee, "_execute_agent_full_loop", full_loop)
    monkeypatch.setattr(ee, "score_result", score)
    monkeypatch.setattr(ee, "_build_run_price_rows", _price_rows)
    monkeypatch.setattr(ee, "run_model_served", _served)
    monkeypatch.setattr(ee, "_compute_run_headlines", headlines)
    service = ee.ConversationContinuationService
    monkeypatch.setattr(service, "start_episode", start_episode)
    monkeypatch.setattr(service, "append_step", append_step)
    monkeypatch.setattr(service, "finalize_episode", finalize_episode)
    return headlines


async def seed_provider(
    session: Any, models: Sequence[str], *, users: Sequence[Any] | None = None, provider: str = "openai"
) -> Any:
    """A *provider* config listing *models*: open to everyone, or with *users* restricted to them."""
    from app.core.security import encrypt_value
    from app.models.llm_config import LLMProviderConfig

    config = LLMProviderConfig(
        provider=provider,
        api_key_enc=encrypt_value("test-key"),
        available_models=list(models),
        access_restricted=users is not None,
        shared_user_ids=[str(user.id) for user in users] if users is not None else None,
    )
    session.add(config)
    await session.flush()
    return config


async def rescore_as_started(session: Any, run_id: Any, **kwargs: Any) -> int:
    """``rescore_run`` on a run the way the rescore endpoints start it.

    They set the run ``running`` and record the status it had, which the rescore hands
    back; it stops on a run that no longer reads ``running``.
    """
    from app.models.evaluation import EvaluationRun
    from app.services import evaluation_engine as ee

    run = await session.get(EvaluationRun, run_id)
    previous_status = run.status
    run.status = "running"
    await session.flush()
    return await ee.rescore_run(session, str(run_id), previous_status=previous_status, **kwargs)


def selects_run_status(statement: Any) -> bool:
    """True for a query reading one run's status: a fake session answers it ``running``.

    A rescore checks before each result that its run still reads ``running``; a
    fake answering every query alike would read that as a cancel.
    """
    columns = list(getattr(statement, "selected_columns", None) or [])
    return (
        len(columns) == 1
        and columns[0].name == "status"
        and getattr(columns[0].table, "name", None) == "evaluation_runs"
    )


async def run_on_postgres(
    monkeypatch: Any,
    *,
    scorer_selection,
    answers,
    capture_fails=False,
    offered_models=("gpt-4o",),
    is_default_model=False,
    skip_llm=False,
    family=None,
    expected_answers=None,
    then=None,
    judge=None,
    dataset_instructions=None,
    file_only=False,
    tool_spans=(),
    pinned=False,
    await_callback=False,
    before_resume=None,
):
    """One real ``execute_evaluation_run`` on PostgreSQL, the agent and the judge stubbed.

    An LLM Only evaluation with one entry per item of ``answers``: each is the
    agent's answer to that entry, or an exception the agent run raises. The run
    is on ``gpt-4o``, which a provider lists unless ``offered_models`` leaves it
    out, and is the agent's default model run with ``is_default_model``; the
    agent is model-independent with ``skip_llm``. ``capture_fails`` makes the
    run-param capture raise. The evaluation has ``family``, and entry ``i`` expects
    ``expected_answers[i]`` (``"a"`` without them). ``then(session, run_id)`` runs
    after the run, before it is read back. ``judge`` replaces the stubbed judge, and
    the dataset has ``dataset_instructions``. With ``file_only`` each entry is a file and no
    prompt. With ``tool_spans`` the agent's turn records its tool calls only as the spans of
    a trace of 1.5 s, under those tool names, and no timeline. With ``pinned`` the run scores a
    snapshot of the entries, whose live expected answers are edited afterwards. With
    ``await_callback`` each entry pauses after its first turn and resumes from a callback
    carrying the same answer, before ``then``, ``before_resume(session, run_id)`` running
    between the pause and the callbacks. Returns the stored run and its results,
    in entry order. Skips if PostgreSQL is unavailable.
    """
    import uuid
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.models.agent import Agent
    from app.models.dataset import Dataset, DatasetEntry, EvaluationDataset
    from app.models.evaluation import Evaluation, EvaluationResult, EvaluationRun
    from app.models.llm_trace import LLMSpan, LLMTrace
    from app.services import evaluation_engine as ee
    from app.services.evaluation import params as run_params
    from tests.scratch_database import ALL_TABLES, scratch_database

    trace_ids: list[str] = []

    async def _agent(_db, _agent, _model_id, input_prompt, *_args, **_kwargs):
        answer = answers[int(input_prompt.removeprefix("Question ")) if input_prompt.startswith("Question ") else 0]
        if isinstance(answer, BaseException):
            raise answer
        return answer, list(trace_ids), None, []

    async def _judge(*_args, **_kwargs):
        return {
            "score": 80.0,
            "sub_scores": {k: 80 for k in ee.SUB_SCORE_WEIGHTS},
            "evaluator_reasoning": "ok",
            "is_judge_failure": False,
        }

    async def _no_price_rows(_db, _model_id):
        return []

    async def _capture_fails(*_args, **_kwargs):
        raise RuntimeError("the run-param capture failed")

    monkeypatch.setattr(ee, "_execute_agent_llm_only", _agent)
    monkeypatch.setattr(ee, "score_result", judge or _judge)
    monkeypatch.setattr(ee, "_build_run_price_rows", _no_price_rows)
    if capture_fails:
        monkeypatch.setattr(run_params, "capture_run_params", _capture_fails)

    async with scratch_database("evalrun", tables=ALL_TABLES, extensions=("vector",)) as db:
        sm = db.sessions
        async with sm() as s:
            agent = Agent(name="Agent", slug=f"a-{uuid.uuid4().hex[:6]}", skip_llm=skip_llm)
            evaluation = Evaluation(
                name="Eval",
                scorer_selection=scorer_selection,
                execution_mode="llm_only",
                family=family,
                trigger_config={"await_callback": True, "callback_timeout_seconds": 3600} if await_callback else None,
            )
            dataset = Dataset(name="ds", evaluation_instructions=dataset_instructions)
            s.add_all([agent, evaluation, dataset])
            await s.flush()
            if offered_models:
                await seed_provider(s, offered_models)
            s.add(EvaluationDataset(evaluation_id=evaluation.id, dataset_id=dataset.id))
            live_entries = [
                DatasetEntry(
                    dataset_id=dataset.id,
                    position=i,
                    input_prompt="" if file_only else f"Question {i}",
                    file_ids=[str(uuid.uuid4())] if file_only else None,
                    expected_answer=expected_answers[i] if expected_answers else "a",
                )
                for i in range(len(answers))
            ]
            s.add_all(live_entries)
            pinned_versions = None
            if pinned:
                from app.models.dataset_version import DatasetVersion, DatasetVersionEntry

                await s.flush()
                version = DatasetVersion(
                    dataset_id=dataset.id, version_number=1, content_hash="h", entry_count=len(live_entries)
                )
                s.add(version)
                await s.flush()
                s.add_all(
                    DatasetVersionEntry(
                        version_id=version.id,
                        entry_id=e.id,
                        position=e.position,
                        input_prompt=e.input_prompt,
                        file_ids=e.file_ids,
                        expected_answer=e.expected_answer,
                    )
                    for e in live_entries
                )
                for e in live_entries:
                    e.expected_answer = "the live answer, edited later"
                pinned_versions = {str(dataset.id): 1}
            if tool_spans:
                trace = LLMTrace(
                    source="evaluation", name="agent turn", started_at=datetime.now(UTC), duration_ms=1500
                )
                s.add(trace)
                await s.flush()
                s.add_all(
                    LLMSpan(trace_id=trace.id, type="TOOL", name=name, position=i, started_at=datetime.now(UTC))
                    for i, name in enumerate(tool_spans)
                )
                trace_ids.append(str(trace.id))
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4o",
                is_default_model=is_default_model,
                trigger_reason="manual",
                status="pending",
                pinned_dataset_versions=pinned_versions,
            )
            s.add(run)
            await s.commit()
            run_id = run.id

        async with sm() as s:
            await ee.execute_evaluation_run(s, str(run_id))
        if await_callback:
            if before_resume is not None:
                async with sm() as s:
                    await before_resume(s, run_id)
                    await s.commit()
            await _resume_every_episode(sm, run_id, answers)
        if then is not None:
            async with sm() as s:
                await then(s, run_id)

        async with sm() as s:
            stored = await s.get(EvaluationRun, run_id)
            results = (
                (
                    await s.execute(
                        select(EvaluationResult)
                        .join(DatasetEntry, DatasetEntry.id == EvaluationResult.entry_id)
                        .where(EvaluationResult.run_id == run_id)
                        .order_by(DatasetEntry.position)
                    )
                )
                .scalars()
                .all()
            )
        return stored, results


async def _resume_every_episode(sm, run_id, answers) -> None:
    """Resume each paused entry of *run_id* from a callback carrying its answer."""
    from sqlalchemy import select

    from app.models.dataset import DatasetEntry
    from app.models.evaluation import EvaluationEpisode
    from app.services.evaluation_continuation import (
        CallbackOutcome,
        ConversationContinuationService,
        process_resume_episode,
    )

    async with sm() as s:
        episodes = (
            await s.execute(
                select(EvaluationEpisode.id, EvaluationEpisode.correlation_id, DatasetEntry.position)
                .join(DatasetEntry, DatasetEntry.id == EvaluationEpisode.entry_id)
                .where(EvaluationEpisode.run_id == run_id)
            )
        ).all()
    assert episodes, "setup: the callback run paused no entry"
    for episode_id, correlation_id, position in episodes:
        async with sm() as s:
            _, outcome = await ConversationContinuationService.resume_from_callback(
                s,
                correlation_id=correlation_id,
                idempotency_key=f"callback-{episode_id}",
                payload={"final_output": answers[position]},
            )
            await s.commit()
        assert outcome is CallbackOutcome.ACCEPTED, f"setup: the callback was {outcome!r}"
        async with sm() as s:
            await process_resume_episode(s, episode_id=str(episode_id))
            await s.commit()
