import asyncio
import json
import logging
import time
import uuid
from types import SimpleNamespace

import pytest

from app.services import evaluation_continuation, integration_tools
from app.services import evaluation_engine as ee
from app.services.prompt_versioning import DEFAULT_PERSONA_SNAPSHOT_MARKER
from tests.evaluation_helpers import (
    HeadlinesFake,
    patch_evaluation_run,
    rescore_as_started,
    run_on_postgres,
    selects_run_status,
)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _EntriesResult:
    def __init__(self, entries):
        self._entries = entries

    def scalars(self):
        return self

    def all(self):
        return self._entries


class _CountResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


def _is_out_of_band_run_query(statement) -> bool:
    """True for ``execute_evaluation_run``'s per-entry cancellation check.

    These fakes answer ``execute()`` POSITIONALLY, from a scripted queue, so any
    new query inside the run loop shifts every later answer and breaks tests
    that are about judge scoring and episode bookkeeping, not cancellation. The
    probe is orthogonal to what they script -- it fires once per entry and asks
    one question -- so it is answered out of band and does NOT consume the
    queue, leaving each test's sequence meaning exactly what its author wrote.

    Answered "not cancelled". The cooperative stop itself is covered against a
    REAL database in ``test_evaluations.py`` (``cancel_run``) and
    ``test_evaluation_lease.py`` (the cancel event, the emergency stop, the real
    sequential loop), where the status can actually change mid-run; a positional
    fake cannot express that.
    """
    columns = list(getattr(statement, "selected_columns", None) or [])
    return (
        len(columns) == 1
        and columns[0].name in ("status", "id")
        and getattr(columns[0].table, "name", None) == "evaluation_runs"
    )


class _StatusResult:
    def __init__(self, status):
        self._status = status

    def scalar_one_or_none(self):
        return self._status


class _FakeSession:
    def __init__(
        self, execute_results, cancel_after_entries=None, cancelled_on_refresh=False
    ):
        self._execute_results = list(execute_results)
        # When set, the per-entry cancellation probe starts answering
        # ``"cancelled"`` once this many entries have been probed -- i.e. the
        # user cancelled the run while entry N+1 was about to start.
        self.cancel_after_entries = cancel_after_entries
        self.cancellation_probes = 0
        self.run_locks = 0
        self.cancelled_on_refresh = cancelled_on_refresh
        self.commits = 0
        self.rollbacks = 0
        self.added = []

    async def execute(self, _statement):
        if _is_out_of_band_run_query(_statement):
            columns = list(getattr(_statement, "selected_columns", None) or [])
            if columns[0].name != "status":
                # The pre-episode-write run lock. Counted separately from the
                # cancellation probe -- folding them together would silently
                # double every ``cancellation_probes`` assertion below. Its
                # result is never read by the caller.
                self.run_locks += 1
                return _StatusResult(None)
            self.cancellation_probes += 1
            stopped = (
                self.cancel_after_entries is not None
                and self.cancellation_probes > self.cancel_after_entries
            )
            return _StatusResult("cancelled" if stopped else "running")
        if not self._execute_results:
            raise AssertionError("Unexpected extra execute() call")
        return self._execute_results.pop(0)

    async def scalar(self, _statement):
        # No General Assistant row: the platform default iteration count.
        return None

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def refresh(self, _obj):
        # The failure handler refreshes the run before labelling it. When the
        # user cancelled mid-attempt, that read returns ``"cancelled"`` from the
        # database -- simulated here, since the fake has no database.
        if self.cancelled_on_refresh:
            _obj.status = "cancelled"
            _obj.error = "Cancelled by user"
        return None

    def add(self, obj):
        self.added.append(obj)


def test_get_llm_provider_and_model_uses_router_resolution(monkeypatch):
    provider = SimpleNamespace(provider_name="openai")
    calls = []

    async def _mock_resolve_provider_for_model(_db, model_id, *, user_id=None):
        calls.append((model_id, user_id))
        return provider, model_id

    monkeypatch.setattr(
        "app.llm.router.resolve_provider_for_model",
        _mock_resolve_provider_for_model,
    )

    llm, model = asyncio.run(
        ee._get_llm_provider_and_model(_FakeSession([]), "gpt-5-mini", user_id="owner")
    )

    assert llm is provider
    assert model == "gpt-5-mini"
    assert calls == [("gpt-5-mini", "owner")]


def test_evaluation_owner_id_is_the_owner_or_no_account():
    assert ee.evaluation_owner_id(SimpleNamespace(user_id="u-1")) == "u-1"
    assert ee.evaluation_owner_id(SimpleNamespace(user_id=None)) is None
    assert ee.evaluation_owner_id(None) is None


def test_get_evaluator_llm_returns_provider_name_from_llm(monkeypatch):
    llm = SimpleNamespace(provider_name="openai")

    async def _mock_get_llm_provider_and_model(_db, _model, **_kwargs):
        return llm, "judge-model"

    monkeypatch.setattr(ee, "_get_llm_provider_and_model", _mock_get_llm_provider_and_model)

    resolved_llm, model, provider_name = asyncio.run(
        ee._get_evaluator_llm(_FakeSession([]), "judge-model")
    )

    assert resolved_llm is llm
    assert model == "judge-model"
    assert provider_name == "openai"


def test_get_evaluator_llm_falls_back_to_unknown_provider_name(monkeypatch):
    llm_without_provider_name = object()

    async def _mock_get_llm_provider_and_model(_db, _model, **_kwargs):
        return llm_without_provider_name, "judge-model"

    monkeypatch.setattr(ee, "_get_llm_provider_and_model", _mock_get_llm_provider_and_model)

    resolved_llm, model, provider_name = asyncio.run(
        ee._get_evaluator_llm(_FakeSession([]), "judge-model")
    )

    assert resolved_llm is llm_without_provider_name
    assert model == "judge-model"
    assert provider_name == "unknown"


def test_resolve_run_snapshot_overrides_keeps_explicit_null_config_markers():
    run = SimpleNamespace(
        agent_id=uuid.uuid4(),
        flow_id=None,
        persona_version_number=None,
        prompt_versions=None,
        skill_versions=None,
        placebo_comparison_id=None,
        arm="real",
        agent_config_versions={"max_iterations": None, "temperature": None},
    )
    db = _FakeSession(execute_results=[])

    overrides, missing = asyncio.run(ee.resolve_run_snapshot_overrides(db, run))

    assert missing == []
    assert overrides["max_iterations"] is None
    assert overrides["temperature"] is None


def test_resolve_run_snapshot_overrides_keeps_default_persona_marker():
    run = SimpleNamespace(
        agent_id=uuid.uuid4(),
        flow_id=None,
        persona_version_number=DEFAULT_PERSONA_SNAPSHOT_MARKER,
        prompt_versions=None,
        skill_versions=None,
        placebo_comparison_id=None,
        arm="real",
        agent_config_versions=None,
    )
    db = _FakeSession(execute_results=[])

    overrides, missing = asyncio.run(ee.resolve_run_snapshot_overrides(db, run))

    assert missing == []
    assert overrides["persona"] == ""


def _drive_full_run(
    monkeypatch,
    *,
    cancel_after_entries=None,
    finalize_error=None,
    cancelled_on_refresh=False,
):
    """Drive one real ``execute_evaluation_run`` over a two-entry dataset.

    Shared by the completion and cancellation tests below; the assertions
    stay with each test.

    *cancel_after_entries*: answer the per-entry cancellation probe with
    ``"cancelled"`` once this many entries have been probed.
    """
    run_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    evaluation_id = uuid.uuid4()

    run = SimpleNamespace(
        id=run_id,
        status="pending",
        agent_id=agent_id,
        flow_id=None,
        evaluation_id=evaluation_id,
        model_id="gpt-test",
        is_default_model=False,
        persona_version_number=None,
        prompt_versions=None,
        skill_versions=None,
        placebo_comparison_id=None,
        arm="real",
        agent_config_versions=None,
        started_at=None,
        completed_at=None,
        completed_entries=0,
        # Seeded, not None: the abandoned-run reclaim in
        # ``execute_evaluation`` stamps this when it believes a previous
        # attempt died. A lease is time-based, so an attempt that merely
        # stalled keeps working and completes afterwards — the completion path
        # MUST clear the message, or the transient wrong state that design
        # deliberately accepts becomes a permanent one: a run reported
        # ``completed`` carrying "Abandoned by a worker…". Asserted below.
        error="Abandoned by a worker that did not survive its attempt — re-run to retry",
        score=None,
        f1_score=None,
        duration_seconds=None,
    )

    agent = SimpleNamespace(
        id=agent_id,
        name="Eval Agent",
        persona="You are helpful",
        temperature=0.2,
        max_iterations=5,
        knowledge_base_ids=[],
        integration_bindings={"slack": [str(uuid.uuid4())]},
        integration_allowed_tools={},
        mcp_server_ids=[],
        custom_tool_ids=[],
        output_format=None,
        output_schema=None,
    )

    evaluation = SimpleNamespace(
        id=evaluation_id,
        execution_mode="full_loop",
        apply_prompt_guards=False,
        require_tool_approval=False,
        evaluator_model="judge-model",
        evaluator_prompt=None,
        evaluation_instructions=None,
        sub_score_weights=None,
        penalty_factor=0.5,
        run_mode="parallel",
        trigger_config={},
        timeout_seconds=3600,
        judge_max_tokens=8192,
    )

    entries = [
        (
            SimpleNamespace(
                id=uuid.uuid4(),
                input_prompt="Question 1",
                expected_answer="Answer 1",
                file_ids=None,
                evaluation_prompt=None,
                position=1,
            ),
            None,  # dataset_instructions
            None,  # dataset_output_schema
            None,  # dataset_label_vocabulary
        ),
        (
            SimpleNamespace(
                id=uuid.uuid4(),
                input_prompt="Question 2",
                expected_answer="Answer 2",
                file_ids=None,
                evaluation_prompt=None,
                position=2,
            ),
            None,
            None,
            None,
        ),
    ]

    db = _FakeSession(
        cancel_after_entries=cancel_after_entries,
        cancelled_on_refresh=cancelled_on_refresh,
        execute_results=[
            _ScalarResult(run),
            _ScalarResult(agent),
            # Per-agent output extractor, resolved once per run between
            # the agent and evaluation loads. None = score the raw output.
            _ScalarResult(None),
            _ScalarResult(evaluation),
            _EntriesResult(entries),
            _CountResult(2),
            _CountResult(2),
        ]
    )

    full_loop_calls = []
    start_episode_calls = []
    append_step_calls = []
    finalize_calls = []

    async def _mock_start_episode(_db, *, run_id, entry_id, correlation_id=None):
        episode = SimpleNamespace(
            run_id=run_id,
            entry_id=entry_id,
            correlation_id=correlation_id,
            status="running",
            steps=[],
        )
        start_episode_calls.append((run_id, entry_id, correlation_id))
        return episode

    async def _mock_append_step(_db, *, episode, step):
        episode.steps.append(step)
        append_step_calls.append((episode.entry_id, step))

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
        finalize_calls.append(
            (
                episode.entry_id,
                final_output,
                first_turn_output,
                participants,
                error,
                timed_out,
            )
        )

    async def _mock_execute_full_loop(
        _db,
        _agent,
        _model_id,
        _input_prompt,
        _file_refs,
        evaluation=None,
        **kwargs,
    ):
        full_loop_calls.append(evaluation)
        return "ok", [], None, ["tool_a"], []

    async def _mock_score_result(*_args, **_kwargs):
        return {
            "score": 100.0,
            "sub_scores": {k: 100 for k in ee.SUB_SCORE_WEIGHTS},
            "evaluator_reasoning": "ok",
            "judge_model": "judge-model",
            "judge_provider": "openai",
            "judge_stop_reason": "stop",
            "judge_truncated": False,
            "is_judge_failure": False,
            "judge_usage": {},
            "judge_system_prompt": "sys",
            "judge_user_prompt": "usr",
            "judge_raw_response": "{}",
        }

    if finalize_error is not None:
        # Raised from the FINALISING path: inside the outer ``try`` but past the
        # per-entry handler, which records an error result and carries on, so an
        # exception thrown during an entry never reaches the handler under test.
        def _boom(**_kwargs):
            raise finalize_error

        monkeypatch.setattr(ee, "log_extraction_failures", _boom)

    headlines = patch_evaluation_run(
        monkeypatch,
        full_loop=_mock_execute_full_loop,
        score=_mock_score_result,
        start_episode=_mock_start_episode,
        append_step=_mock_append_step,
        finalize_episode=_mock_finalize_episode,
    )

    # Imported inside ``_stop_if_the_user_cancelled``, so patched at its source
    # module rather than on ``ee``. Recorded as well as stubbed: re-closing the
    # episodes on the way out is what keeps a late callback from being judged
    # on a cancelled run, so a test needs to be able to see it happen.
    close_calls = []

    async def _mock_close_episodes(_db, *, run_id, **_kwargs):
        close_calls.append(run_id)

    monkeypatch.setattr(
        evaluation_continuation, "close_episodes_for_cancelled_run", _mock_close_episodes
    )

    asyncio.run(ee.execute_evaluation_run(db, str(run_id)))

    return SimpleNamespace(
        close_calls=close_calls,
        db=db,
        run=run,
        run_id=run_id,
        entries=entries,
        evaluation=evaluation,
        evaluation_id=evaluation_id,
        full_loop_calls=full_loop_calls,
        start_episode_calls=start_episode_calls,
        append_step_calls=append_step_calls,
        finalize_calls=finalize_calls,
        headline_calls=[(c["evaluation_id"], c["batch_run_ids"]) for c in headlines.calls],
    )


def test_execute_evaluation_run_records_episodes_and_completes(monkeypatch):
    o = _drive_full_run(monkeypatch)
    full_loop_calls = o.full_loop_calls
    entries = o.entries
    evaluation = o.evaluation
    start_episode_calls = o.start_episode_calls
    finalize_calls = o.finalize_calls
    append_step_calls = o.append_step_calls
    run = o.run
    headline_calls = o.headline_calls
    evaluation_id = o.evaluation_id
    run_id = o.run_id

    assert len(full_loop_calls) == len(entries)
    assert all(_eval is evaluation for _eval in full_loop_calls)
    assert len(start_episode_calls) == len(entries)
    assert len(finalize_calls) == len(entries)
    assert len(append_step_calls) >= len(entries)
    assert run.status == "completed"
    assert run.error is None, (
        "a completing run must clear a stale error — this is the single line "
        "the residual argument rests on (evaluation_engine: run.error = "
        "None in the completion block). Deleting it must fail a test."
    )
    # A completed run scores its own headlines immediately, through the same
    # function the end of the batch uses: it never sits `completed` for the
    # rest of a multi-run batch with a judge headline of 0.0 for a judge that
    # never ran and both composed headlines NULL.
    assert headline_calls == [(evaluation_id, [str(run_id)])]
    assert run.score is None


class _AnyResult:
    """What a query the test does not script answers: nothing found, nothing counted."""

    def scalar(self):
        return 0

    def scalar_one_or_none(self):
        return None

    def scalars(self):
        return SimpleNamespace(all=lambda: [])


class _LenientSession(_FakeSession):
    async def execute(self, _statement):
        if selects_run_status(_statement):
            # A live run reads ``running``; no row at all is a deleted one.
            return _StatusResult("running")
        return self._execute_results.pop(0) if self._execute_results else _AnyResult()

    async def flush(self):
        return None


