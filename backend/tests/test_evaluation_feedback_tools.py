"""The two measuring tools of the self-improvement loop.

``compare_evaluation_runs`` (admins, like every evaluation tool) and
``get_agent_feedback`` (the agent's managers, the gate of the agent Feedback
tab). Neither changes anything; both are exercised against fakes here, the
feedback reads themselves being pinned by ``test_annotations_api.py`` and the
PostgreSQL suites.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.services.builtin_tools.evaluations as evaluations_module
from app.services import annotations as annotation_service
from app.services.feedback_criteria import CriterionStatistics
from app.tools.base import ToolCall

pytestmark = pytest.mark.usefixtures("evaluation_enabled")  # evaluation is off by default


class _Result:
    def __init__(self, *, scalars=(), scalar=None):
        self._scalars = list(scalars)
        self._scalar = scalar

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._scalars))

    def scalar_one_or_none(self):
        return self._scalar


class _Db:
    def __init__(self, *results):
        self._results = list(results)
        self.statements: list[object] = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self._results.pop(0)


class _Session:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_exc):
        return False


def _tools(monkeypatch, db, *, admin=True):
    monkeypatch.setattr(evaluations_module, "async_session", lambda: _Session(db))

    async def _load_user(_db, _uid):
        return SimpleNamespace(id=uuid.uuid4(), is_admin=admin), []

    monkeypatch.setattr(evaluations_module, "_load_user", _load_user)
    _defs, executor = evaluations_module.build_evaluation_tools(
        db=object(),  # type: ignore[arg-type]
        user_id=str(uuid.uuid4()),
    )
    return executor


async def _call(executor, name, **arguments):
    return await executor.execute_tool(ToolCall(id="1", name=name, arguments=arguments))


# ---------------------------------------------------------------------------
# compare_evaluation_runs
# ---------------------------------------------------------------------------


def _run(*, evaluation_id, agent_id, flow_id=None, status="completed", model_id="model-a", entries=10, **scores):
    now = datetime(2026, 9, 27, tzinfo=UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        evaluation_id=evaluation_id,
        agent_id=agent_id,
        flow_id=flow_id,
        model_id=model_id,
        status=status,
        trigger_reason="manual",
        execution_mode="full_loop",
        apply_prompt_guards=None,
        require_tool_approval=None,
        score=scores.get("score"),
        quality_score=scores.get("quality_score"),
        performance_score=scores.get("performance_score"),
        speed_score=scores.get("speed_score"),
        overall_score=scores.get("overall_score"),
        f1_score=scores.get("f1_score"),
        completed_entries=entries,
        total_entries=10,
        error=None,
        started_at=now,
        completed_at=now,
        created_at=now,
    )


def _pair(**overrides):
    evaluation_id, agent_id = uuid.uuid4(), uuid.uuid4()
    baseline = _run(evaluation_id=evaluation_id, agent_id=agent_id, score=80.0, quality_score=70.0, performance_score=55.0)
    candidate = _run(
        evaluation_id=overrides.pop("evaluation_id", evaluation_id),
        agent_id=overrides.pop("agent_id", agent_id),
        score=76.0,
        quality_score=71.5,
        performance_score=70.0,
        **overrides,
    )
    return baseline, candidate


def _args(baseline, candidate):
    return {"baseline_run_id": str(baseline.id), "candidate_run_id": str(candidate.id)}


@pytest.mark.asyncio
async def test_two_runs_are_compared_metric_by_metric(monkeypatch):
    baseline, candidate = _pair(model_id="model-b", entries=9)
    evaluation = SimpleNamespace(id=baseline.evaluation_id, name="Weekly digest", quality_source="judge")
    db = _Db(_Result(scalars=[candidate, baseline]), _Result(scalar=evaluation))

    result = await _call(_tools(monkeypatch, db), "compare_evaluation_runs", **_args(baseline, candidate))

    assert result.is_error is False, result.content
    payload = json.loads(result.content)
    assert payload["evaluation"] == {"id": str(evaluation.id), "name": "Weekly digest", "quality_metric": "score"}
    assert payload["baseline"]["id"] == str(baseline.id)
    assert payload["candidate"]["id"] == str(candidate.id)
    assert payload["noise_margin_points"] == 2.0
    assert [(m["metric"], m["delta"], m["verdict"]) for m in payload["metrics"]] == [
        ("quality_score", 1.5, "neutral"),
        ("performance_score", 15.0, "progression"),
        ("score", -4.0, "regression"),
    ]
    # What makes the difference less than a like-for-like comparison is said.
    assert len(payload["warnings"]) == 2
    assert "different models" in payload["warnings"][0]
    assert "different number of entries" in payload["warnings"][1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"evaluation_id": uuid.uuid4()}, "different evaluations"),
        ({"agent_id": uuid.uuid4()}, "different subjects"),
        # A flow subject (#8257) against the agent's run.
        ({"agent_id": None, "flow_id": uuid.uuid4()}, "different subjects"),
        ({"status": "running"}, "the candidate run is running"),
    ],
)
async def test_runs_that_cannot_be_compared_are_refused(monkeypatch, overrides, message):
    baseline, candidate = _pair(**overrides)
    db = _Db(_Result(scalars=[baseline, candidate]))

    result = await _call(_tools(monkeypatch, db), "compare_evaluation_runs", **_args(baseline, candidate))

    assert result.is_error is True
    assert message in result.content
    # Refused before the evaluation is even read.
    assert len(db.statements) == 1


@pytest.mark.asyncio
async def test_a_missing_run_or_the_same_run_twice_is_refused(monkeypatch):
    baseline, candidate = _pair()
    missing = await _call(
        _tools(monkeypatch, _Db(_Result(scalars=[baseline]))),
        "compare_evaluation_runs",
        **_args(baseline, candidate),
    )
    assert missing.is_error is True
    assert "candidate evaluation run not found" in missing.content

    def _no_session():
        raise AssertionError("no session for the same run twice")

    monkeypatch.setattr(evaluations_module, "async_session", _no_session)
    _defs, executor = evaluations_module.build_evaluation_tools(db=object(), user_id=str(uuid.uuid4()))  # type: ignore[arg-type]
    same = await _call(executor, "compare_evaluation_runs", **_args(baseline, baseline))
    assert same.is_error is True
    assert "the same run" in same.content


# ---------------------------------------------------------------------------
# get_agent_feedback
# ---------------------------------------------------------------------------


def _patch_feedback(monkeypatch, *, agent=None, error=None, entries=(), matching=0):
    calls: dict[str, object] = {}

    async def _load_feedback_agent(_db, _user, agent_id):
        calls["agent_id"] = agent_id
        if error is not None:
            raise error
        return agent

    async def _summarize(_db, _agent_id):
        return {"message": (3, 1), "assignment_run": (0, 1), "sub_agent_task": (0, 0), "agent_delegation": (0, 0)}

    accuracy = SimpleNamespace(key="accuracy", name="Accuracy", type="scale", weight=1.0, enabled=True)

    async def _summarize_criteria(_db, _agent_id):
        return SimpleNamespace(
            statistics=[CriterionStatistics(criterion=accuracy, count=4, mean_score=0.6254)],
            daily={},
        )

    async def _list(_db, _agent_id, **kwargs):
        calls["list"] = kwargs
        return list(entries), matching

    monkeypatch.setattr(annotation_service, "load_feedback_agent", _load_feedback_agent)
    monkeypatch.setattr(annotation_service, "summarize_agent_annotations", _summarize)
    monkeypatch.setattr(annotation_service, "summarize_agent_criteria", _summarize_criteria)
    monkeypatch.setattr(annotation_service, "list_agent_annotations", _list)
    return calls


@pytest.mark.asyncio
async def test_an_agents_manager_reads_its_feedback(monkeypatch):
    agent = SimpleNamespace(id=uuid.uuid4(), name="Digest agent")
    entry = SimpleNamespace(
        target_type="assignment_run",
        target_id=uuid.uuid4(),
        rating="negative",
        comment="Wrong totals",
        weighted_score=0.25,
        updated_at=datetime(2026, 9, 27, tzinfo=UTC),
        user_email="someone@example.com",
    )
    calls = _patch_feedback(monkeypatch, agent=agent, entries=[entry], matching=7)

    result = await _call(
        _tools(monkeypatch, _Db(), admin=False),
        "get_agent_feedback",
        agent_id=str(agent.id),
        rating="Negative",
        limit=500,
    )

    assert result.is_error is False, result.content
    payload = json.loads(result.content)
    assert payload["agent"] == {"id": str(agent.id), "name": "Digest agent"}
    assert payload["ratings"]["total"] == 5
    assert payload["ratings"]["positive_share"] == 0.6
    assert payload["ratings"]["by_kind"]["message"] == {"positive": 3, "negative": 1}
    # With a criterion scored, the criteria decide the weighted score.
    assert payload["weighted_score"] == 0.625
    assert payload["criteria"] == [
        {
            "key": "accuracy",
            "name": "Accuracy",
            "type": "scale",
            "weight": 1.0,
            "enabled": True,
            "scores": 4,
            "mean_score": 0.625,
        }
    ]
    assert payload["recent"]["matching"] == 7
    [rating] = payload["recent"]["ratings"]
    assert (rating["kind"], rating["rating"], rating["comment"]) == ("assignment_run", "negative", "Wrong totals")
    # Who rated is not the agent's business.
    assert "someone@example.com" not in result.content
    # The filter is normalized and the page bounded.
    assert calls["list"] == {"rating": "negative", "limit": evaluations_module._MAX_FEEDBACK_LIMIT}


@pytest.mark.asyncio
async def test_someone_who_does_not_manage_the_agent_is_refused(monkeypatch):
    _patch_feedback(
        monkeypatch,
        error=annotation_service.AgentFeedbackForbiddenError(annotation_service.FEEDBACK_ACCESS_DETAIL),
    )

    result = await _call(
        _tools(monkeypatch, _Db(), admin=False), "get_agent_feedback", agent_id=str(uuid.uuid4())
    )

    assert result.is_error is True
    assert "requires agent owner, admin, or config group permission" in result.content


@pytest.mark.asyncio
async def test_an_agent_the_user_cannot_see_is_not_found(monkeypatch):
    _patch_feedback(monkeypatch, error=HTTPException(status_code=404, detail="Agent not found"))

    result = await _call(_tools(monkeypatch, _Db()), "get_agent_feedback", agent_id=str(uuid.uuid4()))

    assert result.is_error is True
    assert result.content == "Error: agent not found."


@pytest.mark.asyncio
async def test_feedback_input_is_checked_before_any_session(monkeypatch):
    def _no_session():
        raise AssertionError("no session for a malformed request")

    monkeypatch.setattr(evaluations_module, "async_session", _no_session)
    _defs, executor = evaluations_module.build_evaluation_tools(db=object(), user_id=str(uuid.uuid4()))  # type: ignore[arg-type]

    bad_rating = await _call(executor, "get_agent_feedback", agent_id=str(uuid.uuid4()), rating="neutral")
    assert bad_rating.is_error is True
    assert "rating must be positive, negative" in bad_rating.content

    _defs, anonymous = evaluations_module.build_evaluation_tools(db=object(), user_id="anonymous")  # type: ignore[arg-type]
    refused = await _call(anonymous, "get_agent_feedback", agent_id=str(uuid.uuid4()))
    assert refused.is_error is True
    assert "requires an authenticated user" in refused.content
