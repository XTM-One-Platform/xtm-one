from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.services.builtin_tools.evaluations as evaluations_module
from app.tools.base import ToolCall

pytestmark = pytest.mark.usefixtures("evaluation_enabled")  # evaluation is off by default

_ALL_TOOL_NAMES = {
    "list_evaluations",
    "get_evaluation",
    "list_evaluation_runs",
    "get_evaluation_run_values",
    "trigger_evaluation",
    "get_placebo_comparison",
    "compare_evaluation_runs",
    "get_agent_feedback",
}


class _FakeSessionCtx:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False


class _FakeResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._items))


class _FakeDBSession:
    def __init__(self, items=()):
        self._items = list(items)

    async def execute(self, _query):
        return _FakeResult(self._items)


def _fake_async_session_factory(db):
    def _factory():
        return _FakeSessionCtx(db)

    return _factory


def test_evaluation_tools_expose_all_definitions():
    defs, _exec = evaluations_module.build_evaluation_tools(
        db=object(),  # type: ignore[arg-type]
        user_id=str(uuid.uuid4()),
    )
    names = {d.name for d in defs}
    assert names == _ALL_TOOL_NAMES


def test_evaluation_tool_schemas_have_bounded_limit_defaults():
    defs, _exec = evaluations_module.build_evaluation_tools(
        db=object(),  # type: ignore[arg-type]
        user_id=str(uuid.uuid4()),
    )
    by_name = {d.name: d for d in defs}

    for name in ("list_evaluations", "list_evaluation_runs"):
        limit = by_name[name].input_schema["properties"]["limit"]
        assert limit["default"] == evaluations_module._DEFAULT_LIMIT

    result_limit = by_name["get_evaluation_run_values"].input_schema["properties"][
        "result_limit"
    ]
    assert result_limit["default"] == evaluations_module._DEFAULT_RESULT_LIMIT

    assert by_name["get_evaluation"].input_schema["required"] == ["evaluation_id"]
    assert by_name["list_evaluation_runs"].input_schema["required"] == ["evaluation_id"]
    assert by_name["get_evaluation_run_values"].input_schema["required"] == ["run_id"]
    assert by_name["trigger_evaluation"].input_schema["required"] == ["evaluation_id"]
    assert by_name["compare_evaluation_runs"].input_schema["required"] == [
        "baseline_run_id",
        "candidate_run_id",
    ]
    assert by_name["get_agent_feedback"].input_schema["required"] == ["agent_id"]
    feedback_limit = by_name["get_agent_feedback"].input_schema["properties"]["limit"]
    assert feedback_limit["default"] == evaluations_module._DEFAULT_FEEDBACK_LIMIT


