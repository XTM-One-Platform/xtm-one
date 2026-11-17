"""Judge scoring reliability and the judge's structured-output schema.

The per-evaluation judge output budget, the tolerant verdict extraction, the
single ``is_judge_failure`` classification used instead of any fabricated
fallback score, and the Anthropic normalization of the verdict schema. Judge
failures' exclusion from run aggregates is tested with the anomaly exclusion in
``test_evaluation_framing.py``.
"""

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest

from app.services import evaluation_engine as ee
from app.services.evaluation.framing import (
    ANOMALY_KIND_CONFIRMATION_SEEKING,
    ANOMALY_KIND_TRANSIENT_ERROR,
    EXECUTION_ERROR_REASONING,
)
from app.services.structured_output import build_anthropic_structured_output
from tests.evaluation_helpers import HeadlinesFake, patch_evaluation_run, rescore_as_started, selects_run_status

# ── Judge scoring ─────────────────────────────────────────────────────────────────────


SUB_KEYS = list(ee.SUB_SCORE_WEIGHTS)


def _complete_verdict(value: int = 80) -> dict:
    return {
        "sub_scores": {k: value for k in SUB_KEYS},
        "reasoning": "looks good",
    }


class _StubLLM:
    """Records call kwargs (max_tokens, structured_output, messages) and returns a canned response."""

    def __init__(self, content: str, *, stop_reason: str = "stop"):
        self.content = content
        self.stop_reason = stop_reason
        self.max_tokens_seen: list[int] = []
        self.structured_output_seen: list[object] = []
        self.last_messages: list = []

    async def chat(self, *, messages, model, temperature, max_tokens, **kwargs):
        self.max_tokens_seen.append(max_tokens)
        self.structured_output_seen.append(kwargs.get("structured_output"))
        self.last_messages = messages
        return SimpleNamespace(
            content=self.content,
            stop_reason=self.stop_reason,
            usage={"total_tokens": 10},
        )


class _FakeEntry:
    def __init__(self):
        self.id = uuid.uuid4()
        self.input_prompt = "What is 2 + 2?"
        self.expected_answer = "4"
        self.evaluation_prompt = None


def _patch_judge(monkeypatch, llm):
    async def _mock_get_evaluator_llm(_db, _model, *, user_id=None):
        return llm, "judge-model", "openai"

    async def _mock_load_prompt(_db):
        return "You are a judge."

    monkeypatch.setattr(ee, "_get_evaluator_llm", _mock_get_evaluator_llm)
    monkeypatch.setattr(ee, "_load_default_judge_prompt_from_skill", _mock_load_prompt)
    # Neutralise Langfuse tracing side effects.
    import app.services.langfuse_tracing as lf

    async def _noop_trace(*_a, **_k):
        return None

    monkeypatch.setattr(lf, "trace_llm_call", _noop_trace)
    monkeypatch.setattr(lf, "end_trace", lambda *a, **k: None)
    monkeypatch.setattr(lf, "get_local_trace_id", lambda _c: None)


def _score(monkeypatch, content, *, stop_reason="stop", available_tools=None, budget=8192):
    llm = _StubLLM(content, stop_reason=stop_reason)
    _patch_judge(monkeypatch, llm)
    result = asyncio.run(
        ee.score_result(
            db=object(),
            agent_output="4",
            entry=_FakeEntry(),
            evaluator_model="judge-model",
            available_tools=available_tools,
            judge_max_tokens=budget,
        )
    )
    return result, llm


# ── _JUDGE_VERDICT_SCHEMA: single source of truth ────────────────────────


def test_judge_verdict_schema_is_nested_and_derived_from_weights():
    schema = ee._JUDGE_VERDICT_SCHEMA
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"sub_scores", "reasoning"}
    sub = schema["properties"]["sub_scores"]
    assert sub["type"] == "object"
    # Derived from SUB_SCORE_WEIGHTS — never a hand-maintained key list.
    assert list(sub["properties"]) == SUB_KEYS
    assert set(sub["required"]) == set(SUB_KEYS)
    for key in SUB_KEYS:
        prop = sub["properties"][key]
        assert prop == {"type": "integer", "minimum": 0, "maximum": 100}
    assert schema["properties"]["reasoning"]["type"] == "string"
    # additionalProperties:false at both levels keeps the schema consistent with
    # the "no extra fields" prompt rule and native strict-mode enforcement.
    assert schema["additionalProperties"] is False
    assert sub["additionalProperties"] is False


# ── _parse_judge_response: tolerant extraction ───────────────────────────


def _verdict_with_braces_in_its_reasoning() -> str:
    verdict = _complete_verdict(65)
    verdict["reasoning"] = "used a set literal {1, 2} and the output was incomplete}"
    return f"Here is my verdict:\n{json.dumps(verdict)}\nDone."


@pytest.mark.parametrize(
    ("raw", "expected_sub_scores"),
    [
        pytest.param(json.dumps(_complete_verdict(70)), {k: 70 for k in SUB_KEYS}, id="bare_json"),
        pytest.param(
            f"Here is my assessment:\n{json.dumps(_complete_verdict(60))}\nThanks!", None, id="prose_wrapped"
        ),
        # Free-text braces in the reasoning must not trip the brace-balanced
        # scanner into a false parse failure.
        pytest.param(
            _verdict_with_braces_in_its_reasoning(), {k: 65 for k in SUB_KEYS}, id="prose_wrapped_braces_in_string"
        ),
        pytest.param("```json\n" + json.dumps(_complete_verdict(55)) + "\n```", None, id="fenced"),
        # A trailing comma inside sub_scores is repaired through the shared SAP
        # pipeline and scored, not dropped.
        pytest.param(
            '{"sub_scores": {"tool_selection_quality": 100, '
            '"response_completeness": 90, "response_accuracy": 85, '
            '"instruction_adherence": 95, "coherence": 100,}, '
            '"reasoning": "ok"}',
            {
                "tool_selection_quality": 100,
                "response_completeness": 90,
                "response_accuracy": 85,
                "instruction_adherence": 95,
                "coherence": 100,
            },
            id="malformed_trailing_comma",
        ),
        # An unterminated reasoning string on an otherwise complete verdict is
        # repaired rather than treated as a parse failure.
        pytest.param(
            '{"sub_scores": {"tool_selection_quality": 70, '
            '"response_completeness": 70, "response_accuracy": 70, '
            '"instruction_adherence": 70, "coherence": 70}, '
            '"reasoning": "looks good',
            {k: 70 for k in SUB_KEYS},
            id="unterminated_string",
        ),
    ],
)
def test_a_complete_verdict_is_recovered(raw, expected_sub_scores):
    parsed = ee._parse_judge_response(raw)
    assert parsed["parse_error"] is False
    assert parsed["missing_keys"] == []
    if expected_sub_scores is not None:
        assert {k: parsed["sub_scores"][k] for k in SUB_KEYS} == expected_sub_scores