@pytest.mark.parametrize("await_callback", [False, True])
def test_an_evaluation_run_keeps_its_secret_scope_through_judging_and_storage(monkeypatch, await_callback):
    """The agent's own scope closes when it answers; the run's stays open while the output is judged and stored."""
    from app.core.redaction import known_secrets_scope, redact_known_secrets, remember_secret_value

    secret = "ghp_EvalSecretValue123"
    variable_id = str(uuid.uuid4())
    run_id = uuid.uuid4()
    run = SimpleNamespace(
        id=run_id, status="pending", agent_id=uuid.uuid4(), flow_id=None, evaluation_id=uuid.uuid4(), model_id="gpt-test",
        is_default_model=False,
        persona_version_number=None, prompt_versions=None, skill_versions=None, agent_config_versions=None,
        placebo_comparison_id=None, arm="real",
        started_at=None, completed_at=None, completed_entries=0, error=None, score=None, f1_score=None,
        duration_seconds=None, total_entries=1,
    )
    agent = SimpleNamespace(
        id=run.agent_id, name="Eval Agent", persona="You are helpful", temperature=0.2, max_iterations=5,
        knowledge_base_ids=[], integration_bindings={}, integration_allowed_tools={}, mcp_server_ids=[],
        custom_tool_ids=[],
        output_format=None,
        output_schema=None,
    )
    evaluation = SimpleNamespace(
        id=run.evaluation_id, execution_mode="full_loop", evaluator_model="judge-model", evaluator_prompt=None,
        evaluation_instructions=None, sub_score_weights=None, penalty_factor=0.5, run_mode="parallel",
        trigger_config={"await_callback": True} if await_callback else {}, timeout_seconds=3600,
        apply_prompt_guards=False, require_tool_approval=False, judge_max_tokens=8192,
    )
    entry = SimpleNamespace(
        id=uuid.uuid4(), input_prompt="Question", expected_answer="Answer", file_ids=None,
        evaluation_prompt=None, position=1,
    )
    db = _LenientSession(
        execute_results=[
            _ScalarResult(run),
            _ScalarResult(agent),
            _ScalarResult(None),  # no per-agent output extractor
            _ScalarResult(evaluation),
            _EntriesResult([(entry, None, None, None)]),
        ]
    )
    seen: dict[str, str] = {}

    async def _full_loop(*_args, **_kwargs):
        # The agent loop's own scope (run_agent_chat): load_variables resolves the secret there.
        with known_secrets_scope():
            remember_secret_value(variable_id, secret)
            return f"The token is {secret}", [], None, [], []

    async def _score(*_args, **_kwargs):
        seen["judged"] = redact_known_secrets(f"judging {secret}")
        return {
            "score": 100.0, "sub_scores": {k: 100 for k in ee.SUB_SCORE_WEIGHTS}, "evaluator_reasoning": "ok",
            "is_judge_failure": False,
            "judge_model": "judge-model", "judge_provider": "openai", "judge_stop_reason": "stop",
            "judge_truncated": False, "judge_failed_due_to_truncation": False, "judge_usage": {},
            "judge_system_prompt": "sys", "judge_user_prompt": "usr", "judge_raw_response": "{}",
        }

    async def _start(_db, *, run_id, entry_id, correlation_id=None):
        return SimpleNamespace(run_id=run_id, entry_id=entry_id, status="running", steps=[])

    async def _append(_db, *, episode, step):
        episode.steps.append(step)

    async def _finalize(_db, *, episode, final_output, first_turn_output=None, **_kwargs):
        seen["stored"] = redact_known_secrets(final_output or "")

    async def _pause(_db, *, episode, timeout_seconds):
        seen["paused"] = redact_known_secrets(episode.first_turn_output)

    async def _served(_db, _model_id, _owner_id):
        return True

    monkeypatch.setattr(ee, "_execute_agent_full_loop", _full_loop)
    monkeypatch.setattr(ee, "score_result", _score)
    # A provider answers the run's model: the fake session lists none.
    monkeypatch.setattr(ee, "run_model_served", _served)
    for name, fake in (("start_episode", _start), ("append_step", _append), ("finalize_episode", _finalize),
                       ("pause_for_callback", _pause)):
        monkeypatch.setattr(ee.ConversationContinuationService, name, fake)

    asyncio.run(ee.execute_evaluation_run(db, str(run_id)))

    written = [seen["paused"]] if await_callback else [seen["judged"], seen["stored"]]
    for text in written:
        assert secret not in text and "[[SECRET_VARIABLE:" in text


def test_a_cancelled_run_stops_at_the_next_entry_instead_of_finishing(monkeypatch):
    """A run cancelled mid-flight stops itself, without touching its siblings.

    The execution-level ``cancel_event`` cannot serve a per-run cancel: a
    trigger stamps ONE execution across the whole agent x model matrix, so
    firing it for one cancelled run would stop every sibling too.
    ``cancel_run`` therefore cancels the execution only when this run is the
    only one it owns, and a run sharing its execution stops here instead, by
    noticing its own status at the next entry boundary.

    MUTATION CHECKS, both run:
      * delete the probe/return -> both entries execute, first assert fails.
      * ``break`` instead of ``return`` -> the block after the loop writes
        ``status = "completed"``, and the run reports success for work that
        was cancelled. Second assert fails.
    """
    o = _drive_full_run(monkeypatch, cancel_after_entries=1)

    assert len(o.full_loop_calls) == 1, (
        f"{len(o.full_loop_calls)} of {len(o.entries)} entries executed — a "
        "cancelled run must stop at the next entry boundary rather than work "
        "through the rest of the dataset, which is the whole point."
    )
    assert o.run.status != "completed", (
        f"a cancelled run was finalised as {o.run.status!r}. Returning, not "
        "breaking, is what keeps the completion block below the loop from "
        "reporting success for work the user stopped."
    )
    assert o.headline_calls == [], (
        "a cancelled run must not score its headlines: they would be computed "
        "over a partial dataset and then compared against fully-executed "
        "siblings."
    )
    assert o.db.cancellation_probes == 2, (
        f"the probe fired {o.db.cancellation_probes} time(s); it must be asked "
        "once per entry, or cancellation latency grows with the dataset."
    )
    assert o.close_calls == [o.run_id], (
        "a run stopping must re-close its episodes. ``cancel_run`` already "
        "closed them, but the entry in flight ran to its own end here and "
        "``finalize_episode`` has no terminal guard, so a callback-backed entry "
        "can reopen its episode to waiting_callback after the endpoint closed "
        "it — and a late callback is then accepted and judged on a cancelled "
        "run, which is the billing bug this issue closed."
    )


def test_a_run_cancelled_during_its_last_entry_is_not_finalised_as_completed(monkeypatch):
    """The in-loop probe cannot see this, and the block below the loop undoes it.

    The per-entry check runs at the TOP of each iteration, so a cancellation
    landing during the LAST entry is never probed by it — nor is a one-entry
    dataset at all. The loop then exits normally and the code below it would
    write ``status = "completed"`` over the ``"cancelled"`` the endpoint
    committed: the user's stop silently undone, the run reporting success.
    The ``await_callback`` branch would do the same with ``"running"``.

    MUTATION CHECK: removing the post-loop probe fails this — the run comes
    back ``completed``.
    """
    o = _drive_full_run(monkeypatch, cancel_after_entries=2)

    assert len(o.full_loop_calls) == len(o.entries), (
        "the cancellation lands during the last entry, so every entry should "
        "still have executed — this test is about what happens AFTER the loop"
    )
    assert o.run.status != "completed", (
        f"a run cancelled during its final entry was finalised as "
        f"{o.run.status!r}, overwriting the endpoint's 'cancelled'. The user "
        "pressed stop and the run reported success."
    )
    assert o.db.cancellation_probes == len(o.entries) + 1, (
        f"{o.db.cancellation_probes} probes for {len(o.entries)} entries: the "
        "check must also run once after the loop, which is the whole point."
    )
    assert o.close_calls == [o.run_id], (
        "the post-loop stop must re-close episodes too, for the same reason "
        "the in-loop one does"
    )


def test_a_cancelled_run_that_then_raises_is_not_relabelled_as_failed(monkeypatch):
    """The failure handler must not overwrite ``cancelled`` either.

    ``except Exception`` refreshes the run -- reading ``"cancelled"`` straight
    back from the database -- and must not then write ``"failed"`` with
    whatever incidental error ended the attempt: the user pressed cancel and
    would be told the run failed, for a reason they did not cause.

    A cancellation reaches this handler through the cooperative stop: a run
    that keeps working after being cancelled can raise here like any other.
    The execution-level path cancels the asyncio task instead, and
    ``CancelledError`` is a ``BaseException`` that ``except Exception`` never
    sees.

    MUTATION CHECK: removing the ``status == "cancelled"`` guard fails this --
    the run comes back ``failed`` carrying the provider's error.
    """
    # The cancel lands AFTER the post-loop probe -- the residual window that
    # probe documents -- and the run then raises while finalising.
    o = _drive_full_run(
        monkeypatch,
        finalize_error=RuntimeError("provider exploded"),
        cancelled_on_refresh=True,
    )

    assert o.run.status == "cancelled", (
        f"a cancelled run that raised was relabelled {o.run.status!r}. The stop "
        "still worked, but the run reports a failure the user did not cause and "
        "no trace of the cancellation they did."
    )
    assert o.run.error == "Cancelled by user", (
        f"the run reports {o.run.error!r}. The cancellation's own reason must "
        "survive: asserting merely that the provider's error is absent would "
        "also pass if the error had been cleared to None."
    )


def test_every_episode_write_in_the_loop_is_under_its_run_lock():
    """RUN before EPISODE in every transaction segment of the loop.

    Asserted on the SOURCE, like ``test_every_episode_write_path_locks_its_run_first``:
    the behaviour is a lock-acquisition ORDER inside a transaction, and the
    deadlock it prevents needs two connections racing in a window a test cannot
    reliably hit.

    Without the locks the loop takes EPISODE then RUN -- not by locking in the
    wrong order but by not locking at all: the episode writes flush and take
    the episode row lock, and the run is written only at a later commit in the
    same transaction. ``close_episodes_for_cancelled_run`` takes RUN then
    EPISODE, so a cancel landing in that window deadlocks against the worker.

    WALKED, not sandwiched: every segment is checked, not only the judge one.
    The ``await_callback`` branch parks the entry and ``continue``s, closing its
    own transaction with an episode write followed by a run update, which a
    single lock between ``score_result`` and ``finalize_episode`` would miss.

    ``start_episode`` is exempt on purpose: it INSERTs a row no other
    transaction can see, so nothing can be blocked waiting on it.

    KNOWN LIMIT: a lock taken inside a NARROWER conditional than the writes it
    covers still reads as held here, because this walks text, not branches.
    All three locks sit in the same block as the writes they cover; a fourth
    that does not would need a real control-flow check.

    MUTATION CHECKS: delete any of the three lock calls and this fails naming
    the write left uncovered; move one above its segment's model call and the
    second assertion fails.
    """
    import inspect

    # Comment lines are dropped first. Every marker below is a fragment of real
    # code, and this function's comments discuss those same calls constantly --
    # one of them written without its double backticks would silently become a
    # transaction boundary or a covered write and weaken this walk with nothing
    # failing.
    source = "\n".join(
        line
        for line in inspect.getsource(ee.execute_evaluation_run).split("\n")
        if not line.strip().startswith("#")
    )

    kinds = {
        "LOCK": ["_lock_run_before_episode_writes(db, run_uuid"],
        "WRITE": [
            "ConversationContinuationService.append_step(",
            "ConversationContinuationService.pause_for_callback(",
            "ConversationContinuationService.finalize_episode(",
        ],
        # A commit, a rollback or a connection release ends the transaction and
        # drops every row lock with it, so the next segment must take its own.
        # The rollback matters for more than symmetry: the per-entry failure
        # handler opens with one, and without it this walk would carry the judge
        # path's lock forward over the handler's own writes -- source order is
        # not execution order across an ``except``, and a lock taken in the try
        # body is not guaranteed to have run. Without it, deleting the
        # handler's lock goes undetected.
        "END": [
            "await db.commit()",
            "await db.rollback()",
            "release_db_connection_before_loop(db)",
        ],
        "MODEL": ["_execute_agent_full_loop(", "await score_result("],
    }
    events = []
    for kind, needles in kinds.items():
        for needle in needles:
            at = source.find(needle)
            while at != -1:
                events.append((at, kind, needle))
                at = source.find(needle, at + 1)
    events.sort()

    locked = False
    uncovered = []
    held_across_model = []
    for _at, kind, needle in events:
        if kind == "END":
            locked = False
        elif kind == "LOCK":
            locked = True
        elif kind == "WRITE" and not locked:
            uncovered.append(needle)
        elif kind == "MODEL" and locked:
            held_across_model.append(needle)

    assert not uncovered, (
        f"episode write(s) reached with no run lock held in their transaction "
        f"segment: {uncovered}. That is EPISODE-then-RUN against a cancel that "
        "goes RUN-then-EPISODE, which is a deadlock."
    )
    assert not held_across_model, (
        f"the run lock is held across {held_across_model} -- an LLM request. A "
        "cancel would block for the length of that call, which is the defect "
        "this PR exists to fix."
    )


def _integration_tool_def(name: str, provider: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, source=f"integration:{provider}")


def _make_eval_agent(**overrides) -> SimpleNamespace:
    defaults = {
        "id": uuid.uuid4(),
        "name": "Eval Agent",
        "user_id": uuid.uuid4(),
        "integration_bindings": {"opencti": [str(uuid.uuid4())]},
        "integration_template_bindings": [],
        "integration_allowed_tools": {},
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _mock_integration_tool_build(monkeypatch, tool_defs, *, resolved=None):
    """Mock the DB-bound resolver and builder; the filter helpers run for real.

    The resolver gives the agent's direct bindings, or *resolved*; what the
    builder hands it lands in ``captured`` with the build's context.
    """
    captured: dict[str, object] = {}

    async def _resolve(
        _db, agent, acting_user_id, *, owner_run=False, company_automation=False, scope_headers=None
    ):
        captured["acting_user_id"] = acting_user_id
        captured["owner_run"] = owner_run
        captured["company_automation"] = company_automation
        bindings = (
            dict(resolved)
            if resolved is not None
            else integration_tools.flatten_bindings(agent.integration_bindings or {})
        )
        return integration_tools.AgentIntegrationBindings(
            bindings, dict.fromkeys(bindings, integration_tools.DIRECT_BINDING_ORIGIN), []
        )

    async def _mock_build_integration_tools(
        _db, bindings, *, context=None, channel_bot_mode=False
    ):
        del channel_bot_mode
        captured["bindings"] = dict(bindings)
        captured.update(context or {})
        return list(tool_defs), "executor"

    monkeypatch.setattr(integration_tools, "resolve_agent_integration_bindings", _resolve)
    monkeypatch.setattr(
        "app.services.integration_tools.build_integration_tools",
        _mock_build_integration_tools,
    )
    return captured


def test_build_evaluation_integration_tools_passes_evaluation_user_id(monkeypatch):
    agent = _make_eval_agent()
    evaluation_user_id = uuid.uuid4()
    evaluation = SimpleNamespace(id=uuid.uuid4(), user_id=evaluation_user_id)

    tool_defs = [_integration_tool_def("opencti_search", "opencti")]
    captured = _mock_integration_tool_build(monkeypatch, tool_defs)

    bundle = asyncio.run(
        ee._build_evaluation_integration_tools(
            _FakeSession([]),
            agent,
            evaluation=evaluation,
        )
    )

    assert bundle == (tool_defs, "executor")
    assert captured["source"] == "evaluation"
    assert captured["agent"] == "Eval Agent"
    # The bindings are resolved for the evaluation owner, not the agent owner.
    assert captured["acting_user_id"] == str(evaluation_user_id)
    assert captured["user_id"] == str(evaluation_user_id)
    # Real flatten_bindings output: single-element list -> plain provider key.
    assert captured["bindings"] == {
        "opencti": agent.integration_bindings["opencti"][0]
    }
    # What the bundle was built from and for whom, checked by the turn.
    assert bundle.bindings == captured["bindings"]
    assert bundle.acting_user_id == str(evaluation_user_id)


def test_an_evaluated_run_gets_what_the_build_collects_for_the_built_in_tools(monkeypatch):
    """The build fills the lists a chat run's grounded extraction reads (how the bound
    platform names objects, the bound ATT&CK source); the evaluation's build hands the same
    lists over with its tools, so the pipeline it evaluates is the one that runs."""
    from app.services.integration_tools import ATTACK_SOURCES_CONTEXT_KEY, PLATFORM_NAMES_CONTEXT_KEY

    agent = _make_eval_agent()
    evaluation = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4())
    captured = _mock_integration_tool_build(monkeypatch, [_integration_tool_def("opencti_search", "opencti")])

    bundle = asyncio.run(ee._build_evaluation_integration_tools(_FakeSession([]), agent, evaluation=evaluation))

    for key in (PLATFORM_NAMES_CONTEXT_KEY, ATTACK_SOURCES_CONTEXT_KEY):
        assert isinstance(captured[key], list)
        assert bundle.context[key] is captured[key]
    _definitions, executor = bundle  # still the pair callers unpack
    assert executor == "executor"


