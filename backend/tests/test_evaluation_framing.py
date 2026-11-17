"""How an evaluation frames an agent's turn, runs it, and flags what came back.

The anomaly heuristics and their exclusion from run aggregates, the autonomous
framing directive and fallback wording (``evaluation/framing.py``), the "always
ask" tools an evaluated agent carries, and the ``skip_llm`` fan-out collapse.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import evaluation_engine
from app.services import evaluation_engine as ee
from app.services.evaluation import always_ask
from app.services.evaluation.constants import MODEL_INDEPENDENT
from app.services.evaluation.fanout import (
    evaluation_has_runs,
    resolve_effective_models,
)
from app.services.evaluation.framing import (
    ANOMALY_KIND_CONFIRMATION_SEEKING,
    ANOMALY_KIND_TRANSIENT_ERROR,
    ANOMALY_REASONING,
    AUTONOMOUS_EVAL_DIRECTIVE,
    EXECUTION_ERROR_REASONING,
    FILE_ONLY_FALLBACK_PROMPT,
    NO_INPUT_FALLBACK_PROMPT,
    classify_execution_anomaly,
    detect_confirmation_seeking,
    detect_execution_error,
    fallback_input_prompt,
)

# ── Anomaly detection and aggregate exclusion ─────────────────────────────────────────
# The ``detect_confirmation_seeking`` and transient-error heuristics and the
# classifier that picks between them, exclusion of flagged results and judge
# failures from run aggregates, the judge-skipped trace, and ``anomaly_count``.


# ── Detector: positive cases (confirmation / option requests) ───────


@pytest.mark.parametrize(
    "text",
    [
        "Should I proceed with the extraction?",
        "Which format do you prefer for the output?",
        "I can do this two ways. Would you like A) JSON or B) YAML?",
        "Here are the options:\nA) full bundle\nB) summary\nC) both\nWhich option would you like?",
        "Do you want me to include the indicators?",
    ],
)
def test_detects_confirmation_seeking(text):
    assert detect_confirmation_seeking(text) is True


# ── Detector: negative cases (real deliverables) ────────────────────


@pytest.mark.parametrize(
    "text",
    [
        '{"type": "bundle", "objects": [{"type": "indicator"}]}',
        "The report is ready above. It contains 3 indicators and 1 malware object.",
        "Done. I extracted the STIX bundle and validated it against the schema.",
        # A deliverable followed by a courtesy sentence.
        "The bundle is ready above. Let me know if you need anything else.",
        # An A)/B)/C) list that does not ask the user to choose: a flag skips the judge.
        "The malware exhibits three behaviors: A) persistence B) exfiltration C) evasion.",
        "",
        "   \n\t  ",
        None,
    ],
)
def test_ignores_real_deliverables(text):
    assert detect_confirmation_seeking(text) is False


# ── Aggregation exclusion + anomaly_count ───────────────────────────


class _ScalarsResult:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return self

    def all(self):
        return self._values


class _AllResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _EmptyRowsResult:
    """Permissive result for the headline reads.

    Serves the metric-row reads (``.all()``) and the evaluation-wide performance
    basis reload (``.scalars().all()``) with one shape, so the fixture does not
    have to predict the order in which they are issued.
    """

    def __init__(self, runs=None):
        self._runs = runs or []

    def all(self):
        return []

    def scalars(self):
        return _AllResult(self._runs)


class _FakeAggSession:
    """Minimal async session for ``_compute_run_headlines``."""

    def __init__(self, evaluation, runs, results):
        self._evaluation = evaluation
        self._runs = runs
        self._execute_queue = [_ScalarsResult(runs), _ScalarsResult(results)]
        self.commits = 0

    async def get(self, _model, _pk):
        return self._evaluation

    async def execute(self, _query, _params=None):
        if self._execute_queue:
            return self._execute_queue.pop(0)
        # The advisory lock, the performance basis reload and the per-run
        # metric reads that follow the run and result loads. No family metrics
        # are recorded, so both headlines are N/A and the exclusion rules are
        # asserted on the judge aggregate.
        return _EmptyRowsResult(self._runs)

    async def commit(self):
        self.commits += 1


def _entry(score, f1=None, *, kind=None, judge_failure=False, duration=1.0):
    return dict(
        score=score,
        f1_score=f1,
        execution_anomaly_kind=kind,
        is_judge_failure=judge_failure,
        duration_seconds=duration,
    )


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        pytest.param(
            # A transient-error anomaly (fast, zero score/f1) never reaches the
            # aggregates; it is counted apart. Speed, overall and the retired F1 are
            # not written: the entries' F1 would make it 70.
            [
                _entry(90, 80, duration=2.0),
                _entry(70, 60, duration=4.0),
                _entry(0, 0, kind=ANOMALY_KIND_TRANSIENT_ERROR, duration=0.01),
            ],
            {"anomaly_count": 1, "score": 80.0, "speed_score": None, "overall_score": None, "f1_score": None},
            id="transient_error_excluded",
        ),
        pytest.param(
            # A confirmation-seeking entry counts toward the aggregates like any
            # other entry, and is still counted as an anomaly.
            [
                _entry(90, 80, duration=2.0),
                _entry(20, 10, kind=ANOMALY_KIND_CONFIRMATION_SEEKING),
            ],
            {"anomaly_count": 1, "score": 55.0},
            id="confirmation_seeking_counted",
        ),
        pytest.param(
            # A judge failure is excluded from the judge aggregate, not scored 0.
            [_entry(90), _entry(0, judge_failure=True, duration=2.0)],
            {"anomaly_count": 0, "score": 90.0},
            id="judge_failure_excluded",
        ),
        pytest.param(
            # Nothing gradeable: the judge headline is N/A, never a fabricated 0.
            [_entry(0, judge_failure=True), _entry(0, judge_failure=True)],
            {"anomaly_count": 0, "score": None},
            id="all_judge_failures",
        ),
        pytest.param(
            [
                _entry(0, kind=ANOMALY_KIND_TRANSIENT_ERROR),
                _entry(0, kind=ANOMALY_KIND_TRANSIENT_ERROR),
            ],
            {"anomaly_count": 2, "score": None},
            id="all_transient_errors",
        ),
    ],
)
@pytest.mark.asyncio
async def test_excluded_entries_never_reach_the_run_aggregates(entries, expected):
    evaluation = SimpleNamespace(
        penalty_factor=0.0,
        performance_tolerance=5.0,
        overall_quality_metrics=None,
        overall_performance_metrics=None,
        family=None,
    )
    # Every field starts unset, so each expected value is one the pass wrote.
    run = SimpleNamespace(
        id=uuid.uuid4(),
        score=None,
        speed_score=None,
        overall_score=None,
        quality_score=None,
        headline_details=None,
        f1_score=None,
        anomaly_count=None,
    )
    results = [
        SimpleNamespace(
            run_id=run.id, entry_id=uuid.uuid4(), speed_score=None, overall_score=None, execution_details=None, **entry
        )
        for entry in entries
    ]

    await ee._compute_run_headlines(
        _FakeAggSession(evaluation, [run], results), uuid.uuid4(), batch_run_ids=[str(run.id)]
    )

    for field, value in expected.items():
        if value is None:
            assert getattr(run, field) is None, field
        else:
            assert getattr(run, field) == pytest.approx(value), field


def test_build_judge_trace_when_judge_ran():
    score_data = {
        "judge_model": "gpt-4o",
        "judge_provider": "openai",
        "judge_usage": {"total_tokens": 42},
        "judge_system_prompt": "sys",
        "judge_user_prompt": "usr",
        "judge_raw_response": "raw",
    }
    trace = ee._build_judge_trace(score_data, 120, "fallback-model", judge_ran=True)
    assert trace["type"] == "judge"
    assert trace["model"] == "gpt-4o"
    assert trace["usage"] == {"total_tokens": 42}
    assert trace["user_prompt"] == "usr"
    assert "skipped" not in trace


def test_build_judge_trace_when_skipped_for_anomaly():
    # A skipped judge is recorded as a ``judge_skipped`` marker, never as a
    # ``judge`` trace with empty prompts/usage.
    reason = "Judge skipped: execution anomaly (agent sought confirmation instead of delivering)"
    trace = ee._build_judge_trace({}, 0, "some-model", judge_ran=False, skip_reason=reason)
    assert trace["type"] == "judge_skipped"
    assert trace["skipped"] is True
    assert trace["description"] == reason
    # No fabricated judge fields.
    assert "system_prompt" not in trace
    assert "user_prompt" not in trace
    assert "usage" not in trace
    assert "model" not in trace


def test_build_judge_trace_skipped_default_reason():
    trace = ee._build_judge_trace({}, 5, None, judge_ran=False)
    assert trace["type"] == "judge_skipped"
    assert trace["description"] == "Judge scoring skipped"


# ── anomaly_count survives an entry that fails ──────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_entry_that_fails_after_an_anomaly_leaves_the_count_intact(monkeypatch):
    """A failed entry's rollback expires every result stored so far.

    The run's ``anomaly_count`` is summed from flags kept in memory: read off
    the expired results, it would lazy-load in the async session, raise
    ``MissingGreenlet`` and fail the whole run. The entry fails while its
    episode is finalised, inside the transaction the run lock opened: an
    agent failure lands while no transaction is open (the connection is
    released around the agent call), so its rollback expires nothing. The
    headline pass is stubbed out: it recounts ``anomaly_count`` from the
    database and would hide the loop's count. The failure is ours, so the entry is
    itself a platform-error anomaly and counts too.
    """
    from app.services.evaluation_continuation import ConversationContinuationService
    from tests.evaluation_helpers import HeadlinesFake, run_on_postgres

    failing_answer = "The bundle is ready above."
    finalize_episode = ConversationContinuationService.finalize_episode

    async def _finalize_fails_for_one_answer(db, *, episode, final_output, **kwargs):
        if final_output == failing_answer:
            raise RuntimeError("the episode could not be finalised")
        await finalize_episode(db, episode=episode, final_output=final_output, **kwargs)

    monkeypatch.setattr(
        ConversationContinuationService, "finalize_episode", staticmethod(_finalize_fails_for_one_answer)
    )
    monkeypatch.setattr(ee, "_compute_run_headlines", HeadlinesFake())

    run, results = await run_on_postgres(
        monkeypatch,
        scorer_selection=[],
        answers=["Which option would you like, A or B?", failing_answer],
    )

    assert run.status == "completed", run.error
    assert run.anomaly_count == 2
    assert [r.execution_anomaly_kind for r in results] == ["confirmation_seeking", "platform_error"]
    assert results[1].agent_output.startswith("Error:")


# ── Transient execution-error detector ──────────────────────────────
# A transient LLM provider failure surfaced as the agent OUTPUT (e.g. an
# Anthropic overload / HTTP 529) must be recognised so it can be excluded from
# aggregates like a confirmation-seeking anomaly, without excluding genuine
# agent failures or answers that merely mention "error"/"overloaded".

@pytest.mark.parametrize(
    "text",
    [
        # The exact evidence shape observed from an overloaded provider.
        "Error: Error code: 529 - {'type': 'error', 'error': "
        "{'type': 'overloaded_error', 'message': 'Overloaded'}}",
        "Error: Error code: 429 - rate limit exceeded",
        "Error: Error code: 502 - bad gateway",
        "Error: Error code: 503 - service unavailable",
        "Error: Error code: 504 - gateway timeout",
        "Error: the provider is overloaded, please retry",
        "Error: connection error while calling the model",
        "Error: the request timed out",
    ],
)
def test_detects_transient_execution_error(text):
    assert detect_execution_error(text) is True


@pytest.mark.parametrize(
    "text",
    [
        # Valid deliverables that merely mention error/overloaded vocabulary.
        "The report analyses 3 systems that were overloaded last week.",
        "Done. The pipeline recovered gracefully from a transient error.",
        '{"type": "bundle", "summary": "rate limit policy documented"}',
        # Documentation / analysis that merely discusses error codes mid-text
        # must not be treated as error framing.
        "The API returns Error code: 500 when the upstream is down; handle it "
        "by retrying with backoff, as documented in section 3.",
        # Non-transient error markers must still be judged, not excluded.
        "Context overflow: the conversation exceeded the model context window.",
        "Error: Error code: 400 - invalid request (bad schema)",
        # A 4+ digit code must not be read as a transient 3-digit status.
        "Error: Error code: 5290 - unknown provider status",
        "Error: url is required (validation).",
        # Empty / whitespace / None.
        "",
        "   \n\t ",
        None,
    ],
)
def test_ignores_non_transient_or_valid_output(text):
    assert detect_execution_error(text) is False


# ── Shared classifier: gate, precedence, reasons ────────────────────

_TRANSIENT = "Error: Error code: 529 - overloaded_error"
_CONFIRM = "Which output format would you like, A) JSON or B) YAML?"


@pytest.mark.parametrize("exec_mode", ["full_loop", "llm_only", "llm_only_fallback"])
def test_classify_flags_transient_error_on_llm_modes(exec_mode):
    subtype, reason = classify_execution_anomaly(_TRANSIENT, exec_mode)
    assert subtype == ANOMALY_KIND_TRANSIENT_ERROR
    assert reason == EXECUTION_ERROR_REASONING


def test_classify_gates_out_skip_llm():
    # Deterministic non-LLM outputs are never transient-error anomalies.
    assert classify_execution_anomaly(_TRANSIENT, "skip_llm") == (None, None)


def test_classify_still_flags_confirmation_seeking():
    subtype, reason = classify_execution_anomaly(_CONFIRM, "full_loop")
    assert subtype == ANOMALY_KIND_CONFIRMATION_SEEKING
    assert reason == ANOMALY_REASONING
    # Each subtype carries its own reason, so the entry says which one it is.
    assert EXECUTION_ERROR_REASONING != ANOMALY_REASONING


def test_classify_transient_error_takes_precedence_when_both():
    # An output that is BOTH a transient error AND confirmation-seeking is a
    # single anomaly reported with the (more actionable) transient subtype/reason.
    both = "Error: Error code: 529 overloaded. Should I retry?"
    subtype, reason = classify_execution_anomaly(both, "full_loop")
    assert subtype == ANOMALY_KIND_TRANSIENT_ERROR
    assert reason == EXECUTION_ERROR_REASONING


@pytest.mark.parametrize(
    "text",
    [
        "The STIX bundle is ready above with 3 indicators.",
        # A well-formed but wrong answer is judged normally, not excluded.
        "Paris is the capital of Germany.",
        "The answer is 5.",
        # A non-transient error marker is still judged.
        "Context overflow: the conversation exceeded the model context window.",
    ],
)
def test_classify_passes_valid_output(text):
    assert classify_execution_anomaly(text, "full_loop") == (None, None)


# ── Autonomous framing and non-interactive fallback wording ───────────────────────────
# ``evaluation/framing.py`` is the single source of the prompt text used by both
# LLM-driven evaluation execution paths. Which prompt an entry hands the agent is
# tested through the engine in ``test_evaluation_engine.py``.


# ── autonomous directive ────────────────────────────────────────────


def test_directive_is_short_single_sentence():
    # One sentence at most: no system prompt inflation (invariant 16).
    assert AUTONOMOUS_EVAL_DIRECTIVE.count(".") <= 1
    assert len(AUTONOMOUS_EVAL_DIRECTIVE) < 300


class _RecordingLLM:
    """A provider that streams one answer and records the messages it was sent."""

    provider_name = "recording"
    last_stop_reason = "stop"

    def __init__(self):
        self.messages = None

    async def chat_stream(self, messages=None, **_kwargs):
        self.messages = messages
        yield "The answer."


@pytest.mark.asyncio
async def test_an_llm_only_evaluation_tells_the_model_no_human_will_answer(monkeypatch):
    from app.services import langfuse_tracing

    llm = _RecordingLLM()

    async def _provider(_db, _model_id, **_kwargs):
        return llm, None

    async def _trace(*_args, **_kwargs):
        return {"trace": None}

    monkeypatch.setattr(evaluation_engine, "_get_llm_provider_and_model", _provider)
    monkeypatch.setattr(langfuse_tracing, "trace_llm_call", _trace)
    monkeypatch.setattr(langfuse_tracing, "end_trace", lambda *_a, **_k: None)
    monkeypatch.setattr(langfuse_tracing, "get_local_trace_id", lambda *_a, **_k: None)
    agent = SimpleNamespace(id=uuid.uuid4(), name="Analyst", slug="analyst", persona="You analyse.", temperature=None, output_format=None, output_schema=None)

    await evaluation_engine._execute_agent_llm_only(None, agent, "m", "Summarise Q3")

    system = llm.messages[0]
    assert system.role == "system"
    assert system.content == f"You analyse.\n\n{AUTONOMOUS_EVAL_DIRECTIVE}"


@pytest.mark.asyncio
@pytest.mark.parametrize("evaluation_mode", [True, False], ids=["evaluated_turn", "chat_turn"])
async def test_only_an_evaluated_agent_turn_carries_the_directive(evaluation_mode):
    """Chat and assignment prompts are never touched: no prompt inflation (invariant 16)."""
    from tests.test_agent_chat_evaluation import _ADA, _system_prompt

    prompt = await _system_prompt(_ADA, evaluation_mode=evaluation_mode)

    assert (AUTONOMOUS_EVAL_DIRECTIVE in prompt) is evaluation_mode


# ── non-interactive fallback wording ────────────────────────────────


@pytest.mark.parametrize(
    ("has_files", "expected"),
    [(True, FILE_ONLY_FALLBACK_PROMPT), (False, NO_INPUT_FALLBACK_PROMPT)],
    ids=["with_files", "without_files"],
)
def test_the_fallback_prompt_depends_on_file_presence(has_files, expected):
    assert fallback_input_prompt(has_files) == expected
    assert FILE_ONLY_FALLBACK_PROMPT != NO_INPUT_FALLBACK_PROMPT


# ── Tools marked "always ask" that an evaluated agent carries ─────────────────────────
# An evaluation with the tool approval setting off runs those tools without
# approval, so its configuration warns, naming them. An agent keeps no list of its
# tools - a turn builds it - so the names are read from the same columns the turn
# reads, and matched by exact name against the marks, as the gate matches them.


def _agent(**fields) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(),
        name="Analyst",
        builtin_tool_ids=[],
        custom_tool_ids=[],
        mcp_server_ids=[],
        mcp_allowed_tools=None,
        integration_bindings={},
        integration_template_bindings=[],
        integration_allowed_tools=None,
        tool_deny_list=None,
        knowledge_base_ids=[],
        knowledge_base_permissions=None,
        memory_persistent_enabled=False,
        handover_agent_ids=[],
        skill_ids=[],
    )
    return SimpleNamespace(**{**base, **fields})


def test_an_agents_names_come_from_every_source_it_is_configured_with():
    server_id = uuid.uuid4()
    agent = _agent(
        builtin_tool_ids=["web_fetch"],
        custom_tool_ids=["ct"],
        mcp_server_ids=[str(server_id)],
        knowledge_base_ids=["kb"],
        memory_persistent_enabled=True,
        handover_agent_ids=["peer"],
        skill_ids=["skill"],
    )
    names = always_ask.agent_tool_names(
        agent,
        custom_tool_names=["Address search"],
        mcp_servers=[SimpleNamespace(id=server_id, discovered_tools=[{"name": "lookup"}, {"name": "write"}])],
        provider_tool_names={},
    )

    assert {"web_fetch", "Address search", "lookup", "write", "search_knowledge", "search_memory",
            "consult_agent", "load_skill"} <= names
    # ``web_fetch`` is a [DEPRECATED] alias of ``web``: a stored former
    # id names what it granted, not the whole group (``browser`` came with
    # the ``browser`` id).
    assert "browser" not in names


def test_allow_lists_and_the_deny_list_narrow_the_names():
    server_id = uuid.uuid4()
    agent = _agent(
        mcp_server_ids=[str(server_id)],
        mcp_allowed_tools={str(server_id): ["lookup"]},
        integration_bindings={"github": ["integration-id"]},
        integration_allowed_tools={"github": ["search_github_issues"]},
        tool_deny_list=["lookup"],
    )
    names = always_ask.agent_tool_names(
        agent,
        custom_tool_names=[],
        mcp_servers=[SimpleNamespace(id=server_id, discovered_tools=[{"name": "lookup"}, {"name": "write"}])],
        provider_tool_names={"github": ["search_github_issues", "create_github_issue"]},
    )

    assert "search_github_issues" in names
    assert not {"lookup", "write", "create_github_issue"} & names


@pytest.mark.asyncio
async def test_only_marked_tools_are_reported_and_an_agent_carrying_none_is_left_out():
    marked = _agent(name="Marked", builtin_tool_ids=["web_fetch"])
    clean = _agent(name="Clean", builtin_tool_ids=["web_fetch"])
    names = {marked.id: {"web_fetch", "search_knowledge"}, clean.id: {"web_fetch"}}

    async def _names(_db, agent):
        return names[agent.id]

    with (
        patch.object(always_ask, "load_agent_tool_names", new=_names),
        patch("app.services.hitl_whitelist.resolve_vetoed", new=AsyncMock(return_value=frozenset({"search_knowledge"}))),
    ):
        reported = await always_ask.always_ask_tools(object(), [marked, clean])  # type: ignore[arg-type]

    assert reported == [{"agent_id": str(marked.id), "agent_name": "Marked", "tools": ["search_knowledge"]}]


@pytest.mark.asyncio
async def test_matching_is_by_exact_name_as_the_gate_does():
    agent = _agent()

    async def _names(_db, _agent):
        return {"Search_Knowledge", "search_knowledge_docs"}

    with (
        patch.object(always_ask, "load_agent_tool_names", new=_names),
        patch("app.services.hitl_whitelist.resolve_vetoed", new=AsyncMock(return_value=frozenset({"search_knowledge"}))),
    ):
        assert await always_ask.always_ask_tools(object(), [agent]) == []  # type: ignore[arg-type]


def test_an_allow_list_that_is_not_a_list_narrows_nothing():
    """A value that is not a list reads as no allow list: the warning errs toward warning."""
    server_id = uuid.uuid4()
    agent = _agent(
        mcp_server_ids=[str(server_id)],
        mcp_allowed_tools={str(server_id): {}},
        integration_bindings={"github": ["integration-id"]},
        integration_allowed_tools={"github": "search_github_issues"},
    )
    names = always_ask.agent_tool_names(
        agent,
        custom_tool_names=[],
        mcp_servers=[SimpleNamespace(id=server_id, discovered_tools=[{"name": "lookup"}])],
        provider_tool_names={"github": ["search_github_issues", "create_github_issue"]},
    )

    assert {"lookup", "search_github_issues", "create_github_issue"} <= names


def test_a_providers_tool_names_come_from_its_builder_and_an_unknown_provider_is_left_out():
    def _builder(*_args, **_kwargs):
        return [SimpleNamespace(name="search_github_issues")]

    always_ask._one_provider_tool_names.cache_clear()
    try:
        with patch.dict("app.services.integration_tools.PROVIDER_TOOLS", {"github": _builder}):
            first = always_ask._provider_tool_names(["github", "unknown-provider"])
            second = always_ask._provider_tool_names(["github"])
    finally:
        always_ask._one_provider_tool_names.cache_clear()

    assert first == second == {"github": ["search_github_issues"]}


@pytest.mark.asyncio
async def test_the_suite_index_is_loaded_off_the_event_loop_first():
    """Its first build takes seconds in a cold process: never on the event loop."""
    order: list = []

    async def _ensure():
        order.append("ensure")

    def _suite_tools(suite):
        order.append("suite")
        return ()

    with (
        patch("app.tools.builtin_suites.ensure_suite_index_loaded", new=_ensure),
        patch("app.tools.builtin_suites.builtin_suite_tools", new=_suite_tools),
    ):
        await always_ask.load_agent_tool_names(object(), _agent(builtin_tool_ids=["web_fetch"]))  # type: ignore[arg-type]

    assert order == ["ensure", "suite"]


# ── ``skip_llm`` fan-out collapse ─────────────────────────────────────────────────────
# A ``skip_llm`` agent's output does not depend on the chat model, so it runs once
# per evaluation instead of once per model. The manual trigger
# (``api/v1/evaluations.py``) and the autonomous trigger (``services/autonomous.py``)
# share these helpers.


SKIP = "skip-llm-agent"
NORMAL = "regular-agent"
_MODELS = ["gpt-4o", "claude-3-5-sonnet", "gemini-1.5-pro"]


@pytest.mark.parametrize(
    ("agent", "skip_llm_agents", "model_list", "default_model", "expected"),
    [
        # A skip_llm agent yields one sentinel run and no default attribution,
        # however many chat models were selected, or none at all.
        (SKIP, {SKIP}, _MODELS, "gpt-4o", ([MODEL_INDEPENDENT], None)),
        (SKIP, {SKIP}, [], None, ([MODEL_INDEPENDENT], None)),
        # A regular agent keeps its per-model fan-out and default, alone or beside
        # a skip_llm agent.
        (NORMAL, set(), _MODELS[:2], "gpt-4o", (_MODELS[:2], "gpt-4o")),
        (NORMAL, {SKIP}, _MODELS[:2], "claude-3-5-sonnet", (_MODELS[:2], "claude-3-5-sonnet")),
    ],
    ids=["skip_llm_many_models", "skip_llm_no_model", "regular_agent", "regular_agent_in_mixed_set"],
)
def test_resolve_effective_models(agent, skip_llm_agents, model_list, default_model, expected):
    assert (
        resolve_effective_models(
            agent,
            skip_llm_agent_ids=skip_llm_agents,
            model_list=model_list,
            default_model_id=default_model,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("agents", "skip_llm_agents", "agent_model_map", "expected"),
    [
        # Only skip_llm agents and no chat model: still runs, never "No models
        # could be resolved".
        ([SKIP, "other-skip"], {SKIP, "other-skip"}, {SKIP: (None, []), "other-skip": (None, [])}, True),
        ([NORMAL], set(), {NORMAL: (None, [])}, False),
        ([NORMAL], set(), {NORMAL: ("gpt-4o", ["gpt-4o"])}, True),
        ([SKIP, NORMAL], {SKIP}, {SKIP: (None, []), NORMAL: (None, [])}, True),
        ([], set(), {}, False),
    ],
    ids=[
        "only_skip_llm_no_model",
        "regular_agent_no_model",
        "regular_agent_with_model",
        "mixed_set_no_model",
        "no_agent",
    ],
)
def test_evaluation_has_runs(agents, skip_llm_agents, agent_model_map, expected):
    assert (
        evaluation_has_runs(
            agent_ids=agents,
            skip_llm_agent_ids=skip_llm_agents,
            agent_model_map=agent_model_map,
        )
        is expected
    )