def test_parse_unparseable_verdict_flags_parse_error():
    parsed = ee._parse_judge_response("the agent did fine, I'd say 8/10")
    assert parsed["parse_error"] is True
    assert set(parsed["missing_keys"]) == set(SUB_KEYS)


def test_parse_partial_verdict_reports_missing_keys():
    partial = _complete_verdict(90)
    del partial["sub_scores"]["coherence"]
    parsed = ee._parse_judge_response(json.dumps(partial))
    assert parsed["parse_error"] is False
    assert parsed["missing_keys"] == ["coherence"]


def test_parse_verdict_without_sub_scores_object_reports_all_missing():
    """An absent nested ``sub_scores`` object → all keys missing, all zero, but
    NOT a parse error (the JSON was valid)."""
    parsed = ee._parse_judge_response('{"reasoning": "solid answer"}')
    assert parsed["parse_error"] is False
    assert set(parsed["missing_keys"]) == set(SUB_KEYS)
    assert all(parsed["sub_scores"][k] == 0 for k in SUB_KEYS)
    assert parsed["reasoning"] == "solid answer"


def test_nested_verdict_yields_same_global_score_as_direct_compute(monkeypatch):
    """The nested verdict's sub-scores feed ``_compute_global_score`` unchanged."""
    verdict = _complete_verdict(77)
    result, _llm = _score(
        monkeypatch, json.dumps(verdict), available_tools=["search"]
    )
    assert result["sub_scores"] == verdict["sub_scores"]
    assert result["score"] == ee._compute_global_score(verdict["sub_scores"])


# ── score_result: budget wiring ──────────────────────────────────────────


def test_score_result_passes_judge_budget_to_llm(monkeypatch):
    _result, llm = _score(monkeypatch, json.dumps(_complete_verdict()), budget=12345)
    assert llm.max_tokens_seen == [12345]


# ── score_result: structured output enforcement ──────────────────────────


def test_native_structured_output_cfg_reaches_llm_and_no_prompt_fallback(monkeypatch):
    """When the provider supports native enforcement, the cfg is passed to
    llm.chat and NO schema text is appended to the prompt (invariant 16)."""
    import app.services.structured_output as so

    cfg = {"native": "response-format"}
    monkeypatch.setattr(so, "build_native_structured_output", lambda _llm, _schema: cfg)

    _result, llm = _score(monkeypatch, json.dumps(_complete_verdict()))

    assert llm.structured_output_seen == [cfg]
    system_content = llm.last_messages[0].content
    assert "You MUST respond with a valid JSON object" not in system_content


def test_no_native_cfg_falls_back_to_prompt_schema(monkeypatch):
    """A provider without native enforcement gets the schema described in the
    prompt and NO structured_output kwarg passed."""
    import app.services.structured_output as so

    monkeypatch.setattr(so, "build_native_structured_output", lambda _llm, _schema: None)

    _result, llm = _score(monkeypatch, json.dumps(_complete_verdict()))

    assert llm.structured_output_seen == [None]
    system_content = llm.last_messages[0].content
    assert "You MUST respond with a valid JSON object" in system_content


def test_structured_output_kwarg_is_stripped_and_retried_once(monkeypatch):
    """A provider whose chat() lacks **kwargs rejects structured_output by
    name; the judge strips it and retries once (mirrors persistent_memory)."""
    import app.services.structured_output as so

    monkeypatch.setattr(
        so, "build_native_structured_output", lambda _llm, _schema: {"native": "x"}
    )

    class _NoKwargsLLM:
        def __init__(self, content):
            self.content = content
            self.attempts = 0
            self.last_messages: list = []

        async def chat(self, *, messages, model, temperature, max_tokens, **kwargs):
            self.attempts += 1
            self.last_messages = messages
            if "structured_output" in kwargs:
                raise TypeError(
                    "chat() got an unexpected keyword argument 'structured_output'"
                )
            return SimpleNamespace(
                content=self.content,
                stop_reason="stop",
                usage={"total_tokens": 10},
            )

    llm = _NoKwargsLLM(json.dumps(_complete_verdict()))
    _patch_judge(monkeypatch, llm)
    result = asyncio.run(
        ee.score_result(
            db=object(),
            agent_output="4",
            entry=_FakeEntry(),
            evaluator_model="judge-model",
            available_tools=None,
            judge_max_tokens=8192,
        )
    )
    # First attempt rejects structured_output; strip + retry succeeds → 2 calls.
    assert llm.attempts == 2
    assert not result["is_judge_failure"]
    # The retry compensates for the lost native enforcement by describing the
    # schema in the prompt (no weaker than the non-native fallback path).
    assert "You MUST respond with a valid JSON object" in llm.last_messages[0].content


# ── score_result: failure classification ─────────────────────────────────


def _partial_verdict() -> str:
    partial = _complete_verdict(90)
    del partial["sub_scores"]["response_accuracy"]
    return json.dumps(partial)


@pytest.mark.parametrize(
    ("content", "stop_reason", "is_failure"),
    [
        pytest.param(json.dumps(_complete_verdict(80)), "stop", False, id="valid"),
        pytest.param(json.dumps(_complete_verdict(80)), "max_tokens", True, id="truncated"),
        pytest.param("no json here, sorry", "stop", True, id="unparseable"),
        pytest.param(_partial_verdict(), "stop", True, id="partial"),
    ],
)
def test_a_verdict_is_a_judge_failure_unless_complete(monkeypatch, content, stop_reason, is_failure):
    result, _ = _score(monkeypatch, content, stop_reason=stop_reason)
    assert result["is_judge_failure"] is is_failure
    if stop_reason == "max_tokens":
        assert result["judge_truncated"] is True
    if not is_failure:
        assert result["score"] > 0


def test_truncated_and_unparseable_verdict_surfaces_budget_hint(monkeypatch):
    # A verdict both truncated and unparseable (the response ran out of budget
    # mid-JSON) must still surface the "raise the judge response budget" hint,
    # not only the parse error.
    result, _ = _score(
        monkeypatch, '{"tool_selection_quality": 8', stop_reason="max_tokens"
    )
    assert result["is_judge_failure"] is True
    assert result["judge_truncated"] is True
    assert "raise the evaluation's judge response budget" in result[
        "evaluator_reasoning"
    ]


def test_toolless_agent_gets_full_tool_score_and_no_failure(monkeypatch):
    # Judge omits tool_selection_quality; agent has no tools, so it must be
    # deterministically filled to 100 and NOT counted as a missing sub-score.
    verdict = _complete_verdict(75)
    del verdict["sub_scores"]["tool_selection_quality"]
    result, _ = _score(monkeypatch, json.dumps(verdict), available_tools=[])
    assert result["is_judge_failure"] is False
    assert result["sub_scores"]["tool_selection_quality"] == 100


def test_toolless_agent_still_fails_on_other_missing_key(monkeypatch):
    verdict = _complete_verdict(75)
    del verdict["sub_scores"]["tool_selection_quality"]  # allowed to be missing (tool-less)
    del verdict["sub_scores"]["coherence"]  # NOT allowed to be missing
    result, _ = _score(monkeypatch, json.dumps(verdict), available_tools=[])
    assert result["is_judge_failure"] is True