def test_build_evaluation_integration_tools_falls_back_to_agent_owner_user_id(monkeypatch):
    agent_owner_id = uuid.uuid4()
    agent = _make_eval_agent(user_id=agent_owner_id)
    evaluation = SimpleNamespace(id=uuid.uuid4(), user_id=None)

    tool_defs = [_integration_tool_def("opencti_search", "opencti")]
    captured = _mock_integration_tool_build(monkeypatch, tool_defs)

    bundle = asyncio.run(
        ee._build_evaluation_integration_tools(
            _FakeSession([]),
            agent,
            evaluation=evaluation,
        )
    )

    assert bundle == (tool_defs, "executor")
    assert captured["acting_user_id"] == str(agent_owner_id)
    assert captured["user_id"] == str(agent_owner_id)


def test_build_evaluation_integration_tools_handles_ownerless_agent_and_evaluation(
    monkeypatch,
):
    """Company-managed agent (user_id None) + ownerless evaluation must not crash.

    The acting user resolves to None and the tools are still built from the
    fixed bindings (identity-sensitive providers degrade inside the builder,
    same as the pre-existing assignment_engine behavior).
    """
    agent = _make_eval_agent(user_id=None)
    evaluation = SimpleNamespace(id=uuid.uuid4(), user_id=None)

    tool_defs = [_integration_tool_def("opencti_search", "opencti")]
    captured = _mock_integration_tool_build(monkeypatch, tool_defs)

    bundle = asyncio.run(
        ee._build_evaluation_integration_tools(
            _FakeSession([]),
            agent,
            evaluation=evaluation,
        )
    )

    assert bundle == (tool_defs, "executor")
    assert captured["acting_user_id"] is None
    assert captured["owner_run"] is True
    assert captured["user_id"] is None


def test_build_evaluation_integration_tools_degrades_on_malformed_acting_user_id(
    monkeypatch,
):
    """A malformed acting user id must not crash tool setup.

    It degrades to no acting user context (``user_id=None``, template
    resolution skipped) - same contract as ``shadow_invisible_bindings``. It is
    not the agent's own run, so the resolver keeps none of its bindings.
    """
    agent = _make_eval_agent(
        user_id=None,
        integration_template_bindings=["opencti"],
    )
    evaluation = SimpleNamespace(id=uuid.uuid4(), user_id="not-a-uuid")

    tool_defs = [_integration_tool_def("opencti_search", "opencti")]
    captured = _mock_integration_tool_build(monkeypatch, tool_defs)

    bundle = asyncio.run(
        ee._build_evaluation_integration_tools(
            _FakeSession([]),
            agent,
            evaluation=evaluation,
        )
    )

    assert bundle == (tool_defs, "executor")
    assert captured["acting_user_id"] is None
    assert captured["owner_run"] is False
    assert captured["user_id"] is None


@pytest.mark.parametrize("company_managed", [False, True])
def test_build_evaluation_integration_tools_builds_what_the_resolver_gives(
    monkeypatch, company_managed
):
    """The tools come from the shared resolution only: a binding it
    withheld is never built, a template it resolved is. An evaluation is a
    company automation exactly when its agent is company-managed."""
    template_id = str(uuid.uuid4())
    agent = _make_eval_agent(is_company_managed=company_managed)
    tool_defs = [_integration_tool_def("jira_search", "jira")]
    captured = _mock_integration_tool_build(monkeypatch, tool_defs, resolved={"jira": template_id})

    bundle = asyncio.run(
        ee._build_evaluation_integration_tools(
            _FakeSession([]),
            agent,
            evaluation=SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4()),
        )
    )

    assert bundle == (tool_defs, "executor")
    assert captured["bindings"] == {"jira": template_id}
    assert captured["company_automation"] is company_managed


def test_build_evaluation_integration_tools_builds_nothing_when_everything_is_withheld(monkeypatch):
    captured = _mock_integration_tool_build(monkeypatch, [], resolved={})

    bundle = asyncio.run(
        ee._build_evaluation_integration_tools(
            _FakeSession([]),
            _make_eval_agent(),
            evaluation=SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4()),
        )
    )

    assert bundle is None
    assert "bindings" not in captured


def test_build_evaluation_integration_tools_applies_allowed_tools_filter(monkeypatch):
    """The real filter_integration_tools runs against the built definitions."""
    agent = _make_eval_agent(
        integration_allowed_tools={"opencti": ["opencti_search"]},
    )
    evaluation = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4())

    kept = _integration_tool_def("opencti_search", "opencti")
    dropped = _integration_tool_def("opencti_delete", "opencti")
    _mock_integration_tool_build(monkeypatch, [kept, dropped])

    bundle = asyncio.run(
        ee._build_evaluation_integration_tools(
            _FakeSession([]),
            agent,
            evaluation=evaluation,
        )
    )

    assert bundle is not None
    filtered_defs, executor = bundle
    assert executor == "executor"
    assert filtered_defs == [kept]


def test_integration_providers_from_tool_defs_extracts_integration_sources():
    defs = [
        SimpleNamespace(source="integration:opencti"),
        SimpleNamespace(source="integration:github"),
        SimpleNamespace(source="builtin:web_fetch"),
        SimpleNamespace(source=None),
    ]

    providers = ee._integration_providers_from_tool_defs(defs)

    assert providers == {"opencti", "github"}


def test_integration_providers_from_tool_defs_ignores_empty_provider_segment():
    defs = [
        SimpleNamespace(source="integration:"),
        SimpleNamespace(source="integration:  "),
    ]

    providers = ee._integration_providers_from_tool_defs(defs)

    assert providers == set()


# ── Reused prediction duration recovery ──────────────────────────
# A reused entry whose carried step timeline lacks per-step durations
# must inherit the source run's real recorded duration, never collapse to 0
# (which would score a bogus perfect speed and poison the batch's T_min).


@pytest.mark.parametrize(
    ("carried", "details", "source_seconds", "expected"),
    [
        # The carried per-step sum is 0 but the source recorded a real agent duration.
        pytest.param(0, {"agent_duration_ms": 4500}, None, 4500, id="from_execution_details"),
        # No usable agent_duration_ms: derive it from the source's duration_seconds snapshot.
        pytest.param(0, {}, 3.0, 3000, id="from_source_snapshot"),
        pytest.param(0, {"agent_duration_ms": 0}, 3.2, 3200, id="from_source_snapshot_over_a_zero"),
        pytest.param(0, None, 1.0, 1000, id="from_source_snapshot_without_details"),
        # Unrecoverable: 0, so the aggregate excludes the entry as unknown
        # rather than scoring it as instant.
        pytest.param(0, {}, None, 0, id="unrecoverable"),
        pytest.param(0, {"agent_duration_ms": 0}, 0, 0, id="unrecoverable_zeros"),
        pytest.param(0, None, None, 0, id="unrecoverable_nothing"),
        # A healthy timeline's carried sum equals the recorded total: unchanged.
        pytest.param(2500, {"agent_duration_ms": 2500}, 2.5, 2500, id="healthy_timeline"),
        pytest.param(1, {}, None, 1, id="carried_sum_with_nothing_recorded"),
        # Some steps lack durations, so the carried sum undercounts the recorded
        # total: lifted to it, never left looking faster.
        pytest.param(1500, {"agent_duration_ms": 4500}, None, 4500, id="undercount_lifted_to_recorded"),
        pytest.param(1500, {}, 4.5, 4500, id="undercount_lifted_to_snapshot"),
        # A carried sum at or above the recorded total is kept, so healthy
        # reuses stay byte-for-byte identical.
        pytest.param(4500, {"agent_duration_ms": 4500}, None, 4500, id="carried_equals_recorded"),
        pytest.param(5000, {"agent_duration_ms": 4500}, 3.0, 5000, id="carried_above_recorded"),
        pytest.param(1500, {}, None, 1500, id="carried_kept_without_a_total"),
    ],
)
def test_recover_reused_agent_duration(carried, details, source_seconds, expected):
    assert ee._recover_reused_agent_duration_ms(carried, details, source_seconds) == expected


# --- PROSE deterministic scoring alongside the judge ---------------------------


def _run_prose_evaluation(
    monkeypatch,
    expected_answers,
    *,
    break_run_config=False,
    execute_full_loop=None,
    reuse_source=None,
    price_rows=None,
    score_result=None,
    execution_mode="full_loop",
    skip_llm=False,
    apply_prompt_guards=False,
    require_tool_approval=False,
    entry_inputs=None,
    pinned=False,
):
    """Drive a real ``execute_evaluation_run`` for a PROSE evaluation.

    Reuses the fake-session harness the episode test established: only the DB, the agent
    loop and the judge are faked, so the quality seam, the per-entry catch and the result
    assembly all run for real. That is the point -- these tests are about which code path
    the engine takes when scoring degrades, and mocking the seam would test the mock.

    ``entry_inputs`` gives each entry its ``(input_prompt, file_ids)``; ``pinned``
    reads the entries from a pinned dataset version instead of the live dataset.

    Returns the persisted ``EvaluationResult`` objects, in entry order.
    """
    run_id, agent_id, evaluation_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    run = SimpleNamespace(
        id=run_id,
        status="pending",
        agent_id=agent_id,
        flow_id=None,
        evaluation_id=evaluation_id,
        model_id="gpt-test",
        is_default_model=False,
        persona_version_number=None,
        prompt_versions=None,
        skill_versions=None,
        placebo_comparison_id=None,
        arm="real",
        agent_config_versions=None,
        started_at=None,
        completed_at=None,
        completed_entries=0,
        error=None,
        score=None,
        f1_score=None,
        duration_seconds=None,
        metric_suite_version=None,
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Eval Agent",
        persona="You are helpful",
        temperature=0.2,
        max_iterations=5,
        knowledge_base_ids=[],
        integration_bindings={},
        integration_allowed_tools={},
        mcp_server_ids=[],
        custom_tool_ids=[],
        output_format=None,
        output_schema=None,
        skip_llm=skip_llm,
    )
    evaluation = SimpleNamespace(
        id=evaluation_id,
        family="PROSE",
        default_output_schema=None,
        # Unset scorer_selection keeps the judge enabled, which is what makes the
        # judge-plus-deterministic assertion meaningful.
        scorer_selection=None,
        execution_mode=execution_mode,
        apply_prompt_guards=apply_prompt_guards,
        require_tool_approval=require_tool_approval,
        evaluator_model="judge-model",
        evaluator_prompt=None,
        evaluation_instructions=None,
        sub_score_weights=None,
        penalty_factor=0.5,
        run_mode="sequential",
        trigger_config={},
        timeout_seconds=3600,
        judge_max_tokens=8192,
    )
    inputs = entry_inputs or [(f"Question {i}", None) for i in range(1, len(expected_answers) + 1)]
    entries = [
        (
            SimpleNamespace(
                id=uuid.uuid4(),
                input_prompt=input_prompt,
                expected_answer=answer,
                file_ids=file_ids,
                evaluation_prompt=None,
                position=i,
            ),
            None,  # dataset_instructions
            None,  # dataset_output_schema
            None,  # dataset_label_vocabulary
        )
        for i, (answer, (input_prompt, file_ids)) in enumerate(zip(expected_answers, inputs, strict=True), start=1)
    ]
    if pinned:
        from app.services.evaluation import dataset_versioning

        run.pinned_dataset_versions = {str(uuid.uuid4()): 1}
        version_rows = [
            (SimpleNamespace(**vars(entry), entry_id=entry.id, output_schema=None), *rest)
            for entry, *rest in entries
        ]

        async def _pinned_entries(_db, _evaluation_id, _pinned):
            return version_rows

        monkeypatch.setattr(dataset_versioning, "load_pinned_entries", _pinned_entries)
    db = _FakeSession(
        execute_results=[
            _ScalarResult(run),
            _ScalarResult(agent),
            # Per-agent output extractor, resolved once per run between
            # the agent and evaluation loads. None = score the raw output.
            _ScalarResult(None),
            _ScalarResult(evaluation),
            # A pinned run reads its entries through ``load_pinned_entries``.
            *([] if pinned else [_EntriesResult(entries)]),
            _CountResult(len(entries)),
            _CountResult(len(entries)),
        ]
    )

    async def _mock_execute_full_loop(*_args, **_kwargs):
        # The agent answers with the reference in different casing: under the chosen
        # policy that is an exact match, so a scored 100 proves the policy reached the run.
        return "THE CAT SAT", [], None, [], []

    async def _mock_score_result(*_args, **_kwargs):
        return {
            "score": 88.0,
            "sub_scores": {k: 88 for k in ee.SUB_SCORE_WEIGHTS},
            "evaluator_reasoning": "ok",
            "judge_model": "judge-model",
            "judge_provider": "openai",
            "judge_stop_reason": "stop",
            "judge_truncated": False,
            "is_judge_failure": False,
            "judge_usage": {},
            "judge_system_prompt": "sys",
            "judge_user_prompt": "usr",
            "judge_raw_response": "{}",
        }

    patch_evaluation_run(
        monkeypatch,
        full_loop=execute_full_loop or _mock_execute_full_loop,
        score=score_result or _mock_score_result,
        price_rows=price_rows,
    )

    if reuse_source is not None:
        # Every entry reuses ``reuse_source``'s prediction instead of calling the agent.
        from app.services.evaluation import params as run_params

        async def _mock_run_params(_db, _run_id):
            return []

        async def _mock_kb_state(_db, _agent):
            return []

        async def _mock_find_reusable(*_args, **_kwargs):
            return reuse_source

        evaluation.reuse_default = "on"
        monkeypatch.setattr(run_params, "get_run_params", _mock_run_params)
        monkeypatch.setattr(ee, "load_agent_kb_state", _mock_kb_state)
        monkeypatch.setattr(ee, "_find_reusable_prediction", _mock_find_reusable)

    if break_run_config:
        def _raise(*_args, **_kwargs):
            raise ee.QualityConfigError("prose config unavailable for test")

        monkeypatch.setattr(ee, "build_quality_config", _raise)

    asyncio.run(ee.execute_evaluation_run(db, str(run_id)))

    results = [obj for obj in db.added if hasattr(obj, "execution_details")]
    return run, results


def _metrics_of(result) -> dict:
    return (result.execution_details or {}).get("metric_results") or {}


@pytest.mark.parametrize(
    ("stored", "skip_llm", "recorded"),
    [
        ("full_loop", False, "full_loop"),
        ("llm_only", False, "llm_only"),
        ("full_loop", True, "skip_llm"),
        # Every value but ``full_loop`` runs as LLM Only, so that is what is recorded.
        ("full-loop", False, "llm_only"),
    ],
)
def test_a_run_records_the_mode_it_executes_in_and_its_entries_record_the_same(
    monkeypatch, stored, skip_llm, recorded
):
    """The run's label and each entry's record read one resolved mode."""

    async def _llm_only_answers(*_args, **_kwargs):
        return "THE CAT SAT", [], None, []

    monkeypatch.setattr(ee, "_execute_agent_llm_only", _llm_only_answers)
    run, results = _run_prose_evaluation(
        monkeypatch, ["the cat sat"], execution_mode=stored, skip_llm=skip_llm
    )

    assert run.execution_mode == recorded
    assert [r.execution_details["execution_mode"] for r in results] == [recorded]