def test_safe_limit_clamps_to_bounds():
    assert evaluations_module._safe_limit(None, default=20, max_value=100) == 20
    assert evaluations_module._safe_limit(0, default=20, max_value=100) == 1
    assert evaluations_module._safe_limit(-5, default=20, max_value=100) == 1
    assert evaluations_module._safe_limit(999, default=20, max_value=100) == 100
    assert evaluations_module._safe_limit(42, default=20, max_value=100) == 42


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("list_evaluations", {}),
        ("get_evaluation", {"evaluation_id": str(uuid.uuid4())}),
        ("list_evaluation_runs", {"evaluation_id": str(uuid.uuid4())}),
        ("get_evaluation_run_values", {"run_id": str(uuid.uuid4())}),
        ("trigger_evaluation", {"evaluation_id": str(uuid.uuid4())}),
        (
            "compare_evaluation_runs",
            {"baseline_run_id": str(uuid.uuid4()), "candidate_run_id": str(uuid.uuid4())},
        ),
    ],
)
async def test_evaluation_tools_reject_anonymous_users(tool_name, arguments):
    _defs, executor = evaluations_module.build_evaluation_tools(
        db=object(),  # type: ignore[arg-type]
        user_id="anonymous",
    )
    result = await executor.execute_tool(
        ToolCall(id="1", name=tool_name, arguments=arguments)
    )
    assert result.is_error is True
    assert "requires an authenticated admin user" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("list_evaluations", {}),
        ("get_evaluation", {"evaluation_id": str(uuid.uuid4())}),
        ("list_evaluation_runs", {"evaluation_id": str(uuid.uuid4())}),
        ("get_evaluation_run_values", {"run_id": str(uuid.uuid4())}),
        ("trigger_evaluation", {"evaluation_id": str(uuid.uuid4())}),
        (
            "compare_evaluation_runs",
            {"baseline_run_id": str(uuid.uuid4()), "candidate_run_id": str(uuid.uuid4())},
        ),
    ],
)
async def test_evaluation_tools_reject_non_admin_users(
    monkeypatch, tool_name, arguments
):
    monkeypatch.setattr(
        evaluations_module,
        "async_session",
        _fake_async_session_factory(_FakeDBSession()),
    )

    async def _fake_load_user(_db, _uid):
        return SimpleNamespace(id=uuid.uuid4(), is_admin=False), []

    monkeypatch.setattr(evaluations_module, "_load_user", _fake_load_user)

    _defs, executor = evaluations_module.build_evaluation_tools(
        db=object(),  # type: ignore[arg-type]
        user_id=str(uuid.uuid4()),
    )
    result = await executor.execute_tool(
        ToolCall(id="2", name=tool_name, arguments=arguments)
    )
    assert result.is_error is True
    assert "only admins" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected"),
    [
        ("get_evaluation", {"evaluation_id": "not-a-uuid"}, "evaluation_id"),
        ("list_evaluation_runs", {"evaluation_id": "not-a-uuid"}, "evaluation_id"),
        ("get_evaluation_run_values", {"run_id": "not-a-uuid"}, "run_id"),
        ("trigger_evaluation", {"evaluation_id": "not-a-uuid"}, "evaluation_id"),
        (
            "compare_evaluation_runs",
            {"baseline_run_id": "not-a-uuid", "candidate_run_id": str(uuid.uuid4())},
            "baseline_run_id",
        ),
        (
            "compare_evaluation_runs",
            {"baseline_run_id": str(uuid.uuid4()), "candidate_run_id": "not-a-uuid"},
            "candidate_run_id",
        ),
        ("get_agent_feedback", {"agent_id": "not-a-uuid"}, "agent_id"),
    ],
)
async def test_evaluation_tools_validate_uuid_before_db_access(
    monkeypatch, tool_name, arguments, expected
):
    def _should_not_open_session():
        raise AssertionError("async_session must not be opened for invalid UUIDs")

    monkeypatch.setattr(evaluations_module, "async_session", _should_not_open_session)

    _defs, executor = evaluations_module.build_evaluation_tools(
        db=object(),  # type: ignore[arg-type]
        user_id=str(uuid.uuid4()),
    )
    result = await executor.execute_tool(
        ToolCall(id="3", name=tool_name, arguments=arguments)
    )
    assert result.is_error is True
    assert f"{expected} must be a valid UUID" in result.content


@pytest.mark.asyncio
async def test_list_evaluations_returns_json_payload_for_admin(monkeypatch):
    monkeypatch.setattr(
        evaluations_module,
        "async_session",
        _fake_async_session_factory(_FakeDBSession(items=[])),
    )

    async def _fake_load_user(_db, _uid):
        return SimpleNamespace(id=uuid.uuid4(), is_admin=True), []

    monkeypatch.setattr(evaluations_module, "_load_user", _fake_load_user)

    _defs, executor = evaluations_module.build_evaluation_tools(
        db=object(),  # type: ignore[arg-type]
        user_id=str(uuid.uuid4()),
    )
    result = await executor.execute_tool(
        ToolCall(id="4", name="list_evaluations", arguments={})
    )
    assert result.is_error is False
    payload = json.loads(result.content)
    assert payload == {"count": 0, "evaluations": []}


@pytest.mark.asyncio
async def test_trigger_evaluation_surfaces_route_error_detail(monkeypatch):
    monkeypatch.setattr(
        evaluations_module,
        "async_session",
        _fake_async_session_factory(_FakeDBSession()),
    )

    async def _fake_load_user(_db, _uid):
        return SimpleNamespace(id=uuid.uuid4(), is_admin=True), []

    monkeypatch.setattr(evaluations_module, "_load_user", _fake_load_user)

    import app.api.v1.evaluations as evaluations_api

    async def _fake_trigger(_evaluation_id, _body, *, user, db):
        raise HTTPException(status_code=400, detail="No agents in this evaluation")

    monkeypatch.setattr(evaluations_api, "trigger_evaluation", _fake_trigger)

    _defs, executor = evaluations_module.build_evaluation_tools(
        db=object(),  # type: ignore[arg-type]
        user_id=str(uuid.uuid4()),
    )
    result = await executor.execute_tool(
        ToolCall(
            id="5",
            name="trigger_evaluation",
            arguments={"evaluation_id": str(uuid.uuid4())},
        )
    )
    assert result.is_error is True
    assert result.content == "Error: No agents in this evaluation"