def test_an_unknown_tool_list_is_judged_not_graded_100(monkeypatch):
    """An unknown tool list is no tool-less agent: the judge's grade stands."""
    result, llm = _score(monkeypatch, json.dumps(_complete_verdict(40)), available_tools=None)

    assert result["is_judge_failure"] is False
    assert result["sub_scores"]["tool_selection_quality"] == 40
    assert "## AVAILABLE TOOLS" not in llm.last_messages[-1].content


def test_an_unknown_tool_list_does_not_excuse_a_missing_tool_grade(monkeypatch):
    verdict = _complete_verdict(75)
    del verdict["sub_scores"]["tool_selection_quality"]

    result, _ = _score(monkeypatch, json.dumps(verdict), available_tools=None)

    assert result["is_judge_failure"] is True


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        pytest.param({"execution_mode": "full_loop", "available_tools": ["search", "fetch"]}, ["search", "fetch"], id="stored"),
        pytest.param({"execution_mode": "full_loop", "available_tools": []}, [], id="stored_empty"),
        pytest.param({"execution_mode": "llm_only"}, [], id="llm_only_before_5003"),
        pytest.param({"execution_mode": "llm_only_fallback"}, [], id="fallback_before_5003"),
        pytest.param({"execution_mode": "full_loop"}, None, id="full_loop_before_5003"),
        pytest.param({"execution_mode": "skip_llm"}, None, id="skip_llm_before_5003"),
        pytest.param({"available_tools": ["search", "", 3]}, ["search"], id="non_names_dropped"),
        pytest.param({"available_tools": "search"}, None, id="not_a_list"),
        pytest.param(None, None, id="no_details"),
    ],
)
def test_recorded_available_tools(details, expected):
    assert ee._recorded_available_tools(details) == expected


# ── Aggregate fixture ────────────────────────────────────────────────


class _AggFakeSession:
    def __init__(self, eval_row, runs, results):
        self._eval_row = eval_row
        self._runs = runs
        self._results = results
        self._calls = 0
        self.commits = 0

    async def get(self, _model, _id):
        return self._eval_row

    async def execute(self, _statement, _params=None):
        self._calls += 1
        if self._calls == 1:
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: self._runs)
            )
        if self._calls == 2:
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: self._results)
            )
        # Later calls are the headline aggregation (advisory lock, performance
        # basis reload, per-run metric reads); one permissive shape serves them
        # all. No family metrics are recorded, so both headlines are N/A and the
        # judge aggregates must hold on their own.
        return SimpleNamespace(
            all=lambda: [],
            scalars=lambda: SimpleNamespace(all=lambda: self._runs),
        )

    async def commit(self):
        self.commits += 1


def _make_result(
    run_id, entry_id, *, score, judge_failure=False, duration=1.0, anomaly_kind=None
):
    return SimpleNamespace(
        run_id=run_id,
        entry_id=entry_id,
        score=score,
        f1_score=None,
        duration_seconds=duration,
        speed_score=None,
        overall_score=None,
        execution_anomaly_kind=anomaly_kind,
        is_judge_failure=judge_failure,
        execution_details=None,
    )


# ── Transient execution-error anomalies ─────────────────────────────
# A transient LLM-provider failure surfaced as the agent output must be excluded
# from run aggregates like a confirmation-seeking anomaly, must self-heal on
# rescore, and must never hide genuine (well-formed-but-wrong) quality.


_TRANSIENT_OUTPUT = (
    "Error: Error code: 529 - {'type': 'error', 'error': "
    "{'type': 'overloaded_error', 'message': 'Overloaded'}}"
)


def test_rescore_self_heals_transient_error_row(monkeypatch):
    # A stored transient-error result flagged False flips to an anomaly on
    # rescore, is zeroed, skips the judge, and is excluded.
    run_id = uuid.uuid4()
    # As the rescore endpoints leave it.
    run = SimpleNamespace(
        id=run_id, evaluation_id=uuid.uuid4(), agent_id=uuid.uuid4(), status="running"
    )
    evaluation = SimpleNamespace(
        evaluator_model="judge-model",
        evaluator_prompt=None,
        evaluation_instructions=None,
        sub_score_weights=None,
        default_output_schema=None,
        scorer_selection=["judge"],
        judge_max_tokens=8192,
    )
    row = SimpleNamespace(
        run_id=run_id,
        agent_output=_TRANSIENT_OUTPUT,
        execution_anomaly_kind=None,
        is_judge_failure=True,  # stale flag from a prior scoring must be cleared
        score=0,
        sub_scores=None,
        evaluator_reasoning=None,
        execution_details={"execution_mode": "full_loop"},
    )

    class _RescoreDB:
        async def get(self, model, _id):
            return run if model is ee.EvaluationRun else evaluation

        async def execute(self, _statement):
            if selects_run_status(_statement):
                return SimpleNamespace(scalar_one_or_none=lambda: "running")
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [row]),
                # No extractor configured for this agent.
                scalar_one_or_none=lambda: None,
            )

        async def commit(self):
            return None

    # Judge must NOT be invoked for the self-healed anomaly.
    def _boom(*_a, **_k):
        raise AssertionError("judge must not run for a transient-error anomaly")

    monkeypatch.setattr(ee, "_compute_run_headlines", HeadlinesFake())
    monkeypatch.setattr(ee, "score_result", _boom)

    n = asyncio.run(ee.rescore_run(_RescoreDB(), str(run_id), previous_status="completed"))

    assert n == 1
    assert row.execution_anomaly_kind == ANOMALY_KIND_TRANSIENT_ERROR
    assert row.score == 0
    assert row.evaluator_reasoning == EXECUTION_ERROR_REASONING
    assert row.sub_scores == {k: 0 for k in ee.SUB_SCORE_WEIGHTS}
    assert row.is_judge_failure is False  # cleared on conversion to anomaly


def _rescore_fixture(execution_details: dict):
    """A run with one ordinary stored prediction the judge will re-score."""
    run_id = uuid.uuid4()
    # As the rescore endpoints leave it.
    run = SimpleNamespace(id=run_id, evaluation_id=uuid.uuid4(), agent_id=uuid.uuid4(), status="running")
    evaluation = SimpleNamespace(
        evaluator_model="judge-model",
        evaluator_prompt=None,
        evaluation_instructions=None,
        sub_score_weights=None,
        default_output_schema=None,
        scorer_selection=["judge"],
        judge_max_tokens=8192,
    )
    # The entry it was scored against: one deleted since keeps its verdict.
    entry = SimpleNamespace(
        id=uuid.uuid4(), dataset_id=None, input_prompt="q", expected_answer="Paris", evaluation_prompt=None
    )
    row = SimpleNamespace(
        id=uuid.uuid4(),
        run_id=run_id,
        entry_id=entry.id,
        version_entry_id=None,
        agent_output="Paris.",
        execution_anomaly_kind=None,
        is_judge_failure=False,
        score=0,
        sub_scores=None,
        evaluator_reasoning=None,
        f1_score=None,
        execution_details=execution_details,
    )

    class _RescoreDB:
        async def get(self, model, _id):
            if model is ee.DatasetEntry:
                return entry
            return run if model is ee.EvaluationRun else evaluation

        async def execute(self, _statement):
            if selects_run_status(_statement):
                return SimpleNamespace(scalar_one_or_none=lambda: "running")
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [row]),
                scalar_one_or_none=lambda: None,
            )

        # The real metric logger runs against this fixture, so it needs somewhere
        # to put the rows it writes.
        def add(self, obj):
            written.append(obj)

        async def commit(self):
            return None

    written: list = []
    return run_id, row, _RescoreDB(), written