@pytest.mark.parametrize(
    "setting_name",
    ["apply_prompt_guards", "require_tool_approval"],
    ids=["prompt_guard", "tool_approval"],
)
@pytest.mark.parametrize(
    ("stored", "skip_llm", "setting", "recorded"),
    [
        ("full_loop", False, True, True),
        ("full_loop", False, False, False),
        # Neither setting applies outside Full Loop: LLM Only runs no guard
        # check and calls no tool, and a pipeline turn returns before both.
        ("llm_only", False, True, False),
        ("full_loop", True, True, False),
    ],
)
def test_a_run_records_the_setting_it_ran_with_and_passes_it_to_the_turn(
    monkeypatch, setting_name, stored, skip_llm, setting, recorded
):
    """The run keeps the setting it ran with, whatever later happens to its evaluation."""
    full_loop_kwargs: list[dict] = []

    async def _full_loop(*_args, **kwargs):
        full_loop_kwargs.append(kwargs)
        return "THE CAT SAT", [], None, [], []

    async def _llm_only_answers(*_args, **_kwargs):
        return "THE CAT SAT", [], None, []

    fingerprinted: list[bool] = []
    real_fingerprint = ee.compute_input_fingerprint

    def _fingerprint(**kwargs):
        fingerprinted.append(kwargs.get(setting_name, False))
        return real_fingerprint(**kwargs)

    monkeypatch.setattr(ee, "_execute_agent_llm_only", _llm_only_answers)
    monkeypatch.setattr(ee, "compute_input_fingerprint", _fingerprint)
    run, _results = _run_prose_evaluation(
        monkeypatch,
        ["the cat sat"],
        execution_mode=stored,
        skip_llm=skip_llm,
        execute_full_loop=_full_loop,
        **{setting_name: setting},
    )

    assert getattr(run, setting_name) is recorded
    # Reuse looks for answers produced under the same setting.
    assert fingerprinted == [recorded]
    # Every turn the run starts is told what the run records.
    assert [k.get(setting_name) for k in full_loop_kwargs] == [recorded] * len(full_loop_kwargs)


@pytest.mark.parametrize("pinned", [False, True], ids=["live_entries", "pinned_version"])
def test_the_agent_gets_the_author_prompt_and_the_fallback_only_without_one(monkeypatch, pinned):
    """An author prompt always wins; an entry without one gets the non-interactive fallback for its inputs."""
    from app.services.evaluation.framing import FILE_ONLY_FALLBACK_PROMPT, NO_INPUT_FALLBACK_PROMPT

    prompts: list[str] = []

    async def _full_loop(_db, _agent, _model_id, input_prompt, *_args, **_kwargs):
        prompts.append(input_prompt)
        return "THE CAT SAT", [], None, [], []

    async def _no_file_refs(_db, _file_ids):
        return []

    monkeypatch.setattr(ee, "_load_file_refs", _no_file_refs)
    _run_prose_evaluation(
        monkeypatch,
        ["the cat sat"] * 4,
        execute_full_loop=_full_loop,
        pinned=pinned,
        entry_inputs=[
            ("Summarise the report", ["file-1"]),
            ("Summarise the report", None),
            ("", ["file-1"]),
            (None, None),
        ],
    )

    assert prompts == [
        "Summarise the report",
        "Summarise the report",
        FILE_ONLY_FALLBACK_PROMPT,
        NO_INPUT_FALLBACK_PROMPT,
    ]


@pytest.mark.parametrize(
    ("proposals", "marked"),
    [
        ([{"tool_name": "zeta"}, {"tool_name": "alpha"}, {"tool_name": "zeta"}], ["alpha", "zeta"]),
        ([], None),
    ],
    ids=["held", "not-held"],
)
def test_an_entry_whose_calls_were_held_names_the_tools(monkeypatch, proposals, marked):
    """Read from the proposals the turn reported, sorted and deduplicated; none, no key."""

    async def _held(*_args, **kwargs):
        kwargs["approvals_out"].extend(proposals)
        return "I need approval before running: zeta.", [], None, [], []

    _run, results = _run_prose_evaluation(
        monkeypatch, ["the cat sat"], require_tool_approval=True, execute_full_loop=_held
    )

    [result] = results
    assert result.execution_details.get("approval_required_tools") == marked


def _left_out(metrics: dict) -> bool:
    """Every metric stored as left out, with the reason."""
    return bool(metrics) and all(
        blob["status"] == "unavailable"
        and blob["detail"]["reason"] == "configuration_error"
        and blob["detail"]["message"]
        for blob in metrics.values()
    )


def _prose_metrics_of(result) -> dict:
    """Only the prose half.

    ``metric_results`` also carries ``performance.*``, which is computed from telemetry and
    is independent of the quality config -- so "quality degraded" must be asserted as
    "no prose keys", not "empty blob". Asserting the blob is empty would also fail if
    performance scoring merely kept working, which is the desired behaviour.
    """
    return {k: v for k, v in _metrics_of(result).items() if k.startswith("prose.")}


def test_latency_is_timed_around_the_agent_call_not_rebuilt_from_its_steps(monkeypatch):
    """Elapsed time to the answer, measured, not the sum of recorded steps.

    A transfer returns from inside its handover block before its timeline is
    published, so it reaches the engine with no steps at all. Summed from those steps,
    latency would read 0 ms: a best-possible score from nothing observed, which
    normalisation would make the cohort's best, dragging every other run down.

    The stub below is that shape -- an answer with an empty timeline -- and takes a
    known time. The step total is deliberately untouched: ``duration_seconds`` and
    the run totals keep reading it, so only the latency metric moves to the clock.
    """

    async def _slow_answer_with_no_timeline(*_args, **_kwargs):
        # 60 ms, asserted at 50: slack for a sleep that wakes early.
        await asyncio.sleep(0.06)
        return "THE CAT SAT", [], None, [], []

    _run, results = _run_prose_evaluation(
        monkeypatch, ["the cat sat"], execute_full_loop=_slow_answer_with_no_timeline
    )

    details = results[0].execution_details
    wall_clock = _metrics_of(results[0])["performance.latency.wall_clock"]
    assert wall_clock["status"] == "scored"
    assert wall_clock["value"] >= 50
    assert details["agent_wall_clock_ms"] >= 50
    # The step total is untouched, so nothing built on it moves.
    assert details["agent_duration_ms"] == 0


def test_the_step_total_counts_tools_that_ran_together_once_at_the_longest():
    """Parallel tools overlapped: the agent spent the longest of them, not their sum."""
    traces = [
        {"type": "llm", "duration_ms": 100},
        {"type": "tool", "duration_ms": 30, "parallel_group": "g1"},
        {"type": "tool", "duration_ms": 80, "parallel_group": "g1"},
        {"type": "tool", "duration_ms": 20},
        {"type": "tool", "duration_ms": 5, "parallel_group": "g2"},
        {"type": "tool", "duration_ms": None},
    ]

    assert ee._step_total_ms(traces) == 100 + 80 + 20 + 5
    assert ee._step_total_ms([]) == 0


def test_a_run_reads_its_parallel_tools_once_in_its_step_total(monkeypatch):
    """What duration_seconds and Run Duration are built from."""

    async def _two_tools_together(*_args, **_kwargs):
        timeline = [
            {"type": "llm", "duration_ms": 100, "outcome": "success"},
            {"type": "tool", "tool_name": "a", "duration_ms": 30, "parallel_group": "g1", "outcome": "success"},
            {"type": "tool", "tool_name": "b", "duration_ms": 80, "parallel_group": "g1", "outcome": "success"},
            {"type": "llm", "duration_ms": 50, "outcome": "success"},
        ]
        return "THE CAT SAT", [], None, ["a", "b"], timeline

    _run, results = _run_prose_evaluation(monkeypatch, ["the cat sat"], execute_full_loop=_two_tools_together)

    assert results[0].execution_details["agent_duration_ms"] == 100 + 80 + 50


def test_a_reused_entry_carries_its_source_latency_not_the_reuse_lookup(monkeypatch):
    """A reused entry reports the source's clock, not the time the lookup took.

    No agent runs on reuse, so the clock around the (skipped) agent call measures
    almost nothing -- a near-perfect latency.
    """

    async def _agent_must_not_run(*_args, **_kwargs):
        raise AssertionError("a reused entry must not call the agent")

    source = SimpleNamespace(
        id=uuid.uuid4(),
        agent_output="THE CAT SAT",
        execution_details={"agent_wall_clock_ms": 4200, "agent_duration_ms": 3000},
        duration_seconds=3.0,
    )
    _run, results = _run_prose_evaluation(
        monkeypatch, ["the cat sat"], execute_full_loop=_agent_must_not_run, reuse_source=source
    )

    details = results[0].execution_details
    assert details["agent_wall_clock_ms"] == 4200
    wall_clock = _metrics_of(results[0])["performance.latency.wall_clock"]
    assert wall_clock["status"] == "scored"
    assert wall_clock["value"] == 4200


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        pytest.param({"agent_wall_clock_ms": 4200}, 4200, id="recorded_clock"),
        pytest.param({"agent_wall_clock_ms": 4200.7}, 4201, id="recorded_clock_rounded"),
        # A row written before the clock: the step total it carries is another
        # metric, and ranking it beside fresh entries' clock compares unlike figures.
        pytest.param({"agent_duration_ms": 3000}, None, id="step_total_only"),
        pytest.param({}, None, id="empty_details"),
        pytest.param(None, None, id="no_details"),
        # 0, or a fraction that rounds to it, would be a perfect latency from
        # nothing measured; Infinity (accepted by json.loads from an imported
        # package) would crash the rounding.
        *[
            pytest.param({"agent_wall_clock_ms": bad}, None, id=f"not_a_measurement_{label}")
            for label, bad in (
                ("zero", 0),
                ("rounds_to_zero", 0.3),
                ("negative", -5),
                ("bool", True),
                ("string", "4200"),
                ("infinity", float("inf")),
                ("nan", float("nan")),
            )
        ],
    ],
)
def test_recover_reused_wall_clock(details, expected):
    assert ee._recover_reused_wall_clock_ms(details) == expected


def test_a_healthy_prose_run_carries_both_judge_and_deterministic_scores(monkeypatch):
    """Deterministic scoring is ADDITIVE to the judge, not a replacement.

    The degradation tests below only ever assert the judge half, so without this a run
    that silently stopped computing prose metrics would still look correct to them.
    """
    run, results = _run_prose_evaluation(monkeypatch, ["the cat sat"])

    assert run.status == "completed"
    assert len(results) == 1
    result = results[0]

    assert result.score == 88.0  # the judge still ran

    metrics = _metrics_of(result)
    assert {"prose.exact_match", "prose.token_prf", "prose.rouge_l"} <= set(metrics)
    for key in ("prose.exact_match", "prose.token_prf", "prose.rouge_l"):
        assert metrics[key]["status"] == "scored"
    # Casing alone did not cost the match, so the run really used the chosen policy.
    assert metrics["prose.exact_match"]["value"] == 100.0
    # The phrase metrics cannot be configured, so the platform never stores
    # them.
    for key in ("prose.required_phrase_coverage", "prose.forbidden_phrase_absence"):
        assert key not in metrics


@pytest.mark.parametrize(
    "judge_error",
    [
        RuntimeError("Error code: 529 - overloaded_error"),
        ValueError("Error code: 400 - invalid_request_error"),
        KeyError("unexpected"),
    ],
    ids=["transient", "bad_request", "unexpected"],
)
def test_a_judge_call_that_raises_is_a_judge_failure_not_the_agents(monkeypatch, judge_error):
    """A raising judge keeps the agent's answer and metrics, flags a judge failure."""

    async def _raising_judge(*_args, **_kwargs):
        raise judge_error

    async def _nothing_counted(_db, _run_id):
        # The only entry is a judge failure, left out of the headlines.
        return True

    monkeypatch.setattr(ee, "_nothing_counted", _nothing_counted)
    run, results = _run_prose_evaluation(monkeypatch, ["the cat sat"], score_result=_raising_judge)

    # Nothing counted: the run reports no measurement, not the agent's error.
    assert run.status == "failed"
    assert run.error == ee.NOTHING_COUNTED_ERROR
    assert len(results) == 1
    result = results[0]
    # The agent's real answer is kept, so a rescore can judge it again.
    assert result.agent_output == "THE CAT SAT"
    assert result.is_judge_failure is True
    # The judge's error is not classified as the agent's run.
    assert result.execution_anomaly_kind is None
    assert result.evaluator_reasoning.startswith("Judge failure: the judge call raised")
    # The deterministic quality metrics still ran on the agent's answer.
    metrics = _metrics_of(result)
    assert metrics["prose.exact_match"]["status"] == "scored"
    assert metrics["prose.exact_match"]["value"] == 100.0


def test_an_unbuildable_prose_config_degrades_the_whole_run_to_judge_only(monkeypatch):
    """Run-scoped degradation: no deterministic value anywhere, every entry left out with
    the reason, and the run still completes."""
    run, results = _run_prose_evaluation(
        monkeypatch, ["the cat sat", "the cat sat"], break_run_config=True
    )

    assert run.status == "completed"
    assert run.error is None
    assert len(results) == 2
    for result in results:
        assert result.score == 88.0  # judge unaffected
        assert _left_out(_prose_metrics_of(result))  # no fabricated value, and the reason
        # Performance metrics do not depend on the quality config and must survive.
        assert any(k.startswith("performance.") for k in _metrics_of(result))


def test_a_non_text_reference_costs_only_its_own_entry(monkeypatch):
    """Entry-scoped degradation, the different blast radius.

    The same exception type drives both scopes and only the catch site differs, which is
    why this needs its own test: catching a malformed dataset row at the run level would
    silently cost every other entry its deterministic metrics.
    """
    run, results = _run_prose_evaluation(
        monkeypatch, ["the cat sat", {"not": "text"}, "the cat sat"]
    )

    assert run.status == "completed"
    assert len(results) == 3

    good_first, malformed, good_last = results
    assert _left_out(_prose_metrics_of(malformed))
    for result in (good_first, good_last):
        assert _prose_metrics_of(result)["prose.exact_match"]["status"] == "scored"
    # The judge is untouched on every entry, malformed one included.
    assert [r.score for r in results] == [88.0, 88.0, 88.0]


# ---------------------------------------------------------------------------


def _run_classification_evaluation(
    monkeypatch,
    entries,
    *,
    predictions,
    label_vocabulary=None,
    label_vocabularies=None,
    output_extractor=None,
):
    """Drive a real ``execute_evaluation_run`` for a CLASSIFICATION evaluation with a
    declared ``label_vocabulary`` threaded from the dataset.

    ``label_vocabularies`` (one value per entry, ``None`` where that entry's own dataset
    declared nothing) lets a test simulate an evaluation linking MULTIPLE datasets with
    different declarations -- otherwise every entry shares the single ``label_vocabulary``
    value, broadcast to all of them.

    Mirrors ``_run_prose_evaluation``'s harness -- only the DB, agent loop and judge are
    faked -- but exercises the classification family end to end, proving the declared
    vocabulary (not just the derived one) reaches ``build_quality_config`` through the
    live-entries query's 4th column.
    """
    run_id, agent_id, evaluation_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    run = SimpleNamespace(
        id=run_id,
        status="pending",
        agent_id=agent_id,
        flow_id=None,
        evaluation_id=evaluation_id,
        model_id="gpt-test",
        is_default_model=False,
        persona_version_number=None,
        prompt_versions=None,
        skill_versions=None,
        placebo_comparison_id=None,
        arm="real",
        agent_config_versions=None,
        started_at=None,
        completed_at=None,
        completed_entries=0,
        error=None,
        score=None,
        f1_score=None,
        duration_seconds=None,
        metric_suite_version=None,
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Eval Agent",
        persona="You are helpful",
        temperature=0.2,
        max_iterations=5,
        knowledge_base_ids=[],
        integration_bindings={},
        integration_allowed_tools={},
        mcp_server_ids=[],
        custom_tool_ids=[],
        output_format=None,
        output_schema=None,
    )
    evaluation = SimpleNamespace(
        id=evaluation_id,
        family="CLASSIFICATION",
        default_output_schema=None,
        scorer_selection=["judge"],
        execution_mode="full_loop",
        apply_prompt_guards=False,
        require_tool_approval=False,
        evaluator_model="judge-model",
        evaluator_prompt=None,
        evaluation_instructions=None,
        sub_score_weights=None,
        penalty_factor=0.5,
        run_mode="sequential",
        trigger_config={},
        timeout_seconds=3600,
        judge_max_tokens=8192,
    )
    vocab_per_entry = (
        label_vocabularies if label_vocabularies is not None else [label_vocabulary] * len(entries)
    )
    entry_rows = [
        (
            SimpleNamespace(
                id=uuid.uuid4(),
                input_prompt=f"Question {i}",
                expected_answer=answer,
                file_ids=None,
                evaluation_prompt=None,
                position=i,
            ),
            None,  # dataset_instructions
            None,  # dataset_output_schema
            vocab_per_entry[i - 1],  # dataset_label_vocabulary
        )
        for i, answer in enumerate(entries, start=1)
    ]
    db = _FakeSession(
        execute_results=[
            _ScalarResult(run),
            _ScalarResult(agent),
            # Per-agent output extractor, resolved once per run between
            # the agent and evaluation loads. None = score the raw output.
            _ScalarResult(output_extractor),
            _ScalarResult(evaluation),
            _EntriesResult(entry_rows),
            _CountResult(len(entry_rows)),
            _CountResult(len(entry_rows)),
        ]
    )

    predictions_iter = iter(predictions)

    async def _mock_execute_full_loop(*_args, **_kwargs):
        return next(predictions_iter), [], None, [], []

    async def _mock_score_result(*_args, **_kwargs):
        return {
            "score": 88.0,
            "sub_scores": {k: 88 for k in ee.SUB_SCORE_WEIGHTS},
            "evaluator_reasoning": "ok",
            "judge_model": "judge-model",
            "judge_provider": "openai",
            "judge_stop_reason": "stop",
            "judge_truncated": False,
            "is_judge_failure": False,
            "judge_usage": {},
            "judge_system_prompt": "sys",
            "judge_user_prompt": "usr",
            "judge_raw_response": "{}",
        }

    patch_evaluation_run(monkeypatch, full_loop=_mock_execute_full_loop, score=_mock_score_result)

    asyncio.run(ee.execute_evaluation_run(db, str(run_id)))

    results = [obj for obj in db.added if hasattr(obj, "execution_details")]
    return run, results