@pytest.mark.asyncio
async def test_trigger_evaluation_returns_created_runs(monkeypatch):
    monkeypatch.setattr(
        evaluations_module,
        "async_session",
        _fake_async_session_factory(_FakeDBSession()),
    )

    async def _fake_load_user(_db, _uid):
        return SimpleNamespace(id=uuid.uuid4(), is_admin=True), []

    monkeypatch.setattr(evaluations_module, "_load_user", _fake_load_user)

    import app.api.v1.evaluations as evaluations_api

    run_payload = {"id": str(uuid.uuid4()), "status": "pending"}

    async def _fake_trigger(_evaluation_id, body, *, user, db):
        assert body.include_default is True
        assert body.model_ids == ["model-a"]
        return [run_payload]

    monkeypatch.setattr(evaluations_api, "trigger_evaluation", _fake_trigger)

    _defs, executor = evaluations_module.build_evaluation_tools(
        db=object(),  # type: ignore[arg-type]
        user_id=str(uuid.uuid4()),
    )
    result = await executor.execute_tool(
        ToolCall(
            id="6",
            name="trigger_evaluation",
            arguments={
                "evaluation_id": str(uuid.uuid4()),
                "model_ids": ["model-a"],
                "include_default": True,
            },
        )
    )
    assert result.is_error is False
    payload = json.loads(result.content)
    assert payload == {"count": 1, "runs": [run_payload]}


# --- Each run says the mode it recorded ---


class _QueuedResult:
    def __init__(self, item):
        self._item = item

    def scalar_one_or_none(self):
        return self._item

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._item))


class _QueuedDBSession:
    """Answers each ``execute`` with the next item, in the order the tool queries."""

    def __init__(self, *items):
        self._items = list(items)

    async def execute(self, _query):
        return _QueuedResult(self._items.pop(0))


def _tool_run(evaluation_id, agent_id, **fields):
    from datetime import UTC, datetime

    base = {
        "id": uuid.uuid4(), "evaluation_id": evaluation_id, "agent_id": agent_id, "flow_id": None, "model_id": "m",
        "status": "completed", "trigger_reason": "manual", "score": 80.0, "quality_score": None,
        "performance_score": None, "f1_score": None, "speed_score": None, "overall_score": None, "completed_entries": 3, "total_entries": 3,
        "error": None, "started_at": None, "completed_at": None, "created_at": datetime.now(UTC),
        "apply_prompt_guards": None,
        "require_tool_approval": None,
    }
    return SimpleNamespace(**{**base, **fields})


async def _run_tool(monkeypatch, db, modes, name, arguments):
    from app.services.evaluation.run_mode import RunMode

    monkeypatch.setattr(evaluations_module, "async_session", _fake_async_session_factory(db))

    async def _fake_load_user(_db, _uid):
        return SimpleNamespace(id=uuid.uuid4(), is_admin=True), []

    async def _fake_load_run_modes(_db, runs):
        return {run.id: RunMode(*modes[run.id]) for run in runs}

    monkeypatch.setattr(evaluations_module, "_load_user", _fake_load_user)
    monkeypatch.setattr(evaluations_module, "load_run_modes", _fake_load_run_modes)
    _defs, executor = evaluations_module.build_evaluation_tools(
        db=object(),  # type: ignore[arg-type]
        user_id=str(uuid.uuid4()),
    )
    result = await executor.execute_tool(ToolCall(id="m", name=name, arguments=arguments))
    assert result.is_error is False, result.content
    return json.loads(result.content)