def test_rescore_measures_its_own_judge_time(monkeypatch):
    """The judge re-runs, so its duration is re-measured, never carried forward
    from the original ``execution_details``."""
    stale = 987_654
    run_id, row, db, written = _rescore_fixture(
        {"execution_mode": "full_loop", "judge_duration_ms": stale}
    )

    async def _judge(*_a, **_k):
        return {"score": 80, "sub_scores": {}, "evaluator_reasoning": "ok"}

    monkeypatch.setattr(ee, "_compute_run_headlines", HeadlinesFake())
    monkeypatch.setattr(ee, "score_result", _judge)

    asyncio.run(ee.rescore_run(db, str(run_id), previous_status="completed"))

    measured = row.execution_details["judge_duration_ms"]
    assert measured != stale
    assert measured >= 0
    # And it reaches the metric rows the panel reads, not just the blob.
    logged = {m.key: m.value for m in written}
    assert logged["judge_seconds"] == round(measured / 1000, 3)
    # Reused: Run Duration is the judge alone, never the stale agent+judge sum.
    assert logged["run_duration_seconds"] == logged["judge_seconds"]


def test_rescore_judges_on_the_owners_providers(monkeypatch):
    """A re-score's judge runs as the evaluation's owner, as the run's did."""
    run_id, _row, db, _written = _rescore_fixture({"execution_mode": "full_loop"})
    owner = uuid.uuid4()
    evaluation = asyncio.run(db.get(None, None))
    evaluation.user_id = owner
    seen: dict = {}

    async def _judge(*_a, **kwargs):
        seen["user_id"] = kwargs.get("user_id")
        return {"score": 80, "sub_scores": {}, "evaluator_reasoning": "ok"}

    monkeypatch.setattr(ee, "_compute_run_headlines", HeadlinesFake())
    monkeypatch.setattr(ee, "score_result", _judge)

    asyncio.run(ee.rescore_run(db, str(run_id), previous_status="completed"))

    assert seen["user_id"] == str(owner)


_SEARCH_CALL = {"type": "tool", "tool_name": "search", "duration_ms": 5, "description": "search"}


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        # The tools the agent had, not the one it called.
        pytest.param(
            {"execution_mode": "full_loop", "available_tools": ["search", "fetch"], "traces": [_SEARCH_CALL]},
            ["search", "fetch"],
            id="stored_list",
        ),
        # Called none of its tools: judged against them, not graded 100 for having none.
        pytest.param(
            {"execution_mode": "full_loop", "available_tools": ["search"], "traces": []},
            ["search"],
            id="no_call",
        ),
        # Recorded before the list was: unknown, never the calls passed off as the list.
        pytest.param(
            {"execution_mode": "full_loop", "traces": [_SEARCH_CALL]},
            None,
            id="before_5003",
        ),
    ],
)
def test_rescore_judges_against_the_tools_the_agent_had(monkeypatch, details, expected):
    """A rescore gives the judge the stored tool list, as the fresh pass did."""
    run_id, row, db, _written = _rescore_fixture(details)
    seen: dict = {}

    async def _judge(*_a, **kwargs):
        seen["available_tools"] = kwargs.get("available_tools")
        return {"score": 80, "sub_scores": {}, "evaluator_reasoning": "ok"}

    monkeypatch.setattr(ee, "_compute_run_headlines", HeadlinesFake())
    monkeypatch.setattr(ee, "score_result", _judge)

    asyncio.run(ee.rescore_run(db, str(run_id), previous_status="completed"))

    assert seen["available_tools"] == expected
    # Kept for the next rescore.
    assert row.execution_details.get("available_tools") == details.get("available_tools")


def test_a_deselected_judge_logs_no_duration_from_an_earlier_run(monkeypatch):
    """A rescore that does not judge writes no duration at all.

    The stored blob still carries the previous pass's figure; falling back to it
    would mint a Judge Time and a Run Duration nothing measured. Rows already
    stored stay as they are: they still describe the pass behind the score shown.
    """
    run_id, row, db, written = _rescore_fixture(
        {"execution_mode": "full_loop", "judge_duration_ms": 987_654}
    )

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(ee, "_compute_run_headlines", HeadlinesFake())
    monkeypatch.setattr(ee, "score_result", _noop)
    # Judge deselected for this rescore.
    monkeypatch.setattr(ee, "judge_enabled", lambda _sel: False)

    asyncio.run(ee.rescore_run(db, str(run_id), previous_status="completed"))

    assert {m.key for m in written} & {"judge_seconds", "run_duration_seconds"} == set()
    # The stored figure is not erased -- it is still the last real measurement.
    assert row.execution_details["judge_duration_ms"] == 987_654


def test_rescore_logs_metrics_even_without_a_quality_config(monkeypatch):
    """The metric rows are where Judge Time is read from.

    Logging happens even for an evaluation with no quality family.
    ``is_reused`` is true: the agent did not re-run.
    """
    run_id, _row, db, _written = _rescore_fixture({"execution_mode": "full_loop"})
    calls: list[bool] = []

    async def _judge(*_a, **_k):
        return {"score": 80, "sub_scores": {}, "evaluator_reasoning": "ok"}

    async def _record(_db, *, result_id, run_id, execution_details, is_reused=False, replace=False):
        calls.append(is_reused)

    from app.services.evaluation import metrics as run_metrics

    monkeypatch.setattr(ee, "_compute_run_headlines", HeadlinesFake())
    monkeypatch.setattr(ee, "score_result", _judge)
    monkeypatch.setattr(run_metrics, "log_result_metrics", _record)

    asyncio.run(ee.rescore_run(db, str(run_id), previous_status="completed"))

    assert calls == [True]


# ── The entry-level speed / overall blend is retired ─────────────────────
#
# Overall Performance replaces `speed_score` and `overall_score`. These tests
# assert that nothing writes them and that historical values survive untouched:
# the stale-looking columns are the only record of how past runs were scored.


def _agg_setup(results):
    eval_id = uuid.uuid4()
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
    eval_row = SimpleNamespace(
        penalty_factor=0.5,
        performance_tolerance=5.0,
        overall_quality_metrics=None,
        overall_performance_metrics=None,
        family=None,
    )
    for r in results:
        r.run_id = run.id
    db = _AggFakeSession(eval_row, [run], results)
    asyncio.run(ee._compute_run_headlines(db, eval_id, [str(run.id)]))
    return run