def test_declared_vocabulary_lets_a_broader_predicted_label_score_instead_of_invalidate(
    monkeypatch,
):
    """A run against a dataset with a broader declared vocabulary scores a
    dataset-unseen-but-vocabulary-listed predicted label as a classification false
    positive (degraded, non-zero metrics), while a label absent from the declared
    vocabulary still invalidates the run."""
    run, results = _run_classification_evaluation(
        monkeypatch,
        ['["malware"]'],
        predictions=['["malware", "phishing"]'],
        label_vocabulary=["malware", "phishing"],
    )

    assert run.status == "completed"
    assert len(results) == 1
    metrics = _metrics_of(results[0])
    micro = metrics["classification.micro_prf"]
    assert micro["status"] == "scored"
    assert 0.0 < micro["value"] < 100.0


def test_a_label_absent_from_the_declared_vocabulary_is_scored_not_withheld(monkeypatch):
    """The violation is a value on the entry, not an entry with no values.

    Marking every classification metric ``invalid`` would let ``extract_metrics`` drop
    them — the run would show a blank where the penalty belongs, and the aggregate would
    be computed as if the entry had never been scored.
    """
    run, results = _run_classification_evaluation(
        monkeypatch,
        ['["malware"]'],
        predictions=['["ransomware"]'],  # not in the declared vocabulary
        label_vocabulary=["malware", "phishing"],
    )

    assert run.status == "completed"
    assert len(results) == 1
    metrics = _metrics_of(results[0])

    # Nothing recalled and something invented: a real zero, reached by measurement.
    micro = metrics["classification.micro_prf"]
    assert micro["status"] == "scored"
    assert micro["value"] == 0.0

    gate = metrics["classification.vocabulary_validity"]
    assert gate["status"] == "scored"
    assert gate["value"] == 0.0
    assert list(gate["detail"]["offending_labels"]) == ["ransomware"]


def test_multi_dataset_vocabulary_is_unioned_not_first_found_wins(monkeypatch):
    """An evaluation linking a dataset that declares a vocabulary AND a dataset
    that declares none must not drop the undeclared dataset's own reference labels
    from the run-wide vocabulary. Taking the first non-empty declaration
    across ALL entries as authoritative for the whole run would invalidate the
    undeclared dataset's entries whenever they come after a declaring one."""
    run, results = _run_classification_evaluation(
        monkeypatch,
        ['["malware"]', '["ransomware"]'],
        predictions=['["phishing"]', '["ransomware"]'],
        label_vocabularies=[["malware", "phishing"], None],
    )

    assert run.status == "completed"
    assert len(results) == 2
    # entry 1: predicted "phishing" is unseen in its own reference but IS in its
    # dataset's declared vocabulary -> a false positive, not an invalidation.
    micro_1 = _metrics_of(results[0])["classification.micro_prf"]
    assert micro_1["status"] == "scored"
    # entry 2: its dataset declared no vocabulary at all, so it must fall back to
    # its OWN derived reference labels ("ransomware") -- not be silently held to
    # entry 1's dataset's declared vocabulary, which would invalidate a perfect
    # match on a label that vocabulary never mentions.
    micro_2 = _metrics_of(results[1])["classification.micro_prf"]
    assert micro_2["status"] == "scored"
    assert micro_2["value"] == 100.0



def test_a_configured_extractor_is_read_and_applied(monkeypatch):
    """The extractor query's result is CONSUMED, not merely tolerated.

    Otherwise the queue slot for that query is indistinguishable from padding:
    every other classification test passes ``None`` there, so only a
    configured extractor proves the engine reads it and scores the extracted
    prediction rather than the raw answer.

    The raw answer here is the harvester's merged shape, which as a label set
    would be nonsense; extracted, it is exactly the reference.
    """
    raw = json.dumps(
        {
            "report.pdf": [
                {"text": "evidence", "predictions": {"T1059.001": 0.9, "T1105": 0.7}}
            ]
        }
    )
    extractor = {
        "source": {"kind": "agent_output"},
        "operations": [
            {"op": "parse_json"},
            {"op": "object_values"},
            {"op": "flatten"},
            {"op": "collect_keys", "path": ["predictions"]},
            {"op": "dedupe_sort"},
        ],
        "output_kind": "label_set",
    }

    run, results = _run_classification_evaluation(
        monkeypatch,
        ['["T1059.001", "T1105"]'],
        predictions=[raw],
        label_vocabulary=["T1059.001", "T1105"],
        output_extractor=extractor,
    )

    assert run.status == "completed"
    metrics = (results[0].execution_details or {}).get("metric_results") or {}
    assert "extraction.failed" not in metrics
    # Scored against the EXTRACTED labels: the raw merged document could never
    # match the reference label list.
    assert metrics["classification.micro_prf"]["status"] == "scored"
    assert metrics["classification.micro_prf"]["value"] == 100.0
    assert results[0].agent_output == raw


# --- The run ledger reaches the engine ------------------------------


@pytest.mark.parametrize("raises", [False, True], ids=["returns", "raises"])
def test_the_agent_turn_hands_its_ledger_to_the_engine(monkeypatch, raises):
    """``run_agent_chat`` publishes its ledger on every exit; the engine keeps it on a raise too."""
    from app.services import agent_chat

    ledger = {"calls": [_LEDGER_MODEL_CALL], "spawned_task_ids": []}

    async def _turn(*_args, **kwargs):
        kwargs["loop_outcome_out"]["ledger"] = ledger
        if raises:
            raise RuntimeError("the tool loop blew up")
        return "answer", None

    monkeypatch.setattr(agent_chat, "run_agent_chat", _turn)
    agent = SimpleNamespace(id=uuid.uuid4(), is_company_managed=False)
    outcome: dict = {}

    call = ee._execute_agent_full_loop(object(), agent, "model", "prompt", outcome_out=outcome)
    if raises:
        with pytest.raises(RuntimeError, match="blew up"):
            asyncio.run(call)
    else:
        output, *_ = asyncio.run(call)
        assert output == "answer"

    assert outcome["ledger"] is ledger


# --- Time to first token on Full Loop -----------------------------------


def _full_loop_answering(started_at):
    """A turn whose answer started at the instant ``started_at()`` returns while it runs."""

    async def _turn(*_args, **kwargs):
        await asyncio.sleep(0.02)
        kwargs["eval_result_out"]["answer_started_at"] = started_at()
        await asyncio.sleep(0.02)
        return "answer", None

    return _turn


def test_full_loop_time_to_first_token_runs_from_the_agent_call_to_the_answers_start(monkeypatch):
    """Measured from the same start as the latency, so it never exceeds it."""
    from app.services import agent_chat

    monkeypatch.setattr(agent_chat, "run_agent_chat", _full_loop_answering(time.perf_counter))
    agent = SimpleNamespace(id=uuid.uuid4(), is_company_managed=False)

    called_at = time.perf_counter()
    _output, _ids, ttft_ms, _tools, _traces = asyncio.run(
        ee._execute_agent_full_loop(object(), agent, "model", "prompt", outcome_out={})
    )
    wall_clock_ms = (time.perf_counter() - called_at) * 1000

    assert ttft_ms is not None
    assert 15 <= ttft_ms <= wall_clock_ms


@pytest.mark.parametrize(
    "started_at",
    [lambda: None, lambda: "soon", lambda: 0.0],
    ids=["not_observed", "not_a_number", "before_the_call"],
)
def test_full_loop_has_no_time_to_first_token_without_a_usable_start(monkeypatch, started_at):
    from app.services import agent_chat

    monkeypatch.setattr(agent_chat, "run_agent_chat", _full_loop_answering(started_at))
    agent = SimpleNamespace(id=uuid.uuid4(), is_company_managed=False)

    _output, _ids, ttft_ms, _tools, _traces = asyncio.run(
        ee._execute_agent_full_loop(object(), agent, "model", "prompt", outcome_out={})
    )

    assert ttft_ms is None


@pytest.mark.parametrize(
    ("published", "expected"),
    [
        pytest.param({"tool_names": ["search", "fetch", "search", ""]}, ["fetch", "search"], id="sorted_and_deduplicated"),
        pytest.param({"tool_names": []}, [], id="no_tools"),
        # An early return publishes nothing: unknown, never ``[]``, which grades tool selection 100.
        pytest.param({}, None, id="not_published"),
    ],
)
def test_full_loop_reports_the_agents_tools_or_none(monkeypatch, published, expected):
    from app.services import agent_chat

    async def _turn(*_args, **kwargs):
        kwargs["eval_result_out"].update(published)
        return "answer", None

    monkeypatch.setattr(agent_chat, "run_agent_chat", _turn)
    agent = SimpleNamespace(id=uuid.uuid4(), is_company_managed=False)

    _output, _ids, _ttft, tools, _traces = asyncio.run(
        ee._execute_agent_full_loop(object(), agent, "model", "prompt", outcome_out={})
    )

    assert tools == expected


# --- The entry's chain ledger ----------------------------------------

_LEDGER_MODEL_CALL = {
    "kind": "model",
    "name": "llm_call",
    "model": "gpt-a",
    "duration_ms": 40.0,
    "outcome": "success",
    "usage": {"input": 300, "output": 25, "total": 325},
}


def _full_loop_publishing(ledger: dict):
    async def _run(*_args, **kwargs):
        kwargs["outcome_out"]["ledger"] = ledger
        return "THE CAT SAT", [], None, [], []

    return _run


def _script_tasks(
    monkeypatch, polls: list[dict], *, bound_seconds: float = 5.0, continuations=None, cancelled_at_poll=None
):
    """Script what each poll of the delegated-task wait reads.

    ``polls[i]`` maps task id -> (status, metadata[, started]) for the i-th read;
    the last one repeats, and ``started`` defaults to true. ``continuations`` maps
    a continued task id to its continuations. ``cancelled_at_poll`` is the read
    from which the run reads ``cancelled``.
    """
    from app.tools import wait_policy

    reads = {"n": 0}

    async def _rows(_session, task_ids):
        poll = polls[min(reads["n"], len(polls) - 1)]
        reads["n"] += 1
        return {t: (*poll[t], True)[:3] for t in task_ids if t in poll}

    async def _continuations(_session, task_ids):
        return {t: list((continuations or {}).get(t, [])) for t in task_ids if (continuations or {}).get(t)}

    async def _bound(_run):
        return bound_seconds

    async def _cancelled(_session, _run_uuid):
        return cancelled_at_poll is not None and reads["n"] >= cancelled_at_poll

    monkeypatch.setattr(ee, "_run_was_cancelled", _cancelled)
    monkeypatch.setattr(ee, "_load_task_rows", _rows)
    monkeypatch.setattr(ee, "_load_continuations", _continuations)
    monkeypatch.setattr(ee, "_delegation_wait_seconds", _bound)
    monkeypatch.setattr(wait_policy, "WAIT_POLL_INTERVAL", 0.01)
    return reads


def test_an_entry_records_its_chain_ledger_and_scores_from_it(monkeypatch):
    ledger = {
        "calls": [
            _LEDGER_MODEL_CALL,
            {"kind": "tool", "name": "lookup", "duration_ms": 5.0, "outcome": "success"},
            _LEDGER_MODEL_CALL,
        ],
        "spawned_task_ids": [],
    }

    _run, results = _run_prose_evaluation(
        monkeypatch, ["the cat sat"], execute_full_loop=_full_loop_publishing(ledger)
    )

    details = results[0].execution_details
    assert details["chain"]["complete"] is True
    assert len(details["chain"]["calls"]) == 3
    metrics = _metrics_of(results[0])
    assert metrics["performance.counts.model_calls"]["value"] == 2
    assert metrics["performance.counts.tool_calls"]["value"] == 1
    assert metrics["performance.tokens.total"]["value"] == 650


def test_a_delegated_task_still_running_at_the_bound_leaves_the_chain_incomplete(monkeypatch):
    task_id = str(uuid.uuid4())
    ledger = {"calls": [_LEDGER_MODEL_CALL], "spawned_task_ids": [task_id]}
    _script_tasks(monkeypatch, [{task_id: ("running", {})}], bound_seconds=0.0)

    _run, results = _run_prose_evaluation(
        monkeypatch, ["the cat sat"], execute_full_loop=_full_loop_publishing(ledger)
    )

    metrics = _metrics_of(results[0])
    chain = results[0].execution_details["chain"]
    assert (chain["complete"], chain["incomplete_reason"]) == (False, "wait_bound_reached")
    assert metrics["performance.tokens.total"]["status"] == "unavailable"
    assert metrics["performance.counts.model_calls"]["status"] == "unavailable"
    # Said why, so a short aggregate can be told from a full one.
    assert metrics["performance.tokens.total"]["detail"]["reason"] == "chain_incomplete: wait_bound_reached"
    assert metrics["performance.latency.wall_clock"]["status"] == "scored"


def test_a_run_cancelled_during_the_wait_stops_waiting_at_once(monkeypatch):
    """A stopped entry: a user's cancel is read at each poll, not at the bound."""
    task_id = str(uuid.uuid4())
    ledger = {"calls": [_LEDGER_MODEL_CALL], "spawned_task_ids": [task_id]}
    reads = _script_tasks(monkeypatch, [{task_id: ("running", {})}], bound_seconds=60.0, cancelled_at_poll=2)

    started = time.monotonic()
    _run, results = _run_prose_evaluation(
        monkeypatch, ["the cat sat"], execute_full_loop=_full_loop_publishing(ledger)
    )

    assert time.monotonic() - started < 5, "the wait ran on to its bound"
    assert reads["n"] == 2
    chain = results[0].execution_details["chain"]
    assert (chain["complete"], chain["incomplete_reason"]) == (False, "run_cancelled")
    assert _metrics_of(results[0])["performance.tokens.total"]["status"] == "unavailable"


# --- Agents without handovers are unaffected -------------------------

_PRICE_ROW = {
    "input_price_per_1m": 2.5,
    "output_price_per_1m": 10,
    "cache_read_price_per_1m": None,
    "cache_write_price_per_1m": None,
    "currency": "USD",
    "resolved_model": "gpt-a",
    "resolved_provider": "openai",
}


def _step_and_ledger_call(kind: str, *, duration_ms: float, usage: dict | None = None, tool: str = "lookup"):
    """One call as the evaluation's timeline records it, and as the run's trace does."""
    from app.services.langfuse_tracing import usage_detail_buckets

    if kind == "llm":
        step = {"type": "llm", "description": "LLM call", "duration_ms": duration_ms, "outcome": "success", "model": "gpt-a", "usage": usage}
        call = {"kind": "model", "name": "llm_call", "model": "gpt-a", "duration_ms": duration_ms, "outcome": "success", "usage": usage_detail_buckets(usage)}
    else:
        step = {"type": "tool", "description": f"Tool call: {tool}", "duration_ms": duration_ms, "outcome": "success", "tool_name": tool}
        call = {"kind": "tool", "name": tool, "duration_ms": duration_ms, "outcome": "success"}
    return step, call