@pytest.mark.asyncio
async def test_the_run_tools_report_each_runs_recorded_mode_and_its_fallbacks(monkeypatch):
    from datetime import UTC, datetime

    evaluation = SimpleNamespace(
        id=uuid.uuid4(), name="Eval", description=None, enabled=True, execution_mode="full_loop",
        run_mode="parallel", use_agent_default_model=False, model_ids=[], disabled_model_ids=[],
        quality_source="judge", timeout_seconds=3600, last_triggered_at=None,
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC), apply_prompt_guards=False, require_tool_approval=False,
    )
    agent_id = uuid.uuid4()
    llm_only = _tool_run(evaluation.id, agent_id)
    legacy = _tool_run(evaluation.id, agent_id)
    modes = {llm_only.id: ("llm_only", 0), legacy.id: ("full_loop", 2)}

    listed = await _run_tool(
        monkeypatch, _QueuedDBSession(evaluation, [llm_only, legacy]), modes,
        "list_evaluation_runs", {"evaluation_id": str(evaluation.id)},
    )
    assert [(r["execution_mode"], r["fallback_entries"]) for r in listed["runs"]] == [
        ("llm_only", 0), ("full_loop", 2),
    ]
    # The evaluation's own setting stays what it is, and is not a run's label.
    assert listed["evaluation"]["execution_mode"] == "full_loop"

    values = await _run_tool(
        monkeypatch, _QueuedDBSession(llm_only, evaluation, []), modes,
        "get_evaluation_run_values", {"run_id": str(llm_only.id)},
    )
    assert (values["run"]["execution_mode"], values["run"]["fallback_entries"]) == ("llm_only", 0)


@pytest.mark.asyncio
async def test_comparing_runs_of_different_modes_says_so(monkeypatch):
    evaluation = SimpleNamespace(id=uuid.uuid4(), name="Eval", quality_source="judge")
    agent_id = uuid.uuid4()
    baseline = _tool_run(evaluation.id, agent_id)
    candidate = _tool_run(evaluation.id, agent_id)
    modes = {baseline.id: ("llm_only", 0), candidate.id: ("full_loop", 0)}

    compared = await _run_tool(
        monkeypatch, _QueuedDBSession([baseline, candidate], evaluation), modes,
        "compare_evaluation_runs",
        {"baseline_run_id": str(baseline.id), "candidate_run_id": str(candidate.id)},
    )

    assert compared["baseline"]["execution_mode"] == "llm_only"
    assert compared["candidate"]["execution_mode"] == "full_loop"
    assert any("different modes (llm_only then full_loop)" in w for w in compared["warnings"])

    same = {baseline.id: ("full_loop", 0), candidate.id: ("full_loop", 0)}
    compared = await _run_tool(
        monkeypatch, _QueuedDBSession([baseline, candidate], evaluation), same,
        "compare_evaluation_runs",
        {"baseline_run_id": str(baseline.id), "candidate_run_id": str(candidate.id)},
    )
    assert not any("different modes" in w for w in compared["warnings"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ["apply_prompt_guards", "require_tool_approval"],
    ids=["prompt-guard-5202", "tool-approval-5203"],
)
async def test_the_tools_report_the_run_setting_each_run_used(monkeypatch, field):
    """A run reports what it applied, the evaluation its current setting."""
    from datetime import UTC, datetime

    evaluation = SimpleNamespace(
        id=uuid.uuid4(), name="Eval", description=None, enabled=True, execution_mode="full_loop",
        run_mode="parallel", use_agent_default_model=False, model_ids=[], disabled_model_ids=[],
        quality_source="judge", timeout_seconds=3600, last_triggered_at=None,
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC), apply_prompt_guards=False,
        require_tool_approval=False,
    )
    agent_id = uuid.uuid4()
    applied = _tool_run(evaluation.id, agent_id, **{field: True})
    legacy = _tool_run(evaluation.id, agent_id)
    modes = {applied.id: ("full_loop", 0), legacy.id: ("full_loop", 0)}

    listed = await _run_tool(
        monkeypatch, _QueuedDBSession(evaluation, [applied, legacy]), modes,
        "list_evaluation_runs", {"evaluation_id": str(evaluation.id)},
    )
    assert [r[field] for r in listed["runs"]] == [True, None]
    assert listed["evaluation"][field] is False

    values = await _run_tool(
        monkeypatch, _QueuedDBSession(applied, evaluation, []), modes,
        "get_evaluation_run_values", {"run_id": str(applied.id)},
    )
    assert values["run"][field] is True

    compared = await _run_tool(
        monkeypatch, _QueuedDBSession([applied, legacy], evaluation), modes,
        "compare_evaluation_runs",
        {"baseline_run_id": str(applied.id), "candidate_run_id": str(legacy.id)},
    )
    assert (compared["baseline"][field], compared["candidate"][field]) == (True, None)