def test_a_historical_speed_score_is_never_cleared():
    """A historical speed score is retained, never backfilled and never cleared,
    even on a zero-duration row."""
    entry = uuid.uuid4()
    historical = _make_result(None, entry, score=90, duration=0.0)
    historical.speed_score = 50.0
    historical.overall_score = 62.0
    fresh = _make_result(None, entry, score=90, duration=1.0)
    run = _agg_setup([historical, fresh])

    assert historical.speed_score == 50.0
    assert historical.overall_score == 62.0
    # A fresh entry is not backfilled.
    assert fresh.speed_score is None
    assert fresh.overall_score is None
    # The run-level columns are not recomputed from them either.
    assert run.speed_score is None
    assert run.overall_score is None


def test_the_judge_aggregate_still_works_without_the_retired_blend():
    """The LLM Judge headline does not depend on the retired blend."""
    entry = uuid.uuid4()
    good = _make_result(None, entry, score=90, duration=1.0)
    run = _agg_setup([good])

    assert run.score == 90
    assert run.anomaly_count == 0


# ── Fresh-scoring path: judge runs for confirmation-seeking, stays skipped for
# transient-error ───────────────────────────────────────────────────


class _FreshScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FreshEntriesResult:
    def __init__(self, entries):
        self._entries = entries

    def scalars(self):
        return self

    def all(self):
        return self._entries


class _FreshCountResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


def _is_out_of_band_run_query(statement) -> bool:
    """True for ``execute_evaluation_run``'s per-entry cancellation check.

    These fakes answer ``execute()`` positionally from a scripted queue, so the
    probe is answered out of band ("not cancelled") and never consumes the queue.
    The cooperative stop itself is covered against a real database in
    ``test_evaluations.py`` (``cancel_run``) and ``test_evaluation_lease.py`` (the
    cancel event, the emergency stop, the real sequential loop).
    """
    columns = list(getattr(statement, "selected_columns", None) or [])
    return (
        len(columns) == 1
        and columns[0].name in ("status", "id")
        and getattr(columns[0].table, "name", None) == "evaluation_runs"
    )


class _NotCancelledResult:
    def scalar_one_or_none(self):
        # A live run reads ``running``; no row at all is a deleted one.
        return "running"


class _FreshSession:
    def __init__(self, execute_results):
        self._execute_results = list(execute_results)
        self.commits = 0
        self.added = []

    async def execute(self, _statement):
        if _is_out_of_band_run_query(_statement):
            return _NotCancelledResult()
        if not self._execute_results:
            raise AssertionError("Unexpected extra execute() call")
        return self._execute_results.pop(0)

    async def scalar(self, _statement):
        # No General Assistant row: the platform default iteration count.
        return None

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass

    async def refresh(self, _obj):
        return None

    def add(self, obj):
        self.added.append(obj)


def test_fresh_scoring_judges_confirmation_seeking_and_skips_transient_error(
    monkeypatch,
):
    """Confirmation-seeking is judged and counted; transient-error stays
    zeroed with the judge skipped."""
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
        error=None,
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
        integration_bindings={},
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

    entry_confirm = SimpleNamespace(
        id=uuid.uuid4(),
        input_prompt="Question 1",
        expected_answer="Answer 1",
        file_ids=None,
        evaluation_prompt=None,
        position=1,
    )
    entry_transient = SimpleNamespace(
        id=uuid.uuid4(),
        input_prompt="Question 2",
        expected_answer="Answer 2",
        file_ids=None,
        evaluation_prompt=None,
        position=2,
    )
    entries = [
        (entry_confirm, None, None, None),
        (entry_transient, None, None, None),
    ]

    db = _FreshSession(
        [
            _FreshScalarResult(run),
            _FreshScalarResult(agent),
            # No extractor configured for this agent.
            _FreshScalarResult(None),
            _FreshScalarResult(evaluation),
            _FreshEntriesResult(entries),
            _FreshCountResult(2),
            _FreshCountResult(2),
        ]
    )

    confirm_output = "Which output format would you like, A) JSON or B) YAML?"
    transient_output = _TRANSIENT_OUTPUT

    async def _mock_execute_full_loop(
        _db, _agent, _model_id, input_prompt, _file_refs, evaluation=None, **kwargs
    ):
        if input_prompt == "Question 1":
            return confirm_output, [], None, [], []
        return transient_output, [], None, [], []

    score_result_calls = []

    async def _mock_score_result(_db, agent_output, *_args, **_kwargs):
        score_result_calls.append(agent_output)
        return {
            "score": 42.0,
            "sub_scores": {k: 42 for k in ee.SUB_SCORE_WEIGHTS},
            "evaluator_reasoning": "judged normally",
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

    results_by_output = {
        r.agent_output: r for r in db.added if isinstance(r, ee.EvaluationResult)
    }
    confirm_result = results_by_output[confirm_output]
    transient_result = results_by_output[transient_output]

    # Confirmation-seeking: judge ran for real (score/sub_scores come from the
    # judge, not a forced zero); the persisted subtype says so.
    assert confirm_result.execution_anomaly_kind == ANOMALY_KIND_CONFIRMATION_SEEKING
    assert confirm_result.score == 42.0
    assert confirm_result.sub_scores == {k: 42 for k in ee.SUB_SCORE_WEIGHTS}
    assert confirm_output in score_result_calls

    # Transient-error: judge skipped entirely, score forced to zero, subtype
    # persisted.
    assert transient_result.execution_anomaly_kind == ANOMALY_KIND_TRANSIENT_ERROR
    assert transient_result.score == 0
    assert transient_result.sub_scores == {k: 0 for k in ee.SUB_SCORE_WEIGHTS}
    assert transient_output not in score_result_calls

def test_a_cancelled_rescore_stops_judging_and_keeps_nothing(monkeypatch):
    """Cancelling a bulk rescore stops the judge and keeps nothing.

    A bulk rescore stages one execution for every selected run, so ``cancel_run``
    leaves it running for the siblings; ``rescore_run`` must probe for the
    cancellation itself, or the judge keeps being called, and billed, for every
    remaining result. It rolls back rather than committing: ``rescore_run`` holds
    one transaction across every result, so a commit would persist a half-judged
    rescore.

    Removing the probe fails this test: both results are judged.
    """
    run_id = uuid.uuid4()
    # As the rescore endpoints leave it.
    run = SimpleNamespace(id=run_id, evaluation_id=uuid.uuid4(), agent_id=uuid.uuid4(), status="running")
    evaluation = SimpleNamespace(
        evaluator_model="judge-model",
        evaluator_prompt=None,
        evaluation_instructions=None,
        sub_score_weights=None,
        default_output_schema=None,
        scorer_selection=["judge"],
        judge_max_tokens=8192,
    )

    # The entry they were scored against: one deleted since keeps its verdict.
    entry = SimpleNamespace(
        id=uuid.uuid4(), dataset_id=None, input_prompt="q", expected_answer="Paris", evaluation_prompt=None
    )

    def _row():
        return SimpleNamespace(
            id=uuid.uuid4(),
            run_id=run_id,
            entry_id=entry.id,
            version_entry_id=None,
            f1_score=None,
            agent_output="Paris.",
            execution_anomaly_kind=None,
            is_judge_failure=False,
            score=0,
            sub_scores=None,
            evaluator_reasoning=None,
            execution_details={"execution_mode": "full_loop"},
        )

    rows = [_row(), _row()]

    class _CancelMidRescoreDB:
        def __init__(self):
            self.probes = 0
            self.committed = False
            self.rolled_back = False
            self.added = []

        async def get(self, model, _id):
            if model is ee.DatasetEntry:
                return entry
            return run if model is ee.EvaluationRun else evaluation

        async def execute(self, statement):
            if _is_out_of_band_run_query(statement):
                # The user cancels while the FIRST result is being re-judged, so
                # the second probe is the one that sees it.
                self.probes += 1
                status = "cancelled" if self.probes > 1 else "running"
                return SimpleNamespace(scalar_one_or_none=lambda: status)
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: rows),
                scalar_one_or_none=lambda: None,
            )

        # Every result reaches the metric logger, judge or no judge,
        # so the stub accepts the rows it writes; the rollback below
        # discards them.
        def add(self, obj):
            self.added.append(obj)

        async def commit(self):
            self.committed = True

        async def rollback(self):
            self.rolled_back = True

    judged = []

    async def _judge(*_a, **_k):
        judged.append(1)
        return {"score": 80, "sub_scores": {}, "evaluator_reasoning": "ok"}

    monkeypatch.setattr(ee, "_compute_run_headlines", HeadlinesFake())
    monkeypatch.setattr(ee, "score_result", _judge)

    db = _CancelMidRescoreDB()
    n = asyncio.run(ee.rescore_run(db, str(run_id), previous_status="completed"))

    assert len(judged) == 1, (
        f"the judge ran {len(judged)} time(s) for {len(rows)} results -- a "
        "cancelled rescore must stop calling it, which is the billing this "
        "issue is about"
    )
    assert db.rolled_back, (
        "the partial rescore was not rolled back, so some results carry new "
        "scores and the rest the old ones"
    )
    assert not db.committed, "a cancelled rescore must not commit its partial work"
    assert n == 0, f"nothing was persisted, so the count must be 0, got {n}"