def _entry(steps: list, *, ledger_calls: list | None):
    async def _run(*_args, **kwargs):
        if ledger_calls is not None:
            kwargs["outcome_out"]["ledger"] = {"calls": ledger_calls, "spawned_task_ids": []}
        return "THE CAT SAT", [], None, ["lookup"], list(steps)

    return _run


def _performance(result) -> dict:
    return {
        key: (metric["status"], metric.get("value"))
        for key, metric in _metrics_of(result).items()
        if key.startswith("performance.")
    }


def _no_handover_turn():
    return [
        _step_and_ledger_call("llm", duration_ms=40.0, usage={"input_tokens": 300, "output_tokens": 25}),
        _step_and_ledger_call("tool", duration_ms=5.0),
        _step_and_ledger_call("llm", duration_ms=30.0, usage={"input_tokens": 400, "output_tokens": 60}),
    ]


def test_an_entry_with_no_handover_scores_the_same_from_its_ledger(monkeypatch):
    """Scored from the ledger instead of the timeline, the figures do not change."""
    pairs = _no_handover_turn()
    steps = [step for step, _call in pairs]
    calls = [call for _step, call in pairs]

    _run, before = _run_prose_evaluation(
        monkeypatch, ["the cat sat"], execute_full_loop=_entry(steps, ledger_calls=None), price_rows=[_PRICE_ROW]
    )
    _run, after = _run_prose_evaluation(
        monkeypatch, ["the cat sat"], execute_full_loop=_entry(steps, ledger_calls=calls), price_rows=[_PRICE_ROW]
    )

    assert "chain" not in before[0].execution_details
    assert "chain" in after[0].execution_details
    # The wall clock is timed around each run with a real clock, not
    # read from the timeline or the ledger: two runs measure different
    # milliseconds. It is scored in both; every other figure is equal.
    was, now = _performance(before[0]), _performance(after[0])
    wall_clock = "performance.latency.wall_clock"
    assert was.pop(wall_clock)[0] == "scored"
    assert now.pop(wall_clock)[0] == "scored"
    assert now == was
    assert _performance(after[0])["performance.cost.llm"][0] == "scored"
    for key in ("agent_duration_ms", "traces"):
        expected = before[0].execution_details[key]
        actual = after[0].execution_details[key]
        if key == "traces":  # the judge step carries its own timing
            expected = [t for t in expected if t.get("type") != "judge"]
            actual = [t for t in actual if t.get("type") != "judge"]
        assert actual == expected, key


def test_a_continuation_the_timeline_never_showed_is_now_counted(monkeypatch):
    """A continuation the timeline never showed is counted, and nothing else changes."""
    pairs = _no_handover_turn()
    steps = [step for step, _call in pairs]
    _continuation_step, continuation = _step_and_ledger_call(
        "llm", duration_ms=20.0, usage={"input_tokens": 100, "output_tokens": 200}
    )
    calls = [call for _step, call in pairs] + [continuation]

    _run, before = _run_prose_evaluation(
        monkeypatch, ["the cat sat"], execute_full_loop=_entry(steps, ledger_calls=None), price_rows=[_PRICE_ROW]
    )
    _run, after = _run_prose_evaluation(
        monkeypatch, ["the cat sat"], execute_full_loop=_entry(steps, ledger_calls=calls), price_rows=[_PRICE_ROW]
    )

    was, now = _performance(before[0]), _performance(after[0])
    assert now["performance.tokens.total"][1] == was["performance.tokens.total"][1] + 300
    assert now["performance.counts.model_calls"][1] == was["performance.counts.model_calls"][1] + 1
    assert now["performance.cost.llm"][1] > was["performance.cost.llm"][1]
    # The timeline and its step total are the agent's own steps, untouched.
    assert after[0].execution_details["agent_duration_ms"] == before[0].execution_details["agent_duration_ms"]


def test_the_judge_grades_only_the_agents_own_tool_calls(monkeypatch):
    """A helper's tool calls count in the totals, not in the judge's list."""
    judged: dict = {}

    async def _capturing_judge(*_args, **kwargs):
        judged["tool_calls"] = kwargs.get("tool_calls")
        return {
            "score": 88.0,
            "sub_scores": {k: 88 for k in ee.SUB_SCORE_WEIGHTS},
            "evaluator_reasoning": "ok",
            "judge_model": "judge-model",
            "judge_provider": "openai",
            "judge_stop_reason": "stop",
            "judge_truncated": False,
            "is_judge_failure": False,
            "judge_usage": {},
            "judge_system_prompt": "sys",
            "judge_user_prompt": "usr",
            "judge_raw_response": "{}",
        }

    own_step = {"type": "tool", "description": "Tool call: consult_agent", "duration_ms": 30.0, "outcome": "success", "tool_name": "consult_agent"}
    ledger_calls = [
        {"kind": "tool", "name": "consult_agent", "duration_ms": 30.0, "outcome": "success"},
        {"kind": "tool", "name": "lookup", "duration_ms": 5.0, "outcome": "success"},  # the helper's
    ]

    async def _run(*_args, **kwargs):
        kwargs["outcome_out"]["ledger"] = {"calls": ledger_calls, "spawned_task_ids": []}
        return "THE CAT SAT", [], None, ["consult_agent"], [own_step]

    _run_eval, results = _run_prose_evaluation(
        monkeypatch, ["the cat sat"], execute_full_loop=_run, score_result=_capturing_judge
    )

    assert [t.get("tool_name") for t in judged["tool_calls"]] == ["consult_agent"]
    assert _metrics_of(results[0])["performance.counts.tool_calls"]["value"] == 2


# --- A delegated agent's work is counted -----------------------------

_DELEGATE_CALL = {
    "kind": "model",
    "name": "llm_call",
    "model": "gpt-a",
    "duration_ms": 25.0,
    "outcome": "success",
    "usage": {"input": 500, "output": 80, "total": 580},
}


def _finished(calls=None, spawned=None, **extra) -> tuple[str, dict]:
    return ("completed", {"ledger": {"calls": list(calls or [_DELEGATE_CALL]), "spawned_task_ids": list(spawned or []), "retries": 0}, **extra})


def test_the_entry_waits_for_a_task_the_agent_did_not_wait_for(monkeypatch):
    task_id = str(uuid.uuid4())
    reads = _script_tasks(monkeypatch, [{task_id: ("running", {})}, {task_id: ("running", {})}, {task_id: _finished()}])
    ledger = {"calls": [_LEDGER_MODEL_CALL], "spawned_task_ids": [task_id]}

    _run, results = _run_prose_evaluation(monkeypatch, ["the cat sat"], execute_full_loop=_full_loop_publishing(ledger))

    chain = results[0].execution_details["chain"]
    assert reads["n"] >= 3, "the entry did not wait"
    assert chain["complete"] is True
    assert _metrics_of(results[0])["performance.tokens.total"]["value"] == 325 + 580


def test_a_task_the_agent_waited_for_itself_is_counted_once(monkeypatch):
    task_id = str(uuid.uuid4())
    _script_tasks(monkeypatch, [{task_id: _finished()}])
    ledger = {"calls": [_LEDGER_MODEL_CALL], "spawned_task_ids": [task_id, task_id]}

    _run, results = _run_prose_evaluation(monkeypatch, ["the cat sat"], execute_full_loop=_full_loop_publishing(ledger))

    assert _metrics_of(results[0])["performance.counts.model_calls"]["value"] == 2


def test_the_wait_follows_a_continuation_and_a_nested_spawn(monkeypatch):
    first, continuation, nested = (str(uuid.uuid4()) for _ in range(3))
    _script_tasks(
        monkeypatch,
        [
            {
                first: ("incomplete", {"ledger": {"calls": [_DELEGATE_CALL], "spawned_task_ids": [nested], "retries": 1}}),
                continuation: _finished(),
                nested: _finished(),
            }
        ],
        continuations={first: [continuation]},
    )
    ledger = {"calls": [_LEDGER_MODEL_CALL], "spawned_task_ids": [first]}

    _run, results = _run_prose_evaluation(monkeypatch, ["the cat sat"], execute_full_loop=_full_loop_publishing(ledger))

    chain = results[0].execution_details["chain"]
    assert chain["complete"] is True
    assert len(chain["calls"]) == 4  # the agent's call, then the task's, its continuation's and its nested task's
    assert chain["retries"] >= 1


def test_the_wait_is_not_part_of_the_latency(monkeypatch):
    """Latency is the time to the answer, not to the delegated work."""
    task_id = str(uuid.uuid4())
    _script_tasks(monkeypatch, [{task_id: ("running", {})}] * 20 + [{task_id: _finished()}])
    ledger = {"calls": [_LEDGER_MODEL_CALL], "spawned_task_ids": [task_id]}

    _run, results = _run_prose_evaluation(monkeypatch, ["the cat sat"], execute_full_loop=_full_loop_publishing(ledger))

    # 20 polls at 10 ms is ~200 ms of waiting; the stubbed agent answers at once.
    assert _metrics_of(results[0])["performance.latency.wall_clock"]["value"] < 150


def test_cancelling_the_entry_stops_the_wait_at_once(monkeypatch):
    task_id = str(uuid.uuid4())
    _script_tasks(monkeypatch, [{task_id: ("running", {})}], bound_seconds=60.0)

    async def _cancel_while_waiting():
        waiting = asyncio.ensure_future(ee._collect_task_ledgers([task_id], bound_seconds=60.0))
        await asyncio.sleep(0.05)
        waiting.cancel()
        started = asyncio.get_running_loop().time()
        try:
            await waiting
        except asyncio.CancelledError:
            return asyncio.get_running_loop().time() - started
        raise AssertionError("the wait was not cancelled")

    assert asyncio.run(_cancel_while_waiting()) < 0.5


def test_delegating_never_records_less_cost_than_doing_the_work_alone(monkeypatch):
    """The same calls cost the same, whoever in the chain made them."""
    task_id = str(uuid.uuid4())
    _script_tasks(monkeypatch, [{task_id: _finished()}])
    delegating = {"calls": [_LEDGER_MODEL_CALL], "spawned_task_ids": [task_id]}
    alone = {"calls": [_LEDGER_MODEL_CALL, _DELEGATE_CALL], "spawned_task_ids": []}
    price = [{"input_price_per_1m": 2.5, "output_price_per_1m": 10, "cache_read_price_per_1m": None, "cache_write_price_per_1m": None, "currency": "USD", "resolved_model": "gpt-a", "resolved_provider": "openai"}]

    _r, with_task = _run_prose_evaluation(monkeypatch, ["the cat sat"], execute_full_loop=_full_loop_publishing(delegating), price_rows=price)
    _r, by_itself = _run_prose_evaluation(monkeypatch, ["the cat sat"], execute_full_loop=_full_loop_publishing(alone), price_rows=price)

    def _cost(result):
        return _metrics_of(result)["performance.cost.llm"]["value"]

    assert _cost(with_task[0]) == pytest.approx(_cost(by_itself[0]))


# --- Reused predictions ----------------------------------------------


async def _agent_must_not_run(*_args, **_kwargs):
    raise AssertionError("a reused entry must not call the agent")


def _reuse(monkeypatch, details: dict):
    source = SimpleNamespace(
        id=uuid.uuid4(),
        agent_output="THE CAT SAT",
        execution_details={"agent_wall_clock_ms": 4200, "agent_duration_ms": 75, **details},
        duration_seconds=3.0,
    )
    _run, results = _run_prose_evaluation(
        monkeypatch,
        ["the cat sat"],
        execute_full_loop=_agent_must_not_run,
        reuse_source=source,
        price_rows=[_PRICE_ROW],
    )
    return results[0]


def test_a_reused_entry_carries_its_sources_chain_and_scores_from_it(monkeypatch):
    pairs = _no_handover_turn()
    delegated = {"kind": "model", "name": "llm_call", "model": "gpt-a", "duration_ms": 9.0, "outcome": "success", "usage": {"input": 50, "output": 5, "total": 55}}
    chain = {
        "calls": [call for _step, call in pairs] + [delegated],
        "usage_by_model": [{"model": "gpt-a", "usage": {"input": 750, "output": 90, "total": 840}}],
        "retries": 0,
        "complete": True,
    }

    result = _reuse(monkeypatch, {"traces": [step for step, _call in pairs], "chain": chain})

    assert result.execution_details["chain"] == chain
    metrics = _metrics_of(result)
    assert metrics["performance.counts.model_calls"]["value"] == 3
    assert metrics["performance.tokens.total"]["value"] == 840


@pytest.mark.parametrize("handover", [True, False], ids=["with-a-handover", "without"])
def test_a_reused_entry_from_before_the_chain_reports_its_totals_unavailable(monkeypatch, handover):
    """Counted without a chain ledger, it is not ranked against chain-counted entries, as with latency."""
    pairs = _no_handover_turn()
    steps = [step for step, _call in pairs]
    if handover:
        steps[1] = {**steps[1], "tool_name": "spawn_background_task", "description": "Tool call: spawn_background_task"}

    result = _reuse(monkeypatch, {"traces": steps, "execution_mode": "full_loop"})

    chain = result.execution_details["chain"]
    assert (chain["complete"], chain["incomplete_reason"]) == (False, "recorded_before_chain")
    metrics = _metrics_of(result)
    for key in ("performance.tokens.total", "performance.cost.llm", "performance.counts.model_calls"):
        assert metrics[key]["status"] == "unavailable", key
        assert metrics[key]["detail"]["reason"] == "chain_incomplete: recorded_before_chain", key
    assert metrics["performance.latency.wall_clock"]["value"] == 4200


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("full_loop", "recorded_before_chain"), ("skip_llm", "recorded_before_chain"), ("llm_only", None)],
)
def test_only_an_llm_only_source_keeps_its_step_figures(mode, expected):
    chain = ee._reused_chain({"traces": []}, mode)

    assert (chain["incomplete_reason"] if chain is not None else None) == expected


# --- Rescore keeps the chain it was scored from ----------------------


def test_a_rescore_keeps_the_chain_and_the_figures_scored_from_it(monkeypatch):
    """Rescore re-runs the judge and quality over the stored answer; it never re-runs
    the agent, so the chain and the ``performance.*`` figures scored from it stay."""
    run_id = uuid.uuid4()
    # As the rescore endpoints leave it.
    run = SimpleNamespace(id=run_id, evaluation_id=uuid.uuid4(), agent_id=uuid.uuid4(), status="running")
    rescored_evaluation = SimpleNamespace(
        evaluator_model="judge-model",
        evaluator_prompt=None,
        evaluation_instructions=None,
        sub_score_weights=None,
        default_output_schema=None,
        scorer_selection=["judge"],
        judge_max_tokens=8192,
    )
    chain = {
        "calls": [_LEDGER_MODEL_CALL, _LEDGER_MODEL_CALL],
        "usage_by_model": [{"model": "gpt-a", "usage": {"input": 600, "output": 50, "total": 650}}],
        "retries": 0,
        "complete": True,
    }
    performance = {
        "performance.tokens.total": {"status": "scored", "value": 650},
        "performance.counts.model_calls": {"status": "scored", "value": 2},
    }
    # The entry it was scored against: one deleted since keeps its verdict.
    entry = SimpleNamespace(
        id=uuid.uuid4(), dataset_id=None, input_prompt="q", expected_answer="the cat sat", evaluation_prompt=None
    )
    row = SimpleNamespace(
        id=uuid.uuid4(),
        run_id=run_id,
        entry_id=entry.id,
        version_entry_id=None,
        f1_score=None,
        agent_output="THE CAT SAT",
        execution_anomaly_kind=None,
        is_judge_failure=False,
        score=0,
        sub_scores=None,
        evaluator_reasoning=None,
        execution_details={"execution_mode": "full_loop", "traces": [], "chain": chain, "metric_results": dict(performance)},
    )

    class _RescoreDB:
        added: list = []

        async def get(self, model, _id):
            if model is ee.DatasetEntry:
                return entry
            return run if model is ee.EvaluationRun else rescored_evaluation

        async def execute(self, _statement):
            if selects_run_status(_statement):
                return SimpleNamespace(scalar_one_or_none=lambda: "running")
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [row]),
                scalar_one_or_none=lambda: None,
            )

        def add(self, obj):
            self.added.append(obj)

        async def commit(self):
            return None

        async def rollback(self):
            return None

    async def _judge(*_a, **_k):
        return {"score": 80, "sub_scores": {}, "evaluator_reasoning": "ok"}

    monkeypatch.setattr(ee, "_compute_run_headlines", HeadlinesFake())
    monkeypatch.setattr(ee, "score_result", _judge)

    assert asyncio.run(ee.rescore_run(_RescoreDB(), str(run_id), previous_status="completed")) == 1

    assert row.score == 80
    assert row.execution_details["chain"] == chain
    kept = {k: v for k, v in row.execution_details["metric_results"].items() if k.startswith("performance.")}
    assert kept == performance