async def _compare_on_postgres(
    monkeypatch, baseline_scores: dict, candidate_scores: dict, *, quality_source: str = "judge"
) -> dict:
    """``compare_evaluation_runs`` on two completed runs of one agent, stored on
    PostgreSQL with the given score columns; returns the tool's payload."""
    from app.models.agent import Agent
    from app.models.evaluation import Evaluation, EvaluationAgent, EvaluationRun
    from tests.evaluation_database import _admin, _make_db_full

    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin = await _admin(s)
            agent = Agent(user_id=admin.id, name="Agent")
            evaluation = Evaluation(name="Eval", family="STIX_BUNDLE", quality_source=quality_source)
            s.add_all([agent, evaluation])
            await s.flush()
            s.add(EvaluationAgent(evaluation_id=evaluation.id, agent_id=agent.id))
            baseline, candidate = (
                EvaluationRun(
                    evaluation_id=evaluation.id,
                    agent_id=agent.id,
                    model_id="m",
                    status="completed",
                    trigger_reason="manual",
                    completed_entries=3,
                    total_entries=3,
                    **scores,
                )
                for scores in (baseline_scores, candidate_scores)
            )
            s.add_all([baseline, candidate])
            await s.commit()

        monkeypatch.setattr(evaluations_module, "async_session", sm)
        _defs, executor = evaluations_module.build_evaluation_tools(db=object(), user_id=str(admin.id))  # type: ignore[arg-type]
        result = await executor.execute_tool(
            ToolCall(
                id="c",
                name="compare_evaluation_runs",
                arguments={"baseline_run_id": str(baseline.id), "candidate_run_id": str(candidate.id)},
            )
        )
        assert result.is_error is False, result.content
        return json.loads(result.content)
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_comparing_two_runs_reads_the_headlines_not_the_retired_scores(monkeypatch):
    """``compare_evaluation_runs`` compares Overall Quality and Overall Performance,
    then the judge's score, and each run's summary carries the two headlines.
    The retired speed and overall scores, which a new run never writes, are
    neither compared nor served, even on a run that holds them."""
    scores = (
        {"quality_score": 70.0, "performance_score": 50.0, "score": 80.0, "speed_score": 40.0},
        {"quality_score": 62.0, "performance_score": 51.0, "score": 85.0, "speed_score": 90.0},
    )
    compared = await _compare_on_postgres(monkeypatch, *scores)

    assert [(m["metric"], m["delta"], m["verdict"]) for m in compared["metrics"]] == [
        ("quality_score", -8.0, "regression"),
        ("performance_score", 1.0, "neutral"),
        ("score", 5.0, "progression"),
    ]
    for side, stored in zip(("baseline", "candidate"), scores, strict=True):
        summary = compared[side]
        assert (summary["quality_score"], summary["performance_score"]) == (
            stored["quality_score"],
            stored["performance_score"],
        )
        assert "speed_score" not in summary and "overall_score" not in summary
    assert not any("predate" in w for w in compared["warnings"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_evaluation_still_set_to_the_retired_f1_compares_the_judge_score(monkeypatch):
    """An evaluation set to the retired F1 (an older row, an import or a copy)
    names the judge's score as its quality, like any other, and the F1 its runs
    may hold is not compared: no new run records one."""
    compared = await _compare_on_postgres(
        monkeypatch,
        {"quality_score": 70.0, "performance_score": 50.0, "score": 80.0, "f1_score": 30.0},
        {"quality_score": 72.0, "performance_score": 50.0, "score": 85.0, "f1_score": 60.0},
        quality_source="f1",
    )

    assert compared["evaluation"]["quality_metric"] == "score"
    assert [m["metric"] for m in compared["metrics"]] == ["quality_score", "performance_score", "score"]
    assert "f1_score" not in compared["baseline"] and "f1_score" not in compared["candidate"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_run_and_its_results_are_read_without_the_retired_f1(monkeypatch):
    """``get_evaluation_run_values`` serves the judge's score of the run and of
    each result, never the retired F1, even when an older or imported run holds one."""
    from app.models.agent import Agent
    from app.models.dataset import Dataset, DatasetEntry
    from app.models.evaluation import Evaluation, EvaluationAgent, EvaluationResult, EvaluationRun
    from tests.evaluation_database import _admin, _make_db_full

    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin = await _admin(s)
            agent = Agent(user_id=admin.id, name="Agent")
            evaluation = Evaluation(name="Eval", family="PROSE", quality_source="f1")
            dataset = Dataset(name="Set", user_id=admin.id, family="PROSE")
            s.add_all([agent, evaluation, dataset])
            await s.flush()
            entry = DatasetEntry(dataset_id=dataset.id, input_prompt="Q?", expected_answer="A")
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="m",
                status="completed",
                trigger_reason="import",
                score=80.0,
                f1_score=30.0,
                completed_entries=1,
                total_entries=1,
            )
            s.add_all([EvaluationAgent(evaluation_id=evaluation.id, agent_id=agent.id), entry, run])
            await s.flush()
            s.add(EvaluationResult(run_id=run.id, entry_id=entry.id, agent_output="A", score=80.0, f1_score=30.0))
            await s.commit()

        monkeypatch.setattr(evaluations_module, "async_session", sm)
        _defs, executor = evaluations_module.build_evaluation_tools(db=object(), user_id=str(admin.id))  # type: ignore[arg-type]
        result = await executor.execute_tool(
            ToolCall(id="g", name="get_evaluation_run_values", arguments={"run_id": str(run.id)})
        )
        assert result.is_error is False, result.content
        payload = json.loads(result.content)
    finally:
        await cleanup()

    assert payload["run"]["score"] == 80.0 and "f1_score" not in payload["run"]
    assert [(r["score"], "f1_score" in r) for r in payload["results"]] == [(80.0, False)]


_HEADLINES = {"quality_score": 70.0, "performance_score": 50.0, "score": 80.0}
#: A run scored before the headlines: the retired scores, no headline.
_PREDATING = {"score": 80.0, "speed_score": 40.0, "overall_score": 64.0}


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("baseline", "candidate", "warning"),
    [
        (
            _PREDATING,
            _HEADLINES,
            "The baseline run predates Overall Quality and Overall Performance, so only the judge's "
            "score can be compared.",
        ),
        (
            _HEADLINES,
            _PREDATING,
            "The candidate run predates Overall Quality and Overall Performance, so only the judge's "
            "score can be compared.",
        ),
        (
            _PREDATING,
            _PREDATING,
            "Both runs predate Overall Quality and Overall Performance, so only the judge's "
            "score can be compared.",
        ),
    ],
    ids=["baseline-predates", "candidate-predates", "both-predate"],
)
async def test_a_run_scored_before_the_headlines_says_why_they_are_not_compared(
    monkeypatch, baseline, candidate, warning
):
    """A run scored before Overall Quality and Overall Performance existed holds
    only the retired scores, which are not compared: its headline deltas
    are null, and a warning names the run, so the null does not read as a lost
    measurement. The judge's score still compares."""
    compared = await _compare_on_postgres(monkeypatch, baseline, candidate)

    assert warning in compared["warnings"]
    by_metric = {m["metric"]: m for m in compared["metrics"]}
    assert by_metric["quality_score"]["verdict"] is None
    assert by_metric["score"]["verdict"] == "neutral"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_evaluation_says_which_saved_models_no_provider_of_the_owner_offers(monkeypatch):
    """As the evaluation's response does, so an agent can tell a trigger would
    be refused before it calls one: the owner's providers decide, not the caller's."""
    from app.models.evaluation import Evaluation
    from app.models.user import User
    from tests.evaluation_database import _admin, _make_db_full
    from tests.evaluation_helpers import seed_provider

    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin = await _admin(s)
            owner = User(email="o@x.io", full_name="Owner", hashed_password="x", role="user")
            s.add(owner)
            await s.flush()
            await seed_provider(s, ["owner-model"], users=[owner])
            await seed_provider(s, ["admin-model"], users=[admin], provider="mistral")
            evaluation = Evaluation(
                name="Eval", user_id=owner.id, model_ids=["owner-model", "admin-model", "gone-model"]
            )
            s.add(evaluation)
            await s.commit()

        monkeypatch.setattr(evaluations_module, "async_session", sm)
        _defs, executor = evaluations_module.build_evaluation_tools(db=object(), user_id=str(admin.id))  # type: ignore[arg-type]
        result = await executor.execute_tool(
            ToolCall(id="c", name="get_evaluation", arguments={"evaluation_id": str(evaluation.id)})
        )
        assert result.is_error is False, result.content
        assert json.loads(result.content)["unavailable_model_ids"] == ["admin-model", "gone-model"]
    finally:
        await cleanup()