# ── Anthropic structured-output schema normalization ──────────────────────────────────
# Anthropic's ``output_config.format.schema`` rejects numeric/length validation
# keywords that OpenAI strict mode accepts (e.g. integer ``minimum``/``maximum``).
# ``_normalize_for_anthropic`` must strip them so schemas that carry those bounds
# (such as the evaluation judge verdict schema) don't 400 on Anthropic.


@pytest.mark.parametrize(
    ("schema", "path", "expected"),
    [
        ({"type": "integer", "minimum": 0, "maximum": 100}, (), {"type": "integer"}),
        (
            {
                "type": "object",
                "properties": {
                    "sub_scores": {
                        "type": "object",
                        "properties": {
                            "coherence": {"type": "integer", "minimum": 0, "maximum": 100},
                        },
                        "required": ["coherence"],
                        "additionalProperties": False,
                    },
                    "reasoning": {"type": "string"},
                },
                "required": ["sub_scores", "reasoning"],
                "additionalProperties": False,
            },
            ("properties", "sub_scores", "properties", "coherence"),
            {"type": "integer"},
        ),
        (
            {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 5}},
            ("items",),
            {"type": "integer"},
        ),
        (
            {"type": "object", "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 10}}},
            ("properties", "name"),
            {"type": "string"},
        ),
        (
            {"type": "object", "properties": {"step": {"type": "number", "multipleOf": 0.5}}},
            ("properties", "step"),
            {"type": "number"},
        ),
    ],
    ids=["integer_bounds_top_level", "nested_object_property", "array_items", "string_length", "multiple_of"],
)
def test_unsupported_keywords_are_stripped_at_every_depth(schema, path, expected):
    node = build_anthropic_structured_output(schema)["schema"]
    for step in path:
        node = node[step]
    assert node == expected


def test_preserves_supported_keywords():
    schema = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["a", "b"], "description": "d"},
            "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        },
        "required": ["kind"],
    }
    out = build_anthropic_structured_output(schema)["schema"]
    kind = out["properties"]["kind"]
    assert kind["enum"] == ["a", "b"]
    assert kind["description"] == "d"
    # minItems (0 or 1) is API-enforced by Anthropic and must survive.
    assert out["properties"]["tags"]["minItems"] == 1
    assert out["additionalProperties"] is False


def test_does_not_mutate_input_schema():
    schema = {"type": "integer", "minimum": 0, "maximum": 100}
    build_anthropic_structured_output(schema)
    assert schema == {"type": "integer", "minimum": 0, "maximum": 100}


# ── The judge reads the same inputs on every path ─────────────────────────────


def _recording_judge(calls: list[dict]):
    """The judge, stubbed: what it was asked to score, and a fixed verdict."""

    async def _judge(_db, agent_output, entry, _evaluator_model, **kwargs):
        calls.append(
            {
                "answer": agent_output,
                "input": entry.input_prompt,
                "expected": entry.expected_answer,
                "entry_prompt": entry.evaluation_prompt,
                "instructions": kwargs.get("evaluation_instructions"),
                "available_tools": kwargs.get("available_tools"),
                "tool_calls": [t.get("tool_name") for t in kwargs.get("tool_calls") or []],
            }
        )
        return {
            "score": 80.0,
            "sub_scores": {k: 80 for k in ee.SUB_SCORE_WEIGHTS},
            "evaluator_reasoning": "ok",
            "is_judge_failure": False,
        }

    return _judge


async def _admin(session):
    from app.models.user import User

    admin = User(email=f"judge-{uuid.uuid4().hex[:6]}@x.io", full_name="Admin", hashed_password="x", role="admin")
    session.add(admin)
    await session.commit()
    return admin