# --- The wait's edge cases -------------------------------------------


def _entry_spawning(task_id: str):
    return _full_loop_publishing({"calls": [_LEDGER_MODEL_CALL], "spawned_task_ids": [task_id]})


def _chain_of(monkeypatch, polls, **script):
    task_id = next(iter(polls[0]))
    reads = _script_tasks(monkeypatch, polls, **script)
    started = time.monotonic()
    _run, results = _run_prose_evaluation(monkeypatch, ["the cat sat"], execute_full_loop=_entry_spawning(task_id))
    return results[0].execution_details["chain"], reads, time.monotonic() - started


def test_a_task_killed_while_it_ran_is_read_once_its_worker_writes_the_ledger(monkeypatch):
    """The kill commits ``cancelled`` first; the worker, still running, writes the ledger after."""
    task_id = str(uuid.uuid4())
    ledger = {"calls": [_DELEGATE_CALL], "spawned_task_ids": [], "retries": 0}

    chain, reads, _elapsed = _chain_of(
        monkeypatch, [{task_id: ("cancelled", {})}, {task_id: ("cancelled", {"ledger": ledger})}]
    )

    assert reads["n"] == 2
    assert chain["complete"] is True
    assert len(chain["calls"]) == 2


def test_a_task_killed_before_it_started_is_not_waited_for(monkeypatch):
    task_id = str(uuid.uuid4())

    chain, reads, elapsed = _chain_of(monkeypatch, [{task_id: ("cancelled", {}, False)}], bound_seconds=60.0)

    assert (reads["n"], elapsed < 5) == (1, True)
    assert chain["complete"] is False  # a flagged kill writes an empty ledger; this row has none


@pytest.mark.parametrize("marked_at_poll", [0, 1], ids=["in-the-same-read", "a-poll-later"])
def test_an_incomplete_task_marked_with_no_continuation_is_not_waited_on(monkeypatch, marked_at_poll):
    from app.services.run_ledger import NO_CONTINUATION_KEY

    task_id = str(uuid.uuid4())
    ledger = {"calls": [_DELEGATE_CALL], "spawned_task_ids": [], "retries": 0}
    unmarked = ("incomplete", {"ledger": ledger})
    marked = ("incomplete", {"ledger": ledger, NO_CONTINUATION_KEY: True})
    polls = [unmarked] * marked_at_poll + [marked]

    chain, reads, elapsed = _chain_of(monkeypatch, [{task_id: p} for p in polls], bound_seconds=60.0)

    assert elapsed < 5, "the wait ran on to its bound"
    assert reads["n"] == marked_at_poll + 1
    assert chain["complete"] is True


def test_a_partial_task_ledger_leaves_the_chain_incomplete(monkeypatch):
    """An earlier attempt the engine lost made calls no ledger holds."""
    task_id = str(uuid.uuid4())
    partial = {"calls": [_DELEGATE_CALL], "spawned_task_ids": [], "retries": 0, "partial": True}

    chain, _reads, _elapsed = _chain_of(monkeypatch, [{task_id: ("completed", {"ledger": partial})}])

    assert chain["complete"] is False


@pytest.mark.parametrize(
    "bad",
    [["not", "a", "ledger"], "a string", {"calls": [], "spawned_task_ids": "not-a-list"}],
    ids=["list", "string", "spawned-not-a-list"],
)
def test_a_malformed_task_ledger_is_read_as_missing_not_raised(monkeypatch, bad):
    task_id = str(uuid.uuid4())

    chain, _reads, _elapsed = _chain_of(monkeypatch, [{task_id: ("completed", {"ledger": bad})}])

    assert chain["complete"] is isinstance(bad, dict)


def test_each_poll_reads_in_one_session(monkeypatch):
    from app.core import database

    task_id = str(uuid.uuid4())
    _script_tasks(monkeypatch, [{task_id: ("running", {})}, {task_id: ("running", {})}, {task_id: _finished()}])
    opened = {"n": 0}

    class _Session:
        async def __aenter__(self):
            opened["n"] += 1
            return object()

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(database, "async_session", _Session)

    ledgers = asyncio.run(ee._collect_task_ledgers([task_id], bound_seconds=5.0, run_uuid=uuid.uuid4()))

    assert ledgers[task_id] is not None
    assert opened["n"] == 3


# --- Pricing a chain -------------------------------------------------


def _row(resolved: str, price: float) -> dict:
    return {**_PRICE_ROW, "resolved_model": resolved, "input_price_per_1m": price}


def _price_chain(monkeypatch, models: list[str | None], resolves: dict[str, dict | None]):
    looked_up: list[str] = []

    async def _rows(_db, model):
        looked_up.append(model)
        row = resolves.get(model)
        return [row] if row else []

    monkeypatch.setattr(ee, "_build_run_price_rows", _rows)
    chain = {
        "calls": [],
        "usage_by_model": [{"model": m, "usage": {"input": 10, "output": 1, "total": 11}} for m in models],
        "retries": 0,
        "complete": True,
    }
    run_rows = [_row("gpt-a", 2.5)]
    inputs = asyncio.run(ee._chain_inputs(None, chain, "gpt-a", run_rows, {}))
    return inputs.price_rows_by_model, run_rows, looked_up


@pytest.mark.parametrize(
    ("models", "resolves", "expected"),
    [
        # "run" stands for the run's own price rows.
        pytest.param(["gpt-a"], {}, {"gpt-a": "run"}, id="the_runs_own_model"),
        pytest.param(
            ["gpt-a-2026-08-01"],
            {"gpt-a-2026-08-01": _row("gpt-a", 9.9)},
            {"gpt-a-2026-08-01": "run"},
            id="a_served_name_resolving_to_the_runs_model",
        ),
        # Only a helper reported usage: its tokens are not priced at the evaluated agent's rates.
        pytest.param(["gpt-b"], {"gpt-b": _row("gpt-b", 0.1)}, {"gpt-b": [_row("gpt-b", 0.1)]}, id="a_lone_helper_model"),
        pytest.param(["unknown-model"], {}, {"unknown-model": "run"}, id="a_lone_model_with_no_row"),
        pytest.param(
            ["gpt-a", "unknown-model"], {}, {"gpt-a": "run", "unknown-model": []}, id="one_of_several_with_no_row"
        ),
    ],
)
def test_each_model_of_a_chain_is_priced_at_its_own_row(monkeypatch, models, resolves, expected):
    rows, run_rows, looked_up = _price_chain(monkeypatch, models, resolves)

    assert rows == {model: run_rows if row == "run" else row for model, row in expected.items()}
    if models == ["gpt-a"]:
        # The run's own model needs no lookup.
        assert looked_up == []


# --- A failed run, a dead worker ------------------------------


def _full_loop_failing_after(ledger: dict):
    async def _run(*_args, **kwargs):
        kwargs["outcome_out"]["ledger"] = ledger
        raise RuntimeError("the tool loop blew up")

    return _run


async def _llm_only_must_not_run(*_args, **_kwargs):
    raise AssertionError("a failed Full Loop entry must not be answered in LLM Only")


@pytest.mark.parametrize(
    "ledger",
    [
        {"calls": [], "spawned_task_ids": []},
        {"calls": [_LEDGER_MODEL_CALL], "spawned_task_ids": ["task"]},
    ],
    ids=["spent-nothing", "spent-and-delegated"],
)
def test_a_full_loop_entry_whose_agent_run_raises_fails_with_its_error(monkeypatch, ledger):
    """The entry is the agent's failure, never an answer from its persona alone.

    It is not re-run in LLM Only. Its delegated work is not waited for: the entry has failed.
    """
    ledger = {**ledger, "spawned_task_ids": [str(uuid.uuid4()) for _ in ledger["spawned_task_ids"]]}
    reads = _script_tasks(monkeypatch, [{t: ("running", {}) for t in ledger["spawned_task_ids"]}], bound_seconds=60.0)
    monkeypatch.setattr(ee, "_execute_agent_llm_only", _llm_only_must_not_run)

    run, results = _run_prose_evaluation(monkeypatch, ["the cat sat"], execute_full_loop=_full_loop_failing_after(ledger))

    [result] = results
    assert result.agent_output == "Error: the tool loop blew up"
    assert result.score == 0
    # No answer was scored: the entry keeps only why its run failed.
    assert set(result.execution_details) == {"agent_run_failure", "metric_results"}
    assert run.execution_mode == "full_loop"
    assert reads["n"] == 0


def test_a_task_killed_while_its_worker_died_is_given_up_after_the_grace(monkeypatch):
    """No worker writes its ledger: the entry is not held to the wait's bound for it."""
    task_id = str(uuid.uuid4())
    monkeypatch.setattr(ee, "_KILLED_TASK_LEDGER_GRACE_SECONDS", 0.05)

    chain, reads, elapsed = _chain_of(monkeypatch, [{task_id: ("cancelled", {})}], bound_seconds=60.0)

    assert elapsed < 5, "the wait ran on to its bound"
    assert reads["n"] >= 2, "a live worker was not given its grace"
    assert (chain["complete"], chain["incomplete_reason"]) == (False, "ledger_missing")


def test_an_id_that_is_no_chain_task_is_not_waited_for(monkeypatch):
    """A task id is read off a tool's output; one the evaluation's chain did not spawn has no row here."""
    reads = _script_tasks(monkeypatch, [{}], bound_seconds=60.0)
    started = time.monotonic()

    _run, results = _run_prose_evaluation(
        monkeypatch, ["the cat sat"], execute_full_loop=_entry_spawning(str(uuid.uuid4()))
    )

    assert time.monotonic() - started < 5, "the wait ran on to its bound"
    assert reads["n"] == 1
    chain = results[0].execution_details["chain"]
    assert (chain["complete"], chain["incomplete_reason"]) == (False, "ledger_missing")


# --- A model no provider offers ----------------------------------------
# The model resolver falls back to another provider's model when none lists the
# one asked for: a run on a saved model is answered by that model or not at all.


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("is_default_model", "skip_llm", "status"),
    [(False, False, "failed"), (True, False, "completed"), (False, True, "completed")],
    ids=["a-saved-model", "the-agents-default", "a-model-independent-agent"],
)
async def test_a_run_on_a_model_no_provider_offers_fails_before_calling_it(
    monkeypatch, is_default_model, skip_llm, status
):
    """A saved model fails the run with the reason and no entry runs. The agent's
    default model runs on whatever the agent resolves to, and a model-independent
    agent calls no model."""
    monkeypatch.setattr(ee, "_compute_run_headlines", HeadlinesFake())

    run, results = await run_on_postgres(
        monkeypatch,
        scorer_selection=[],
        answers=["an answer"],
        offered_models=(),
        is_default_model=is_default_model,
        skip_llm=skip_llm,
    )

    assert run.status == status, run.error
    assert run.completed_at is not None
    if status == "failed":
        assert run.error == "Model gpt-4o is not a chat model, or no provider this evaluation can use offers it"
        assert results == []
    else:
        assert len(results) == 1


# ── LLM Only delivers what the agent delivers ─────────────────────


class _AnswersWith:
    """A provider that streams one fixed answer, or answers it once streaming fails."""

    provider_name = "scripted"
    last_stop_reason = "stop"

    def __init__(self, answer: str, *, streams: bool):
        self.answer = answer
        self.streams = streams

    async def chat_stream(self, messages=None, **_kwargs):
        if not self.streams:
            raise ConnectionError("stream refused")
        yield self.answer

    async def chat(self, messages=None, **_kwargs):
        from app.llm.base import ChatResponse

        return ChatResponse(content=self.answer, model="m", stop_reason="end_turn")


async def _llm_only_output(monkeypatch, agent, provider) -> str:
    from app.services import langfuse_tracing

    async def _provider(_db, _model_id, **_kwargs):
        return provider, None

    async def _trace(*_args, **_kwargs):
        return {"trace": None}

    monkeypatch.setattr(ee, "_get_llm_provider_and_model", _provider)
    monkeypatch.setattr(langfuse_tracing, "trace_llm_call", _trace)
    monkeypatch.setattr(langfuse_tracing, "end_trace", lambda *_a, **_k: None)
    monkeypatch.setattr(langfuse_tracing, "get_local_trace_id", lambda *_a, **_k: None)
    output, _trace_ids, _ttft, _traces = await ee._execute_agent_llm_only(
        None, agent, "m", "Extract the indicators."
    )
    return output


_FENCED = '```json\n{"indicators": ["evil.com"]}\n```'


@pytest.mark.asyncio
@pytest.mark.parametrize("streams", [True, False], ids=["streamed", "stream-fallback"])
@pytest.mark.parametrize(
    ("output_format", "delivered"),
    [("json", {"indicators": ["evil.com"]}), ("markdown", None)],
    ids=["json-producer", "markdown"],
)
async def test_llm_only_delivers_what_the_agent_delivers_in_a_chat(
    monkeypatch, streams, output_format, delivered
):
    """Manual test case 13: the same fenced answer scored 100 in Full Loop, which
    delivers bare JSON for an agent declared as a JSON producer, and 0 in LLM Only. Any
    other agent keeps its answer, on both of LLM Only's paths."""
    from app.models.agent import Agent

    agent = Agent(id=uuid.uuid4(), name="Extractor", persona="p", output_format=output_format)
    output = await _llm_only_output(monkeypatch, agent, _AnswersWith(_FENCED, streams=streams))
    if delivered is None:
        assert output == _FENCED
    else:
        assert json.loads(output) == delivered