async def _replay_judge(session, run_id):
    from sqlalchemy import select

    from app.api.v1 import evaluations as api
    from app.api.v1.schemas.evaluation import ReplayJudgeRequest
    from app.models.evaluation import EvaluationResult

    result = (await session.execute(select(EvaluationResult).where(EvaluationResult.run_id == run_id))).scalar_one()
    await api.replay_judge_result(
        str(run_id), str(result.id), ReplayJudgeRequest(), user=await _admin(session), db=session, _ai_gate=None
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("file_only", [False, True], ids=["prompt", "file-only"])
async def test_a_stored_answer_is_judged_on_the_same_inputs_fresh_rescored_and_replayed(monkeypatch, file_only):
    """The dataset's instructions, and the prompt a file-only entry is sent, reach the judge
    on every path, not only the fresh one."""
    from app.services.evaluation.framing import fallback_input_prompt
    from tests.evaluation_helpers import run_on_postgres

    calls: list[dict] = []

    async def _rescore_then_replay(session, run_id):
        await rescore_as_started(session, run_id)
        await _replay_judge(session, run_id)

    await run_on_postgres(
        monkeypatch,
        scorer_selection=None,
        answers=["Paris"],
        expected_answers=["Paris"],
        dataset_instructions="Accept the city name alone.",
        file_only=file_only,
        judge=_recording_judge(calls),
        then=_rescore_then_replay,
    )

    fresh, rescored, replayed = calls
    assert fresh["instructions"] == "Accept the city name alone."
    assert fresh["input"] == (fallback_input_prompt(True) if file_only else "Question 0")
    assert rescored == fresh
    assert replayed == fresh


async def _rerun_reusing(session, run_id):
    """A second run of the same evaluation, reusing the first run's answers."""
    from sqlalchemy import select

    from app.models.evaluation import Evaluation, EvaluationResult, EvaluationRun

    first = await session.get(EvaluationRun, run_id)
    evaluation = await session.get(Evaluation, first.evaluation_id)
    evaluation.reuse_default = "on"
    second = EvaluationRun(
        evaluation_id=evaluation.id, agent_id=first.agent_id, model_id=first.model_id, trigger_reason="manual", status="pending"
    )
    session.add(second)
    await session.commit()
    await ee.execute_evaluation_run(session, str(second.id))
    reused = (await session.execute(select(EvaluationResult).where(EvaluationResult.run_id == second.id))).scalar_one()
    assert reused.reused_from_result_id is not None, "setup: the second run did not reuse the first's answer"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_reused_answer_is_judged_on_the_tool_calls_its_source_made(monkeypatch):
    """A source whose tool calls live only in its trace's spans keeps them when reused."""
    from tests.evaluation_helpers import run_on_postgres

    calls: list[dict] = []

    await run_on_postgres(
        monkeypatch,
        scorer_selection=None,
        answers=["Paris"],
        tool_spans=["search"],
        judge=_recording_judge(calls),
        then=_rerun_reusing,
    )

    fresh, reused = calls
    assert fresh["tool_calls"] == ["search"]
    assert reused == fresh


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_rescored_answer_whose_entry_was_deleted_keeps_its_verdict(monkeypatch):
    """No question left to judge against: the stored verdict stands, as a replay refuses."""
    from sqlalchemy import select

    from app.models.dataset import DatasetEntry
    from app.models.evaluation import EvaluationResult
    from tests.evaluation_helpers import run_on_postgres

    calls: list[dict] = []

    scores: list[float] = []

    async def _delete_then_rescore(session, run_id):
        result = (await session.execute(select(EvaluationResult).where(EvaluationResult.run_id == run_id))).scalar_one()
        await session.delete(await session.get(DatasetEntry, result.entry_id))
        await session.commit()
        await rescore_as_started(session, run_id)
        await session.refresh(result)
        scores.append(result.score)

    await run_on_postgres(
        monkeypatch, scorer_selection=None, answers=["Paris"], judge=_recording_judge(calls), then=_delete_then_rescore
    )

    assert len(calls) == 1, "the rescore judged an answer against an entry that no longer exists"
    assert scores == [80.0]


def _numbered_judge(calls: list[dict]):
    """The recording judge, each verdict naming the call that gave it."""
    record = _recording_judge(calls)

    async def _judge(*args, **kwargs):
        verdict = await record(*args, **kwargs)
        return {**verdict, "judge_raw_response": f"verdict {len(calls)}"}

    return _judge


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_rescore_and_a_replay_record_the_judge_pass_they_ran(monkeypatch):
    """Judge Time and the timeline's judge step describe the verdict on display, after a
    rescore and after a replay of a run first scored with the judge off."""
    from sqlalchemy import select

    from app.models.evaluation import EvaluationResult
    from app.models.evaluation_metric import EvaluationMetric
    from app.services.evaluation.metrics import JUDGE_SECONDS_KEY
    from tests.evaluation_helpers import run_on_postgres

    calls: list[dict] = []
    seen: list[dict] = []

    async def _snapshot(session, run_id):
        result = (await session.execute(select(EvaluationResult).where(EvaluationResult.run_id == run_id))).scalar_one()
        await session.refresh(result)
        keys = await session.execute(select(EvaluationMetric.key).where(EvaluationMetric.result_id == result.id))
        details = result.execution_details or {}
        seen.append(
            {
                "judge_ms": "judge_duration_ms" in details,
                "steps": [(t.get("type"), t.get("raw_response")) for t in details.get("traces") or [] if "judge" in t.get("type", "")],
                "judge_seconds": JUDGE_SECONDS_KEY in set(keys.scalars()),
            }
        )

    async def _replay_then_rescore(session, run_id):
        await _snapshot(session, run_id)
        await _replay_judge(session, run_id)
        await _snapshot(session, run_id)
        # The judge turned on for the evaluation, then a rescore (there is no
        # per-rescore override).
        run = await session.get(ee.EvaluationRun, run_id)
        evaluation = await session.get(ee.Evaluation, run.evaluation_id)
        evaluation.scorer_selection = ["judge"]
        await session.flush()
        await rescore_as_started(session, run_id)
        await _snapshot(session, run_id)

    await run_on_postgres(
        monkeypatch, scorer_selection=[], answers=["Paris"], judge=_numbered_judge(calls), then=_replay_then_rescore
    )

    judge_off, replayed, rescored = seen
    assert judge_off == {"judge_ms": False, "steps": [("judge_skipped", None)], "judge_seconds": False}
    assert replayed == {"judge_ms": True, "steps": [("judge", "verdict 1")], "judge_seconds": True}
    assert rescored["steps"] == [("judge", "verdict 2")]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scorer_selection", "answer"),
    [(None, "Paris"), ([], "Paris"), (None, _TRANSIENT_OUTPUT)],
    ids=["judged", "judge-off", "transient-error"],
)
async def test_a_resumed_callback_entry_is_scored_as_a_fresh_one(monkeypatch, scorer_selection, answer):
    """A pinned run's entry resumed from a callback: the judge reads what it reads for the
    same entry run fresh, or is skipped as there, and the result holds the same snapshot,
    quality metrics, performance metrics, judge step and Judge Time."""
    from sqlalchemy import select

    from app.models.evaluation_metric import EvaluationMetric
    from tests.evaluation_helpers import run_on_postgres

    outcomes: dict[str, tuple] = {}
    for mode in ("fresh", "callback"):
        calls: list[dict] = []
        keys: list[dict[str, float]] = []

        async def _metric_keys(session, run_id, keys=keys):
            rows = await session.execute(
                select(EvaluationMetric.key, EvaluationMetric.value).where(EvaluationMetric.run_id == run_id)
            )
            keys.append(dict(rows.tuples().all()))

        _, [result] = await run_on_postgres(
            monkeypatch,
            scorer_selection=scorer_selection,
            answers=[answer],
            expected_answers=["Paris"],
            family="PROSE",
            pinned=True,
            tool_spans=["search"],
            dataset_instructions="Accept the city name alone.",
            judge=_recording_judge(calls),
            await_callback=mode == "callback",
            then=_metric_keys,
        )
        outcomes[mode] = (calls, keys[0], result)

    fresh_calls, fresh_keys, fresh = outcomes["fresh"]
    resumed_calls, resumed_keys, resumed = outcomes["callback"]

    def _quality(result):
        return {k: v for k, v in result.execution_details["metric_results"].items() if not k.startswith("performance.")}

    def _judge_steps(result):
        return [(t["type"], t.get("description")) for t in result.execution_details["traces"] if "judge" in t["type"]]

    assert [call["expected"] for call in fresh_calls] in ([], ["Paris"]), "setup: the fresh run read no snapshot"
    assert resumed_calls == fresh_calls
    assert _judge_steps(resumed) == _judge_steps(fresh)
    assert resumed.execution_anomaly_kind == fresh.execution_anomaly_kind
    assert resumed.version_entry_id is not None
    assert set(resumed_keys) == set(fresh_keys)
    assert _quality(resumed) == _quality(fresh)
    assert set(resumed.execution_details) == set(fresh.execution_details)
    assert (resumed.score, resumed.is_judge_failure) == (fresh.score, fresh.is_judge_failure)
    # Its agent ran for this answer: Run Duration is agent plus judge, as fresh.
    assert resumed_keys["run_duration_seconds"] >= 1.5


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_callback_resumed_after_its_snapshot_was_deleted_is_judged_on_the_live_entry(monkeypatch):
    """The callback can come hours later: a snapshot deleted meanwhile does not fail the
    resume, whose answer is judged on the live entry, as a rescore falls back; and the
    episode keeps no copy of the first turn once its result holds it."""
    from sqlalchemy import delete, select

    from app.models.dataset_version import DatasetVersionEntry
    from app.models.evaluation import EvaluationEpisode
    from tests.evaluation_helpers import run_on_postgres

    calls: list[dict] = []
    records: list = []

    async def _delete_the_snapshot(session, _run_id):
        await session.execute(delete(DatasetVersionEntry))

    async def _episode_records(session, run_id):
        rows = await session.execute(select(EvaluationEpisode.first_turn_details).where(EvaluationEpisode.run_id == run_id))
        records.extend(rows.scalars())

    _, [resumed] = await run_on_postgres(
        monkeypatch,
        scorer_selection=None,
        answers=["Paris"],
        expected_answers=["Paris"],
        pinned=True,
        await_callback=True,
        before_resume=_delete_the_snapshot,
        judge=_recording_judge(calls),
        then=_episode_records,
    )

    assert resumed.version_entry_id is None
    assert [call["expected"] for call in calls] == ["the live answer, edited later"]
    assert records == [None]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_replayed_judge_reads_the_snapshot_a_pinned_run_was_scored_on(monkeypatch):
    """Editing the live entry afterwards never changes what a past run is judged on."""
    from sqlalchemy import select

    from app.models.dataset import DatasetEntry
    from app.models.dataset_version import DatasetVersion, DatasetVersionEntry
    from app.models.evaluation import EvaluationResult
    from tests.evaluation_helpers import run_on_postgres

    calls: list[dict] = []

    async def _pin_edit_and_replay(session, run_id):
        result = (await session.execute(select(EvaluationResult).where(EvaluationResult.run_id == run_id))).scalar_one()
        live = await session.get(DatasetEntry, result.entry_id)
        version = DatasetVersion(dataset_id=live.dataset_id, version_number=1, content_hash="h", entry_count=1)
        session.add(version)
        await session.flush()
        snapshot = DatasetVersionEntry(
            version_id=version.id, entry_id=live.id, position=0, input_prompt=live.input_prompt, expected_answer="the snapshot answer"
        )
        session.add(snapshot)
        await session.flush()
        result.version_entry_id = snapshot.id
        live.expected_answer = "the live answer, edited later"
        await session.commit()
        calls.clear()
        await _replay_judge(session, run_id)

    await run_on_postgres(
        monkeypatch,
        scorer_selection=None,
        answers=["the snapshot answer"],
        judge=_recording_judge(calls),
        then=_pin_edit_and_replay,
    )

    assert [call["expected"] for call in calls] == ["the snapshot answer"]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(("scorer_selection", "recorded"), [(None, True), ([], False)], ids=["judge-on", "judge-off"])
async def test_judge_time_is_recorded_only_when_the_judge_ran(monkeypatch, scorer_selection, recorded):
    from sqlalchemy import select

    from app.models.evaluation_metric import EvaluationMetric
    from app.services.evaluation.metrics import JUDGE_SECONDS_KEY
    from tests.evaluation_helpers import run_on_postgres

    keys: list[set[str]] = []

    async def _metric_keys(session, run_id):
        rows = await session.execute(select(EvaluationMetric.key).where(EvaluationMetric.run_id == run_id))
        keys.append(set(rows.scalars()))

    _, [result] = await run_on_postgres(
        monkeypatch, scorer_selection=scorer_selection, answers=["Paris"], then=_metric_keys
    )

    assert ("judge_duration_ms" in result.execution_details) is recorded
    assert (JUDGE_SECONDS_KEY in keys[0]) is recorded


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(("scorer_selection", "averages"), [(None, 80.0), ([], None)], ids=["judge-on", "judge-off"])
async def test_sub_score_averages_count_the_entries_the_judge_score_counts(monkeypatch, scorer_selection, averages):
    """A transient provider error is out of the judge score, so out of its averages; a
    run scored with the judge off has none."""
    from app.api.v1 import evaluations as api
    from tests.evaluation_helpers import run_on_postgres

    responses: list[dict] = []

    async def _response(session, run_id):
        from app.models.evaluation import EvaluationRun

        run = await session.get(EvaluationRun, run_id)
        responses.append(await api._build_run_response(session, run, missing_snapshots=[]))

    await run_on_postgres(
        monkeypatch, scorer_selection=scorer_selection, answers=["Paris", _TRANSIENT_OUTPUT], then=_response
    )

    sub_scores = responses[0]["avg_sub_scores"]
    if averages is None:
        assert not sub_scores
    else:
        assert set(sub_scores.values()) == {averages}


def test_the_judge_prompt_describes_every_section_the_judge_receives() -> None:
    """A tripwire: a section the judge is sent but its instructions never name is graded
    by guesswork."""
    import re

    from tests.evaluation_helpers import BACKEND_DIR

    prompt = (BACKEND_DIR / "app/agents/seed_data/skill_content/evaluation-judge-system-prompt.md").read_text(encoding="utf-8")
    sent = ee._build_judge_user_prompt(
        "input", "output", "expected", evaluation_prompt="instructions", available_tools=["t"], tool_calls=[{"tool_name": "t"}]
    )
    # Named in the prompt's numbered list of inputs, not merely somewhere in its text.
    listed = re.findall(r"^\d+\. (.+)$", prompt, flags=re.M)
    for section in re.findall(r"^## (.+)$", sent, flags=re.M):
        assert any(section in line for line in listed), section