# ── Overall Quality says how many entries it covers ─────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_overall_quality_says_which_entries_it_leaves_out_and_why(monkeypatch):
    """An entry with an empty expected answer is never scored, so the run's quality
    is computed over the other entry, and the run says so."""
    run, _ = await run_on_postgres(
        monkeypatch,
        scorer_selection=[],
        answers=["the cat sat", "anything"],
        family="PROSE",
        expected_answers=["the cat sat", ""],
    )

    assert run.status == "completed", run.error
    assert run.quality_score == 100.0
    assert run.headline_details["quality_coverage"] == {
        "entries": 2,
        "counted": 1,
        "left_out": {"reference_empty": 1},
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_rescore_whose_expected_answers_cannot_be_scored_keeps_no_old_quality_value(monkeypatch):
    """The previous pass's quality values would describe a configuration that no longer
    holds: the entry says why it is left out instead, and the run's figures drop them.
    Performance stays, scored from the agent's run, which a rescore does not repeat."""
    from sqlalchemy import select

    from app.models.evaluation import EvaluationResult
    from app.models.evaluation_metric import EvaluationMetric

    seen: dict = {}

    async def _stored(session, run_id):
        result = (await session.execute(select(EvaluationResult).where(EvaluationResult.run_id == run_id))).scalar_one()
        rows = await session.execute(select(EvaluationMetric.key).where(EvaluationMetric.run_id == run_id))
        return result.execution_details["metric_results"], set(rows.scalars())

    async def _rescore_with_a_broken_config(session, run_id):
        def _raise(*_args, **_kwargs):
            raise ee.QualityConfigError("the vocabulary is gone")

        seen["results"], seen["rows"] = await _stored(session, run_id)
        monkeypatch.setattr(ee, "build_quality_config", _raise)
        await rescore_as_started(session, run_id)
        seen["after_rows"] = (await _stored(session, run_id))[1]

    run, [rescored] = await run_on_postgres(
        monkeypatch,
        scorer_selection=[],
        answers=["the cat sat"],
        family="PROSE",
        expected_answers=["the cat sat"],
        then=_rescore_with_a_broken_config,
    )

    after = rescored.execution_details["metric_results"]
    assert seen["results"]["prose.exact_match"]["status"] == "scored"
    assert after["prose.exact_match"]["status"] == "unavailable"
    assert "the vocabulary is gone" in after["prose.exact_match"]["detail"]["message"]
    performance = {k for k in seen["results"] if k.startswith("performance.")}
    assert {k for k in after if k.startswith("performance.")} == performance
    assert run.quality_score is None
    # The rows the run's figures are read from: the quality ones go, the platform's
    # timing of the agent's run stays.
    prose_rows = {k for k in seen["rows"] if k.startswith("prose.")}
    assert prose_rows and seen["rows"] - seen["after_rows"] == prose_rows
    assert "run_duration_seconds" in seen["after_rows"]


# ── An entry whose agent run raised ──────────────────────────────


def _raises_by_owner():
    """One answer, then a raise the agent caused, one we caused, one the provider caused."""
    from app.llm.base import ContextOverflowError

    return [
        "a",
        ContextOverflowError(estimated_tokens=300_000, context_window=128_000, model="gpt-4o"),
        RuntimeError("the vector store is gone"),
        TimeoutError("the provider did not answer"),
    ]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answers", "status"),
    [
        # Ours and the provider's raises leave every headline: nothing was measured.
        pytest.param(
            [RuntimeError("the vector store is gone"), TimeoutError("the provider did not answer")],
            "failed",
            id="nothing_counted",
        ),
        # The agent's own raise scores 0 and counts: a measurement.
        pytest.param(
            [RuntimeError("the vector store is gone"), _raises_by_owner()[1]],
            "completed",
            id="the_agent_failed_one",
        ),
        pytest.param([RuntimeError("the vector store is gone"), "a"], "completed", id="one_answered"),
    ],
)
async def test_a_run_none_of_whose_entries_counts_ends_failed(monkeypatch, answers, status):
    """``completed`` would report a measurement the run did not make: its
    headlines read N/A, like a callback-backed run whose episode failed."""
    run, _ = await run_on_postgres(monkeypatch, scorer_selection=["judge"], answers=answers)

    assert run.status == status
    assert run.error == (ee.NOTHING_COUNTED_ERROR if status == "failed" else None)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("previous_status", ["completed", "cancelled", "failed"])
async def test_a_rescore_hands_the_run_back_its_status_and_records_its_suite(monkeypatch, previous_status):
    """A rescore neither finishes a cancelled run nor repairs a failed one: the
    run keeps its status, and records the suite version the rescore scored with."""

    async def _rescore(session, run_id):
        run = await session.get(ee.EvaluationRun, run_id)
        run.status = previous_status
        run.metric_suite_version = "0.0.0"
        await session.flush()
        await rescore_as_started(session, run_id)

    run, _ = await run_on_postgres(
        monkeypatch, scorer_selection=["judge"], answers=["a"], family="PROSE",
        expected_answers=["a"], then=_rescore,
    )

    assert run.status == previous_status
    assert run.metric_suite_version == ee.metric_suite_version()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_rescore_refuses_a_run_the_endpoints_did_not_start(monkeypatch):
    """A rescore owns a run only while it reads ``running``, as the endpoints leave it:
    started on a finished run, it says so instead of stopping as if
    cancelled."""
    seen: dict = {}

    async def _rescore_a_completed_run(session, run_id):
        with pytest.raises(ValueError) as exc:
            await ee.rescore_run(session, str(run_id), previous_status="completed")
        seen["code"] = str(exc.value)

    run, _ = await run_on_postgres(
        monkeypatch, scorer_selection=["judge"], answers=["a"], then=_rescore_a_completed_run
    )

    assert seen["code"] == "run_not_running"
    assert run.status == "completed"


async def _run_metrics(session, run_id):
    from app.services.evaluation.aggregation import aggregate_run_metrics

    return await aggregate_run_metrics(session, run_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_raised_entry_is_treated_the_same_by_the_three_headlines(monkeypatch, caplog):
    """The agent's raise scores 0 in Judge Quality and Overall Quality and is a failed
    call in the completion rate; ours and the provider's leave every headline, and
    Overall Quality says it was computed without ours."""
    seen: dict = {}

    async def _figures(session, run_id):
        from app.api.v1.evaluations import get_run, search_runs
        from app.api.v1.schemas.evaluation import RunSearchRequest

        seen["metrics"] = await _run_metrics(session, run_id)
        admin = SimpleNamespace(id=uuid.uuid4(), is_admin=True)
        seen["served"] = (await get_run(str(run_id), user=admin, db=session))["platform_error_count"]
        found = await search_runs(RunSearchRequest(), user=admin, db=session)
        seen["searched"] = [r["platform_error_count"] for r in found["runs"]]

    run, results = await run_on_postgres(
        monkeypatch,
        scorer_selection=["judge"],
        answers=_raises_by_owner(),
        family="CLASSIFICATION",
        expected_answers=["a"] * 4,
        then=_figures,
    )

    assert run.status == "completed", run.error
    assert [r.execution_anomaly_kind for r in results] == [None, None, "platform_error", "transient_error"]
    assert results[1].execution_details["agent_run_failure"]["failure_cause"] == "context_overflow"
    assert results[2].execution_details["agent_run_failure"]["failure_cause"] == "unknown"
    # Judge Quality: the answer's 80 and the agent's 0, less the spread penalty.
    assert run.score == pytest.approx(20.0)
    # Overall Quality over the answer and the agent's 0, counted as the expected answer:
    # micro F1 2 of 4, macro F1 of `a` (tp 1, fn 1) 66.67, samples F1 50 less 25.
    contributions = run.headline_details["quality_contributions"]
    assert contributions == pytest.approx(
        {"classification.micro_prf": 50.0, "classification.macro_prf": 66.67, "classification.samples_f1": 25.0}
    )
    assert run.quality_score == pytest.approx(47.22)
    assert run.headline_details["quality_coverage"] == {
        "entries": 3,
        "counted": 2,
        "left_out": {"platform_error": 1},
    }
    # Overall Performance: the call that raised is the run's only measured call.
    completion = seen["metrics"]["performance.reliability.completion_rate"]
    assert (completion.value, completion.entry_count) == (0.0, 1)
    assert seen["metrics"]["classification.micro_prf"].entry_count == 2
    # Wherever runs are listed or compared, the run says how many entries we left out,
    # and the server log says why.
    assert seen["served"] == 1
    assert seen["searched"] == [1]
    assert [
        record.getMessage().split(" of run ", 1)[1].split(": ", 1)[1]
        for record in caplog.records
        if record.getMessage().startswith("The platform could not run")
    ] == ["unknown=1"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_rescore_keeps_the_treatment_of_a_raised_entry(monkeypatch):
    """The stored error text is not an answer: a rescore neither judges it nor reads it
    as labels, and leaves our failures and the provider's out."""
    judged: list = []

    async def _rescore(session, run_id):
        async def _judge(_db, output, *_args, **_kwargs):
            judged.append(output)
            return {"score": 80.0, "sub_scores": {k: 80 for k in ee.SUB_SCORE_WEIGHTS}, "evaluator_reasoning": "ok"}

        monkeypatch.setattr(ee, "score_result", _judge)
        await rescore_as_started(session, run_id)

    run, results = await run_on_postgres(
        monkeypatch,
        scorer_selection=["judge"],
        answers=_raises_by_owner(),
        family="CLASSIFICATION",
        expected_answers=["a"] * 4,
        then=_rescore,
    )

    assert judged == ["a"]
    assert [r.execution_anomaly_kind for r in results] == [None, None, "platform_error", "transient_error"]
    assert results[1].score == 0
    micro = results[1].execution_details["metric_results"]["classification.micro_prf"]
    assert (micro["value"], micro["detail"]["reason"]) == (0.0, "agent_run_failed")
    assert run.quality_score == pytest.approx(47.22)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answers", "expected", "overall"),
    [
        # Agent X answers the majority label everywhere; agent Y finds the rare ones but
        # misses three `A` (manual test cases 1 and 2).
        (
            ["A"] * 10,
            {"micro_prf": 80.0, "macro_prf": 29.63, "samples_f1": 60.0, "subset_accuracy": 80.0},
            56.54,
        ),
        (
            ["A"] * 5 + ["B"] * 3 + ["B", "C"],
            {"micro_prf": 70.0, "macro_prf": 72.31, "samples_f1": 47.09, "subset_accuracy": 70.0},
            63.13,
        ),
    ],
    ids=["majority_label", "rare_labels"],
)
async def test_classification_metrics_are_computed_over_the_run(monkeypatch, answers, expected, overall):
    """Micro F1 and subset accuracy pool their counts, macro F1 averages each label's F1
    over the run, samples F1 averages its entries: the agent that finds the rare
    labels leads Overall Quality."""
    seen: dict = {}

    async def _figures(session, run_id):
        seen["metrics"] = await _run_metrics(session, run_id)

    run, _ = await run_on_postgres(
        monkeypatch,
        scorer_selection=[],
        answers=answers,
        family="CLASSIFICATION",
        expected_answers=["A"] * 8 + ["B", "C"],
        then=_figures,
    )

    values = {key.removeprefix("classification."): m.value for key, m in seen["metrics"].items()}
    assert {key: values[key] for key in expected} == pytest.approx(expected)
    assert run.quality_score == pytest.approx(overall)


# ── A crash in quality scoring never costs the answer ───────────────


def _crash_quality(monkeypatch) -> None:
    def _raise(*_args, **_kwargs):
        raise RuntimeError("the scorer broke")

    monkeypatch.setattr(ee, "quality_with_extraction", _raise)


def _scoring_errors_logged(caplog) -> list[str]:
    """The run's one error line about entries whose scoring raised, without its ids."""
    return [
        record.getMessage().split(" of run ", 1)[0]
        for record in caplog.records
        if record.levelno == logging.ERROR and record.getMessage().startswith("Quality scoring raised on")
    ]


def _failed_scoring(metrics: dict) -> bool:
    return bool(metrics) and all(
        blob["status"] == "unavailable"
        and blob["detail"]["reason"] == "scoring_error"
        and blob["detail"]["message"] == "Quality scoring raised RuntimeError; see the server log."
        for blob in metrics.values()
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_crash_in_quality_scoring_keeps_the_answer_and_the_judge_verdict(monkeypatch, caplog):
    _crash_quality(monkeypatch)
    with caplog.at_level(logging.ERROR, logger=ee.logger.name):
        run, [result] = await run_on_postgres(
            monkeypatch, scorer_selection=None, answers=["the cat sat"], family="PROSE",
            expected_answers=["the cat sat"],
        )

    assert run.status == "completed", run.error
    assert result.agent_output == "the cat sat"
    assert result.score == 80.0
    assert result.execution_anomaly_kind is None
    assert _failed_scoring(_prose_metrics_of(result))
    assert run.headline_details["quality_coverage"]["left_out"] == {"scoring_error": 1}
    assert _scoring_errors_logged(caplog) == ["Quality scoring raised on 1 entries"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_crash_in_quality_scoring_never_fails_the_rescore(monkeypatch, caplog):
    async def _rescore_with_a_broken_scorer(session, run_id):
        _crash_quality(monkeypatch)
        with caplog.at_level(logging.ERROR, logger=ee.logger.name):
            assert await rescore_as_started(session, run_id) == 1

    run, [result] = await run_on_postgres(
        monkeypatch, scorer_selection=None, answers=["the cat sat"], family="PROSE",
        expected_answers=["the cat sat"], then=_rescore_with_a_broken_scorer,
    )

    assert run.status == "completed", run.error
    assert result.agent_output == "the cat sat"
    assert result.score == 80.0
    assert _failed_scoring(_prose_metrics_of(result))
    assert run.headline_details["quality_coverage"]["left_out"] == {"scoring_error": 1}
    assert _scoring_errors_logged(caplog) == ["Quality scoring raised on 1 entries"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_pipeline_tool_refusal_is_a_platform_error_not_the_agents_answer(monkeypatch):
    """A Skip LLM agent whose Prepare tool refuses returns the tool's error as its text.
    Scored, it was the agent's 0 and could be reused after the configuration was fixed:
    it is a platform error, left out of every headline with the tool's message, its
    extractor and judge never run."""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.core.config import settings
    from app.core.security import encrypt_value
    from app.models.agent import Agent
    from app.models.dataset import Dataset, DatasetEntry, EvaluationDataset
    from app.models.evaluation import Evaluation, EvaluationResult, EvaluationRun
    from app.models.integration import Integration
    from app.models.user import User
    from tests.scratch_database import ALL_TABLES, scratch_database

    # A licence, and no deployment-wide base URL: the integration's own, without a
    # TTP base URL, is what the tool reads.
    monkeypatch.setattr(settings, "enterprise_license", "a-licence")
    monkeypatch.setattr(settings, "filigran_ai_base_url_stix", "")
    monkeypatch.setattr(settings, "filigran_ai_base_url_ttp", "")
    judged: list = []

    async def _judge(_db, output, *_args, **_kwargs):
        judged.append(output)
        return {"score": 80.0, "sub_scores": {k: 80 for k in ee.SUB_SCORE_WEIGHTS}, "evaluator_reasoning": "ok"}

    async def _no_price_rows(_db, _model_id):
        return []

    monkeypatch.setattr(ee, "score_result", _judge)
    monkeypatch.setattr(ee, "_build_run_price_rows", _no_price_rows)

    async with scratch_database("evalrun", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as s:
            owner = User(email="o@x.io", full_name="Owner", hashed_password="x", role="admin")
            s.add(owner)
            await s.flush()
            integration = Integration(
                user_id=owner.id,
                provider="filigran_ai",
                auth_type="none",
                name="Filigran AI",
                status="connected",
                extra_config_enc=encrypt_value(
                    json.dumps({"base_url_stix": "https://stix.example.invalid", "base_url_ttp": ""})
                ),
            )
            s.add(integration)
            await s.flush()
            agent = Agent(
                user_id=owner.id,
                name="TTP extractor",
                slug=f"a-{uuid.uuid4().hex[:6]}",
                skip_llm=True,
                prepare_tool_ref={
                    "source": "integration",
                    "provider": "filigran_ai",
                    "tool_name": "extract_ttp",
                    "integration_id": str(integration.id),
                },
            )
            evaluation = Evaluation(
                user_id=owner.id, name="Eval", scorer_selection=["judge"], family="CLASSIFICATION"
            )
            dataset = Dataset(name="ds")
            s.add_all([agent, evaluation, dataset])
            await s.flush()
            s.add(EvaluationDataset(evaluation_id=evaluation.id, dataset_id=dataset.id))
            s.add(
                DatasetEntry(
                    dataset_id=dataset.id,
                    position=0,
                    input_prompt="The actor sent spearphishing attachments.",
                    expected_answer="T1566.001",
                )
            )
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="model-independent",
                trigger_reason="manual",
                status="pending",
                created_at=datetime.now(UTC),
            )
            s.add(run)
            await s.commit()
            run_id = run.id

        async with db.sessions() as s:
            await ee.execute_evaluation_run(s, str(run_id))

        async with db.sessions() as s:
            stored = await s.get(EvaluationRun, run_id)
            result = (
                await s.execute(select(EvaluationResult).where(EvaluationResult.run_id == run_id))
            ).scalar_one()

    message = "No API base URL configured for extract_ttp. Edit the integration and set the appropriate base URL."
    assert result.execution_anomaly_kind == "platform_error"
    assert result.agent_output == f"Error: {message}"
    assert result.execution_details["agent_run_failure"]["failure_cause"] == "pipeline_tool_failed"
    # Neither scored by the extractor nor judged: each quality metric is left out with
    # the tool's message.
    assert judged == []
    assert {
        (m["status"], m["detail"]["reason"], m["detail"]["message"])
        for m in result.execution_details["metric_results"].values()
    } == {("unavailable", "platform_error", f"Error: {message}")}
    # The only entry did not count: the run measured nothing.
    assert stored.quality_score is None
    assert (stored.status, stored.error) == ("failed", ee.NOTHING_COUNTED_ERROR)
