"""Run tags, run search and run parameters.

The tag endpoints, run search, the baseline and agent-tag endpoints and parameter
capture run against an isolated PostgreSQL database (skipped when none is
reachable); the effective-tag merge and the parameter value logic are pure.
"""

from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import select

import app.models  # noqa: F401 - register model metadata
from app.api import deps
from app.api.v1 import evaluations as evaluations_api
from app.api.v1.schemas.evaluation import (
    PatchRunTagsRequest,
    RunSearchRequest,
    SetAgentTagsRequest,
    SetBaselineRequest,
)
from app.core.config import settings
from app.core.database import get_db
from app.core.security import create_access_token
from app.models.agent import Agent
from app.models.dataset import Dataset, DatasetEntry, EvaluationDataset
from app.models.dataset_version import DatasetVersion, DatasetVersionEntry
from app.models.evaluation import (
    Evaluation,
    EvaluationAgent,
    EvaluationResult,
    EvaluationRun,
)
from app.models.evaluation_metric import EvaluationMetric
from app.models.evaluation_param import EvaluationRunParam
from app.models.flow import Flow
from app.models.user import User
from app.services.evaluation import params
from app.services.evaluation.run_search import search_evaluation_runs
from app.services.evaluation.tags import merge_effective_tags, strip_reserved_keys
from tests.scratch_database import scratch_database

# ── Run tag endpoints ─────────────────────────────────────────────────────────────────
# ``PUT`` / ``PATCH /evaluations/runs/{run_id}/tags`` through the router with its real
# auth dependency (bearer token decoded, user loaded, ``require_admin``): ``PUT``
# replaces the tag set, ``PATCH`` merges it and a ``null`` value removes a key; the
# reserved ``baseline`` key is stripped; a non-admin, or a user who cannot see the
# run's evaluation, is refused.


_TABLES = [
    User.__table__,
    Agent.__table__,
    Evaluation.__table__,
    Flow.__table__,
    EvaluationRun.__table__,
]


@pytest.fixture
def _no_groups_or_terms(monkeypatch):
    async def _no_groups(_db, _uid):
        return []

    monkeypatch.setattr(evaluations_api, "get_user_group_ids", _no_groups)
    monkeypatch.setattr(settings, "terms_of_use_enabled", False)


@contextlib.asynccontextmanager
async def _database():
    async with scratch_database("tags", tables=_TABLES) as db:
        yield db.sessions


async def _seed(sessions, tags):
    """An admin-owned evaluation with one run, an admin and a non-admin."""
    async with sessions() as s:
        admin = User(email="a@x.io", full_name="Admin", hashed_password="x", role="admin")
        member = User(email="u@x.io", full_name="User", hashed_password="x", role="user")
        s.add_all([admin, member])
        await s.flush()
        evaluation = Evaluation(name="Eval", user_id=admin.id)
        agent = Agent(name="Agent")
        s.add_all([evaluation, agent])
        await s.flush()
        run = EvaluationRun(
            evaluation_id=evaluation.id,
            agent_id=agent.id,
            model_id="gpt-4o",
            trigger_reason="manual",
            status="completed",
            tags=tags,
        )
        s.add(run)
        await s.flush()
        ids = admin.id, member.id, run.id
        await s.commit()
        return ids


@contextlib.asynccontextmanager
async def _client(sessions):
    app = FastAPI()
    app.include_router(evaluations_api.router, prefix="/api/v1/evaluations")

    async def _get_db():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[deps.require_ai_enabled] = lambda: None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _stored_tags(sessions, run_id):
    async with sessions() as s:
        return (await s.get(EvaluationRun, run_id)).tags


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_groups_or_terms")
@pytest.mark.parametrize(
    ("method", "initial", "body", "expected"),
    [
        (
            "PATCH",
            {"a": "1", "b": "2"},
            {"a": "9", "b": None, "c": "3", "baseline": "x"},
            {"a": "9", "c": "3"},
        ),
        ("PATCH", {"a": "1"}, {"a": None}, {}),
    ],
    ids=[
        "patch-merges-and-null-removes",
        "patch-removing-the-last-key-clears",
    ],
)
async def test_an_admin_edits_run_tags(method, initial, body, expected):
    async with _database() as sessions:
        admin_id, _member_id, run_id = await _seed(sessions, initial)
        async with _client(sessions) as client:
            resp = await client.request(
                method,
                f"/api/v1/evaluations/runs/{run_id}/tags",
                json={"tags": body},
                headers={"Authorization": f"Bearer {create_access_token(str(admin_id))}"},
            )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"run_id": str(run_id), "tags": expected}
        # An emptied set is stored as NULL, never as {}.
        assert await _stored_tags(sessions, run_id) == (expected or None)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_groups_or_terms")
async def test_a_non_admin_cannot_edit_run_tags():
    """Refused at the route, and by the handler's own manage check (rule 19)."""
    async with _database() as sessions:
        _admin_id, member_id, run_id = await _seed(sessions, {"a": "1"})
        token = create_access_token(str(member_id))
        async with _client(sessions) as client:
            resp = await client.request(
                "PATCH",
                f"/api/v1/evaluations/runs/{run_id}/tags",
                json={"tags": {"a": "2"}},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 403, resp.text

        # Called past the route gate, the handler still refuses a user who cannot
        # see the run's evaluation.
        async with sessions() as s:
            user = await s.get(User, member_id)
            with pytest.raises(HTTPException) as exc:
                await evaluations_api.patch_run_tags(
                    str(run_id),
                    PatchRunTagsRequest(tags={"a": None}),
                    user=user,
                    db=s,
                    _ai_gate=None,
                )
        assert exc.value.status_code == 404

        assert await _stored_tags(sessions, run_id) == {"a": "1"}


# ── Run search ────────────────────────────────────────────────────────────────────────


async def _runs_only(session, body, visible):
    """Return just the runs from the ``(run, effective_tags, is_baseline)`` triples."""
    return [r for r, _, _ in await search_evaluation_runs(session, body, visible)]

_TABLES_search = [
    User.__table__,
    Agent.__table__,
    Dataset.__table__,
    DatasetEntry.__table__,
    DatasetVersion.__table__,
    DatasetVersionEntry.__table__,
    Evaluation.__table__,
    Flow.__table__,
    EvaluationAgent.__table__,
    EvaluationRun.__table__,
    EvaluationResult.__table__,
    EvaluationRunParam.__table__,
    EvaluationMetric.__table__,
]


async def _make_db():
    """An isolated database with the tables this module needs (``tests/scratch_database.py``).

    Returns ``(sessionmaker, cleanup)``, the shape the tests here take: the database
    is dropped once ``cleanup`` is awaited. Skips if PostgreSQL is unavailable.
    """
    stack = contextlib.AsyncExitStack()
    db = await stack.enter_async_context(scratch_database("search", tables=_TABLES_search))
    return db.sessions, stack.aclose


async def _seed_search(session):
    agent = Agent(name="Agent")
    evaluation = Evaluation(name="Eval")
    session.add_all([agent, evaluation])
    await session.flush()

    def _run(model, overall, tags=None, pinned=None):
        return EvaluationRun(
            evaluation_id=evaluation.id,
            agent_id=agent.id,
            model_id=model,
            trigger_reason="manual",
            status="completed",
            overall_score=overall,
            tags=tags,
            pinned_dataset_versions=pinned,
        )

    r1 = _run("gpt-4o", 90.0, tags={"baseline": "true"}, pinned={"ds": 3})
    r2 = _run("claude", 70.0, tags={"env": "prod"})
    r3 = _run("gpt-4o", 40.0)
    session.add_all([r1, r2, r3])
    await session.flush()

    # Params
    session.add_all(
        [
            EvaluationRunParam(run_id=r1.id, key="temperature", value="0.7", source="manual"),
            EvaluationRunParam(run_id=r2.id, key="temperature", value="0.2", source="manual"),
        ]
    )
    # Metrics — result rows needed for FK.
    for run, ttft in ((r1, 100.0), (r2, 600.0)):
        res = EvaluationResult(
            run_id=run.id, entry_id=None, agent_output="x", score=1,
            sub_scores={}, evaluator_reasoning="", duration_seconds=1,
        )
        session.add(res)
        await session.flush()
        session.add(EvaluationMetric(result_id=res.id, run_id=run.id, key="ttft_seconds", value=ttft))
    await session.flush()
    return evaluation, [r1, r2, r3]


def _body(**over):
    base = dict(
        evaluation_id=None, params=None, metrics=None, scores=None,
        tags=None, dataset_version=None, created_after=None,
        created_before=None, limit=100,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "visible", "expected"),
    [
        (_body(scores=[SimpleNamespace(field="overall_score", op="gt", value=60.0)]), True, {0, 1}),
        (_body(params={"temperature": "0.7"}), True, {0}),
        (_body(dataset_version=3), True, {0}),
        (_body(params={"does_not_exist": "x"}), True, set()),
        # The search is scoped to the evaluations the caller can see.
        (_body(), False, set()),
    ],
    ids=["score", "param-exact-match", "dataset-version", "unknown-param", "no-visible-evaluation"],
)
async def test_a_filter_returns_exactly_the_matching_runs(body, visible, expected):
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            evaluation, runs = await _seed_search(s)
            await s.commit()
            found = await _runs_only(s, body, {evaluation.id} if visible else set())
            assert {r.id for r in found} == {runs[i].id for i in expected}
            assert len(found) == len(expected)
    finally:
        await cleanup()


# Baseline filtering (agent-scoped `is_baseline` + legacy `tags={"baseline"}`
# → `baseline_only` mapping) is covered by the effective-tag run search tests
# below.


@pytest.mark.integration
@pytest.mark.asyncio
async def test_metric_aggregate_filter():
    """A filter naming a legacy key keeps matching stored rows.

    `ttft_seconds` is not written but stored rows keep it; the
    rows are seeded directly, as an older run's are.
    """
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            evaluation, _ = await _seed_search(s)
            await s.commit()
            body = _body(metrics=[SimpleNamespace(key="ttft_seconds", agg="mean", op="lt", value=300.0)])
            runs = await _runs_only(s, body, {evaluation.id})
            # Only r1 (ttft 100) is under 300.
            assert len(runs) == 1
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nonexistent_metric_returns_empty():
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            evaluation, _ = await _seed_search(s)
            await s.commit()
            body = _body(metrics=[SimpleNamespace(key="nope", agg="mean", op="lt", value=1.0)])
            runs = await _runs_only(s, body, {evaluation.id})
            assert runs == []
    finally:
        await cleanup()


# ── Run parameters ────────────────────────────────────────────────────────────────────
# Auto/evaluation/manual capture, source precedence (manual > evaluation > auto),
# ``(run_id, key)`` uniqueness, value serialization and the two-run diff.


_TABLES_parameters = [
    User.__table__,
    Agent.__table__,
    Evaluation.__table__,
    Flow.__table__,
    EvaluationRun.__table__,
    EvaluationRunParam.__table__,
]


# ---------------------------------------------------------------------------
# Pure-logic tests (no DB)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_serialize_value_scalars_and_json():
    assert params.serialize_value("gpt-4.1") == "gpt-4.1"
    assert params.serialize_value(None) is None
    assert params.serialize_value(0.7) == "0.7"
    assert params.serialize_value(3) == "3"
    assert params.serialize_value(True) == "true"
    assert params.serialize_value(False) == "false"
    # Dicts are JSON with sorted keys for stable comparison.
    assert params.serialize_value({"b": 1, "a": 2}) == '{"a": 2, "b": 1}'


@pytest.mark.integration
def test_should_overwrite_precedence():
    # manual > evaluation > auto
    assert params.should_overwrite("auto", "evaluation") is True
    assert params.should_overwrite("auto", "manual") is True
    assert params.should_overwrite("evaluation", "manual") is True
    # equal precedence refreshes (idempotent re-capture)
    assert params.should_overwrite("manual", "manual") is True
    # lower precedence never overwrites higher
    assert params.should_overwrite("manual", "auto") is False
    assert params.should_overwrite("manual", "evaluation") is False
    assert params.should_overwrite("evaluation", "auto") is False


@pytest.mark.integration
def test_build_auto_params_captures_condition():
    run = SimpleNamespace(
        model_id="claude-sonnet-4",
        pinned_dataset_versions={"11111111-1111-1111-1111-111111111111": 3},
        persona_version_number=2,
        agent_config_versions={"temperature": 5},
        prompt_versions={"22222222-2222-2222-2222-222222222222": 1},
    )
    agent = SimpleNamespace(
        knowledge_base_ids=["a", "b"],
        mcp_server_ids=["m1"],
        custom_tool_ids=[],
        active_integration_providers={"github", "slack"},
    )
    evaluation = SimpleNamespace(
        quality_source="judge",
        evaluator_model=None,
        custom_params=None,
    )

    auto = params.build_auto_params(run, agent, evaluation, execution_mode="full_loop")

    assert auto["model"] == "claude-sonnet-4"
    assert auto["execution_mode"] == "full_loop"
    assert auto["quality_source"] == "judge"
    # The retired quality weight composes no score, so it is no run condition.
    assert "quality_weight" not in auto
    assert auto["evaluator_model"] == "platform_default"
    assert auto["dataset_version:11111111-1111-1111-1111-111111111111"] == 3
    assert auto["kb_count"] == 2
    # 1 mcp + 0 custom + 2 integrations
    assert auto["tool_count"] == 3
    assert auto["persona_version"] == 2
    assert auto["config_version:temperature"] == 5
    assert auto["prompt_version:22222222-2222-2222-2222-222222222222"] == 1


@pytest.mark.integration
def test_a_run_of_an_evaluation_still_set_to_the_retired_f1_logs_the_judge():
    """A new run is scored by the judge whatever the evaluation's retired
    setting says, so its logged condition says the judge, not F1."""
    run = SimpleNamespace(
        model_id="m",
        pinned_dataset_versions={},
        persona_version_number=None,
        agent_config_versions={},
        prompt_versions={},
    )
    evaluation = SimpleNamespace(quality_source="f1", evaluator_model=None, custom_params=None)

    auto = params.build_auto_params(run, None, evaluation, execution_mode="llm_only")

    assert auto["quality_source"] == "judge"


@pytest.mark.integration
def test_compute_param_diff_changed_vs_identical():
    a = [
        {"key": "model", "value": "claude-sonnet-4", "source": "auto"},
        {"key": "temperature", "value": "0.7", "source": "manual"},
        {"key": "only_a", "value": "x", "source": "manual"},
    ]
    b = [
        {"key": "model", "value": "claude-sonnet-4", "source": "auto"},
        {"key": "temperature", "value": "0.2", "source": "manual"},
    ]
    diff = params.compute_param_diff(a, b)

    identical_keys = {d["key"] for d in diff["identical"]}
    changed = {d["key"]: d for d in diff["changed"]}

    assert identical_keys == {"model"}
    assert changed["temperature"] == {
        "key": "temperature",
        "value_a": "0.7",
        "value_b": "0.2",
        "source_a": "manual",
        "source_b": "manual",
    }
    # A key present only in run A shows up as changed with the missing side None.
    assert changed["only_a"] == {
        "key": "only_a",
        "value_a": "x",
        "value_b": None,
        "source_a": "manual",
        "source_b": None,
    }


# ---------------------------------------------------------------------------
# DB-backed tests (isolated PostgreSQL)
# ---------------------------------------------------------------------------


async def _make_db_parameters():
    """An isolated database with the tables this module needs (``tests/scratch_database.py``).

    Returns ``(sessionmaker, cleanup)``, the shape the tests here take: the database
    is dropped once ``cleanup`` is awaited. Skips if PostgreSQL is unavailable.
    """
    stack = contextlib.AsyncExitStack()
    db = await stack.enter_async_context(scratch_database("param", tables=_TABLES_parameters))
    return db.sessions, stack.aclose


async def _seed_run(session) -> EvaluationRun:
    agent = Agent(name="Agent")
    evaluation = Evaluation(name="Eval")
    session.add_all([agent, evaluation])
    await session.flush()
    run = EvaluationRun(
        evaluation_id=evaluation.id,
        agent_id=agent.id,
        model_id="claude-sonnet-4",
        trigger_reason="manual",
    )
    session.add(run)
    await session.flush()
    return run


@pytest.mark.integration
@pytest.mark.asyncio
async def test_capture_and_get_run_params_roundtrip():
    sm, cleanup = await _make_db_parameters()
    try:
        async with sm() as s:
            run = await _seed_run(s)
            await params.capture_params(
                s, run.id, {"model": "claude-sonnet-4", "temperature": 0.7}, "auto"
            )
            await s.commit()

            rows = await params.get_run_params(s, run.id)
            by_key = {r["key"]: r for r in rows}
            assert by_key["model"]["value"] == "claude-sonnet-4"
            assert by_key["model"]["source"] == "auto"
            assert by_key["temperature"]["value"] == "0.7"
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("captures", "expected"),
    [
        # auto first, then evaluation, then manual: manual wins.
        (
            [("auto", "0.1"), ("evaluation", "0.5"), ("manual", "0.9")],
            ("0.9", "manual"),
        ),
        # manual captured first (at trigger), auto later (at execute): auto must
        # not clobber the manual value.
        ([("manual", "manual-value"), ("auto", "auto-value")], ("manual-value", "manual")),
    ],
    ids=["higher-precedence-later", "lower-precedence-later"],
)
async def test_the_highest_precedence_source_wins_whatever_the_capture_order(
    captures, expected
):
    sm, cleanup = await _make_db_parameters()
    try:
        async with sm() as s:
            run = await _seed_run(s)
            for source, value in captures:
                await params.capture_params(s, run.id, {"temperature": value}, source)
            await s.commit()

            rows = {r["key"]: r for r in await params.get_run_params(s, run.id)}
            assert (rows["temperature"]["value"], rows["temperature"]["source"]) == expected
            # One row per (run, key), however many captures wrote it.
            result = await s.execute(
                select(EvaluationRunParam).where(
                    EvaluationRunParam.run_id == run.id,
                    EvaluationRunParam.key == "temperature",
                )
            )
            assert len(result.scalars().all()) == 1
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_capture_run_params_full_set():
    sm, cleanup = await _make_db_parameters()
    try:
        async with sm() as s:
            agent = Agent(name="Agent")
            evaluation = Evaluation(name="Eval")
            evaluation.custom_params = {"experiment": "baseline"}
            s.add_all([agent, evaluation])
            await s.flush()
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="claude-sonnet-4",
                trigger_reason="manual",
                pinned_dataset_versions={"33333333-3333-3333-3333-333333333333": 1},
            )
            s.add(run)
            await s.flush()

            await params.capture_run_params(
                s,
                run,
                agent,
                evaluation,
                execution_mode="llm_only",
                manual_params={"temperature": "0.3", "experiment": "override"},
            )
            await s.commit()

            rows = {r["key"]: r for r in await params.get_run_params(s, run.id)}
            assert rows["model"]["value"] == "claude-sonnet-4"
            assert rows["model"]["source"] == "auto"
            assert rows["execution_mode"]["value"] == "llm_only"
            assert rows["dataset_version:33333333-3333-3333-3333-333333333333"]["value"] == "1"
            # manual override beats the evaluation custom_param of the same key
            assert rows["experiment"]["value"] == "override"
            assert rows["experiment"]["source"] == "manual"
            assert rows["temperature"]["source"] == "manual"
    finally:
        await cleanup()


class _QueryCounter:
    """Counts real cursor executions on the session's engine, scoped to a block.

    Same pattern as ``test_evaluation_aggregation.py``'s counter: instruments
    the engine (where the cursor actually is) rather than the session.
    """

    def __init__(self, session):
        self._engine = session.bind.sync_engine
        self.count = 0

    def _on_execute(self, *args, **kwargs):
        self.count += 1

    def __enter__(self):
        from sqlalchemy import event

        event.listen(self._engine, "before_cursor_execute", self._on_execute)
        return self

    def __exit__(self, *exc):
        from sqlalchemy import event

        event.remove(self._engine, "before_cursor_execute", self._on_execute)
        return False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_capture_params_for_runs_writes_the_same_rows_as_capture_params():
    """The batched helper must be a drop-in replacement, value-for-value."""
    sm, cleanup = await _make_db_parameters()
    try:
        async with sm() as s:
            runs = [await _seed_run(s) for _ in range(3)]
            await s.commit()

            await params.capture_params_for_runs(
                s, [r.id for r in runs], {"reuse": "off"}, "manual"
            )
            await s.commit()

            for run in runs:
                rows = {r["key"]: r for r in await params.get_run_params(s, run.id)}
                assert rows["reuse"]["value"] == "off"
                assert rows["reuse"]["source"] == "manual"
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_capture_params_for_runs_costs_less_than_looping_capture_params():
    """One ``INSERT ... ON CONFLICT`` per key for the whole batch costs strictly
    less than looping ``capture_params`` (a per-run ``SELECT`` + write) over the
    same batch.
    """
    sm, cleanup = await _make_db_parameters()
    try:
        async with sm() as s:
            old_runs = [await _seed_run(s) for _ in range(3)]
            await s.commit()
            with _QueryCounter(s) as looped:
                for run in old_runs:
                    await params.capture_params(s, run.id, {"reuse": "off"}, "manual")
                await s.flush()
            await s.commit()

            new_runs = [await _seed_run(s) for _ in range(3)]
            await s.commit()
            with _QueryCounter(s) as batched:
                await params.capture_params_for_runs(
                    s, [r.id for r in new_runs], {"reuse": "off"}, "manual"
                )
                await s.flush()
            await s.commit()

        assert batched.count < looped.count, (
            f"batched helper issued {batched.count} statements, looped "
            f"capture_params issued {looped.count} for the same 3-run batch — "
            "the batched path must not cost one round trip per run"
        )
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_capture_params_for_runs_upsert_precedence_on_a_collision():
    """A real upsert honouring ``should_overwrite``'s manual > evaluation > auto
    precedence: calling it twice for the same run/key does not raise
    ``uq_eval_run_param_key``, an equal-or-higher source overwrites and a
    strictly lower one is left alone.
    """
    sm, cleanup = await _make_db_parameters()
    try:
        async with sm() as s:
            run = await _seed_run(s)
            await s.commit()

            # Same source re-captured: equal precedence refreshes the value.
            await params.capture_params_for_runs(s, [run.id], {"reuse": "on"}, "manual")
            await s.commit()
            await params.capture_params_for_runs(s, [run.id], {"reuse": "off"}, "manual")
            await s.commit()

            rows = {r["key"]: r for r in await params.get_run_params(s, run.id)}
            assert rows["reuse"]["value"] == "off"
            assert rows["reuse"]["source"] == "manual"

            # A strictly lower-precedence source leaves "manual" alone.
            await params.capture_params_for_runs(s, [run.id], {"reuse": "on"}, "auto")
            await s.commit()
            rows = {r["key"]: r for r in await params.get_run_params(s, run.id)}
            assert rows["reuse"]["value"] == "off", (
                "a lower-precedence source must not overwrite an existing "
                "higher-precedence value"
            )
            assert rows["reuse"]["source"] == "manual"
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_groups_or_terms")
async def test_run_search_tells_a_placebo_comparison_run_from_an_ordinary_one():
    """A comparison's run carries its arm, target and comparison; any other reads ``real``."""
    comparison_id = uuid.uuid4()
    target = {"kind": "persona", "skill_id": None}
    async with scratch_database("search_arm", tables=_TABLES_search) as db:
        async with db.sessions() as s:
            admin = User(email="a@x.io", full_name="Admin", hashed_password="x", role="admin")
            agent = Agent(name="Agent")
            s.add_all([admin, agent])
            await s.flush()
            evaluation = Evaluation(name="Eval", user_id=admin.id)
            s.add(evaluation)
            await s.flush()
            common = dict(
                evaluation_id=evaluation.id, agent_id=agent.id, model_id="gpt-4o",
                trigger_reason="manual", status="completed",
            )
            s.add_all([
                EvaluationRun(**common, quality_score=80.0),
                EvaluationRun(
                    **common, quality_score=50.0, arm="placebo", trial_index=1,
                    placebo_target=target, placebo_comparison_id=comparison_id,
                ),
            ])
            await s.commit()
            admin_id = admin.id

        async with _client(db.sessions) as client:
            resp = await client.post(
                "/api/v1/evaluations/runs/search",
                json={"scores": [{"field": "quality_score", "op": "gte", "value": 0}]},
                headers={"Authorization": f"Bearer {create_access_token(str(admin_id))}"},
            )

    assert resp.status_code == 200, resp.text
    by_score = {r["quality_score"]: r for r in resp.json()["runs"]}
    assert (by_score[80.0]["arm"], by_score[80.0]["placebo_comparison_id"]) == ("real", None)
    assert by_score[50.0]["arm"] == "placebo"
    assert by_score[50.0]["placebo_target"] == target
    assert by_score[50.0]["placebo_comparison_id"] == str(comparison_id)


# ── Effective-tag and baseline_only run search ────────────────────────────────────────
# An evaluation-level tag selects runs across evaluations, an agent-level tag is
# scoped to its agent's runs and wins on a key collision, ``baseline_only`` returns
# only the baseline agent's runs, and the legacy ``tags={"baseline": "true"}`` maps
# to it.


_TABLES_effective_tags = [
    User.__table__,
    Agent.__table__,
    Evaluation.__table__,
    Flow.__table__,
    EvaluationAgent.__table__,
    EvaluationRun.__table__,
]


async def _make_db_effective_tags():
    """An isolated database with the tables this module needs (``tests/scratch_database.py``).

    Returns ``(sessionmaker, cleanup)``, the shape the tests here take: the database
    is dropped once ``cleanup`` is awaited. Skips if PostgreSQL is unavailable.
    """
    stack = contextlib.AsyncExitStack()
    db = await stack.enter_async_context(scratch_database("runsearch", tables=_TABLES_effective_tags))
    return db.sessions, stack.aclose


async def _add_run(session, ev_id, agent_id, *, tags=None):
    run = EvaluationRun(
        evaluation_id=ev_id,
        agent_id=agent_id,
        model_id="claude-sonnet-4",
        trigger_reason="manual",
        tags=tags,
        status="completed",
    )
    session.add(run)
    await session.flush()
    return run


@pytest.mark.integration
@pytest.mark.asyncio
async def test_effective_tag_filter_spans_evaluations_and_agents():
    sm, cleanup = await _make_db_effective_tags()
    try:
        async with sm() as s:
            # Two evaluations, each with one agent; both evaluation-tagged
            # campaign=q1. A stray third eval has no tag.
            ev1 = Evaluation(name="E1", tags={"campaign": "q1"})
            ev2 = Evaluation(name="E2", tags={"campaign": "q1"})
            ev3 = Evaluation(name="E3", tags={"campaign": "other"})
            a1, a2, a3 = Agent(name="A1"), Agent(name="A2"), Agent(name="A3")
            s.add_all([ev1, ev2, ev3, a1, a2, a3])
            await s.flush()
            s.add_all([
                EvaluationAgent(evaluation_id=ev1.id, agent_id=a1.id),
                EvaluationAgent(evaluation_id=ev2.id, agent_id=a2.id),
                EvaluationAgent(evaluation_id=ev3.id, agent_id=a3.id),
            ])
            r1 = await _add_run(s, ev1.id, a1.id)
            r2 = await _add_run(s, ev2.id, a2.id)
            await _add_run(s, ev3.id, a3.id)
            await s.commit()

            visible = {ev1.id, ev2.id, ev3.id}
            body = RunSearchRequest(tags={"campaign": "q1"})
            hits = await search_evaluation_runs(s, body, visible)
            hit_ids = {run.id for run, _eff, _bl in hits}
            assert hit_ids == {r1.id, r2.id}
            # Effective tags surfaced per hit.
            for _run, eff, _bl in hits:
                assert eff["campaign"] == "q1"
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_tag_overrides_evaluation_tag_in_search():
    sm, cleanup = await _make_db_effective_tags()
    try:
        async with sm() as s:
            ev = Evaluation(name="E", tags={"campaign": "eval"})
            a, b = Agent(name="A"), Agent(name="B")
            s.add_all([ev, a, b])
            await s.flush()
            s.add_all([
                EvaluationAgent(evaluation_id=ev.id, agent_id=a.id, tags={"campaign": "agent"}),
                EvaluationAgent(evaluation_id=ev.id, agent_id=b.id),
            ])
            ra = await _add_run(s, ev.id, a.id)
            rb = await _add_run(s, ev.id, b.id)
            await s.commit()

            visible = {ev.id}
            # Only A's run has effective campaign=agent.
            hits = await search_evaluation_runs(
                s, RunSearchRequest(tags={"campaign": "agent"}), visible
            )
            assert {run.id for run, _e, _b in hits} == {ra.id}
            # B's run still matches the evaluation value.
            hits2 = await search_evaluation_runs(
                s, RunSearchRequest(tags={"campaign": "eval"}), visible
            )
            assert {run.id for run, _e, _b in hits2} == {rb.id}
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_baseline_only_returns_only_baseline_agent_runs():
    sm, cleanup = await _make_db_effective_tags()
    try:
        async with sm() as s:
            ev = Evaluation(name="E")
            a, b = Agent(name="A"), Agent(name="B")
            s.add_all([ev, a, b])
            await s.flush()
            s.add_all([
                EvaluationAgent(evaluation_id=ev.id, agent_id=a.id, is_baseline=True),
                EvaluationAgent(evaluation_id=ev.id, agent_id=b.id),
            ])
            ra = await _add_run(s, ev.id, a.id)
            await _add_run(s, ev.id, b.id)
            await s.commit()

            hits = await search_evaluation_runs(
                s, RunSearchRequest(baseline_only=True), {ev.id}
            )
            assert {run.id for run, _e, _b in hits} == {ra.id}
            assert all(is_bl for _r, _e, is_bl in hits)
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_legacy_baseline_tag_request_maps_to_baseline_only():
    sm, cleanup = await _make_db_effective_tags()
    try:
        async with sm() as s:
            ev = Evaluation(name="E")
            a, b = Agent(name="A"), Agent(name="B")
            s.add_all([ev, a, b])
            await s.flush()
            s.add_all([
                EvaluationAgent(evaluation_id=ev.id, agent_id=a.id, is_baseline=True),
                EvaluationAgent(evaluation_id=ev.id, agent_id=b.id),
            ])
            ra = await _add_run(s, ev.id, a.id)
            await _add_run(s, ev.id, b.id)
            await s.commit()

            # Legacy convention: tags={"baseline": "true"} → baseline_only.
            hits = await search_evaluation_runs(
                s, RunSearchRequest(tags={"baseline": "true"}), {ev.id}
            )
            assert {run.id for run, _e, _b in hits} == {ra.id}
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_boolean_and_numeric_tag_filter_values_coerce():
    """A direct API client may send non-string tag filter values.

    Effective tags are always stored as coerced strings, so numeric/boolean
    filter values must be coerced the same way for exact matching, and only a
    boolean ``True`` (not a numeric ``1``) may act as the "key exists" sentinel.
    """
    sm, cleanup = await _make_db_effective_tags()
    try:
        async with sm() as s:
            ev = Evaluation(name="E")
            ag = Agent(name="A")
            s.add_all([ev, ag])
            await s.flush()
            s.add(EvaluationAgent(evaluation_id=ev.id, agent_id=ag.id))
            r_count1 = await _add_run(s, ev.id, ag.id, tags={"count": 1})
            r_count2 = await _add_run(s, ev.id, ag.id, tags={"count": 2})
            r_flag_false = await _add_run(s, ev.id, ag.id, tags={"flag": False})
            r_flag_true = await _add_run(s, ev.id, ag.id, tags={"flag": True})
            await s.commit()

            visible = {ev.id}

            # Numeric exact match: 1 must match "1" only (not act as key-exists,
            # which would also return count=2).
            hits = await search_evaluation_runs(
                s, RunSearchRequest(tags={"count": 1}), visible
            )
            assert {run.id for run, _e, _b in hits} == {r_count1.id}

            # Boolean False exact match: must match the coerced "false" value.
            hits = await search_evaluation_runs(
                s, RunSearchRequest(tags={"flag": False}), visible
            )
            assert {run.id for run, _e, _b in hits} == {r_flag_false.id}

            # Boolean True is the "key exists" sentinel: both flag runs match,
            # neither count run does.
            hits = await search_evaluation_runs(
                s, RunSearchRequest(tags={"flag": True}), visible
            )
            assert {run.id for run, _e, _b in hits} == {
                r_flag_false.id,
                r_flag_true.id,
            }

            # Sanity: count=2 still resolves as an exact match on its own.
            hits = await search_evaluation_runs(
                s, RunSearchRequest(tags={"count": 2}), visible
            )
            assert {run.id for run, _e, _b in hits} == {r_count2.id}
    finally:
        await cleanup()


# ── Baseline and agent-tag endpoints ──────────────────────────────────────────────────
# ``PUT /evaluations/{id}/baseline`` and the agent-tag endpoints, called directly with
# stubbed audit and group lookups: set and clear the baseline, an atomic re-point
# (only one baseline survives), ``400`` for an agent not linked to the evaluation,
# idempotent re-set, and the reserved ``baseline`` key stripped from agent tags.


_TABLES_baseline = [
    User.__table__,
    Agent.__table__,
    Evaluation.__table__,
    Flow.__table__,
    EvaluationAgent.__table__,
    Dataset.__table__,
    DatasetEntry.__table__,
    EvaluationDataset.__table__,
    EvaluationRun.__table__,
]


@pytest.fixture
def _stub_audit_and_groups(monkeypatch):
    async def _no_groups(_db, _uid):
        return []

    async def _no_audit(*_a, **_k):
        return None

    monkeypatch.setattr(evaluations_api, "get_user_group_ids", _no_groups)
    monkeypatch.setattr(evaluations_api, "log_audit", _no_audit)


async def _make_db_baseline():
    """An isolated database with the tables this module needs (``tests/scratch_database.py``).

    Returns ``(sessionmaker, cleanup)``, the shape the tests here take: the database
    is dropped once ``cleanup`` is awaited. Skips if PostgreSQL is unavailable.
    """
    stack = contextlib.AsyncExitStack()
    db = await stack.enter_async_context(scratch_database("baseline", tables=_TABLES_baseline))
    return db.sessions, stack.aclose


async def _seed_baseline(session):
    admin = User(email="a@x.io", full_name="Admin", hashed_password="x", role="admin")
    session.add(admin)
    await session.flush()
    ev = Evaluation(name="Eval", user_id=admin.id)
    a = Agent(name="A")
    b = Agent(name="B")
    session.add_all([ev, a, b])
    await session.flush()
    session.add_all([
        EvaluationAgent(evaluation_id=ev.id, agent_id=a.id),
        EvaluationAgent(evaluation_id=ev.id, agent_id=b.id),
    ])
    await session.commit()
    return admin, ev, a, b


async def _baseline_agent_id(session, ev_id):
    row = (
        await session.execute(
            select(EvaluationAgent.agent_id).where(
                EvaluationAgent.evaluation_id == ev_id,
                EvaluationAgent.is_baseline.is_(True),
            )
        )
    ).scalar_one_or_none()
    return row


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_audit_and_groups")
async def test_set_clear_and_atomic_repoint_baseline():
    sm, cleanup = await _make_db_baseline()
    try:
        async with sm() as s:
            admin, ev, a, b = await _seed_baseline(s)

            resp = await evaluations_api.set_baseline(
                str(ev.id), SetBaselineRequest(agent_id=str(a.id)), user=admin, db=s
            )
            assert resp["baseline_agent_id"] == str(a.id)
            assert await _baseline_agent_id(s, ev.id) == a.id

            # Re-pointing is atomic: only B remains baseline.
            resp = await evaluations_api.set_baseline(
                str(ev.id), SetBaselineRequest(agent_id=str(b.id)), user=admin, db=s
            )
            assert resp["baseline_agent_id"] == str(b.id)
            baselines = (
                await s.execute(
                    select(EvaluationAgent).where(
                        EvaluationAgent.evaluation_id == ev.id,
                        EvaluationAgent.is_baseline.is_(True),
                    )
                )
            ).scalars().all()
            assert len(baselines) == 1
            assert baselines[0].agent_id == b.id

            resp = await evaluations_api.set_baseline(
                str(ev.id), SetBaselineRequest(agent_id=None), user=admin, db=s
            )
            assert resp["baseline_agent_id"] is None
            assert await _baseline_agent_id(s, ev.id) is None
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_audit_and_groups")
async def test_set_baseline_idempotent():
    sm, cleanup = await _make_db_baseline()
    try:
        async with sm() as s:
            admin, ev, a, _b = await _seed_baseline(s)
            await evaluations_api.set_baseline(
                str(ev.id), SetBaselineRequest(agent_id=str(a.id)), user=admin, db=s
            )
            await evaluations_api.set_baseline(
                str(ev.id), SetBaselineRequest(agent_id=str(a.id)), user=admin, db=s
            )
            assert await _baseline_agent_id(s, ev.id) == a.id
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_audit_and_groups")
async def test_set_baseline_unlinked_agent_400():
    sm, cleanup = await _make_db_baseline()
    try:
        async with sm() as s:
            admin, ev, _a, _b = await _seed_baseline(s)
            stranger = Agent(name="Stranger")
            s.add(stranger)
            await s.commit()
            with pytest.raises(HTTPException) as exc:
                await evaluations_api.set_baseline(
                    str(ev.id),
                    SetBaselineRequest(agent_id=str(stranger.id)),
                    user=admin,
                    db=s,
                )
            assert exc.value.status_code == 400
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_audit_and_groups")
async def test_set_agent_tags_strips_reserved_baseline_key():
    sm, cleanup = await _make_db_baseline()
    try:
        async with sm() as s:
            admin, ev, a, _b = await _seed_baseline(s)
            resp = await evaluations_api.set_agent_tags(
                str(ev.id),
                str(a.id),
                SetAgentTagsRequest(tags={"baseline": "true", "variant": "cot"}),
                user=admin,
                db=s,
            )
            assert resp["tags"] == {"variant": "cot"}
            persisted = (
                await s.execute(
                    select(EvaluationAgent.tags).where(
                        EvaluationAgent.evaluation_id == ev.id,
                        EvaluationAgent.agent_id == a.id,
                    )
                )
            ).scalar_one()
            assert persisted == {"variant": "cot"}
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_audit_and_groups")
async def test_agent_tags_unlinked_agent_400():
    sm, cleanup = await _make_db_baseline()
    try:
        async with sm() as s:
            admin, ev, _a, _b = await _seed_baseline(s)
            stranger = Agent(name="Stranger")
            s.add(stranger)
            await s.commit()
            with pytest.raises(HTTPException) as exc:
                await evaluations_api.set_agent_tags(
                    str(ev.id),
                    str(stranger.id),
                    SetAgentTagsRequest(tags={"k": "v"}),
                    user=admin,
                    db=s,
                )
            assert exc.value.status_code == 400
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_audit_and_groups")
async def test_agent_tags_are_scoped_and_leave_other_levels_untouched():
    """Writing one agent's tags touches neither the evaluation's tags nor another agent's."""
    sm, cleanup = await _make_db_baseline()
    try:
        async with sm() as s:
            admin, ev, a, b = await _seed_baseline(s)
            ev.tags = {"campaign": "spring"}
            await s.commit()

            await evaluations_api.set_agent_tags(
                str(ev.id),
                str(a.id),
                SetAgentTagsRequest(tags={"variant": "cot"}),
                user=admin,
                db=s,
            )

            eval_tags = (
                await s.execute(select(Evaluation.tags).where(Evaluation.id == ev.id))
            ).scalar_one()
            assert eval_tags == {"campaign": "spring"}

            a_tags = (
                await s.execute(
                    select(EvaluationAgent.tags).where(
                        EvaluationAgent.evaluation_id == ev.id,
                        EvaluationAgent.agent_id == a.id,
                    )
                )
            ).scalar_one()
            b_tags = (
                await s.execute(
                    select(EvaluationAgent.tags).where(
                        EvaluationAgent.evaluation_id == ev.id,
                        EvaluationAgent.agent_id == b.id,
                    )
                )
            ).scalar_one()
            assert a_tags == {"variant": "cot"}
            assert b_tags is None
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_audit_and_groups")
async def test_agents_returned_in_insertion_order_not_heap_order():
    """Agents come back in created_at order, not heap order.

    Setting a tag or baseline UPDATEs the EvaluationAgent row, which moves it in
    physical order; without an explicit ORDER BY the agent would jump position in the UI.
    """
    from datetime import UTC, datetime, timedelta

    sm, cleanup = await _make_db_baseline()
    try:
        async with sm() as s:
            admin = User(
                email="a@x.io", full_name="Admin", hashed_password="x", role="admin"
            )
            s.add(admin)
            await s.flush()
            ev = Evaluation(name="Eval", user_id=admin.id)
            s.add(ev)
            await s.flush()

            base = datetime.now(UTC)
            # Rows are inserted in c, b, a order, the reverse of created_at, so an
            # unordered query would return [c, b, a].
            c = Agent(name="C")
            s.add(c)
            await s.flush()
            s.add(EvaluationAgent(
                evaluation_id=ev.id,
                agent_id=c.id,
                created_at=base + timedelta(seconds=2),
            ))
            await s.flush()
            b = Agent(name="B")
            s.add(b)
            await s.flush()
            s.add(EvaluationAgent(
                evaluation_id=ev.id,
                agent_id=b.id,
                created_at=base + timedelta(seconds=1),
            ))
            await s.flush()
            a = Agent(name="A")
            s.add(a)
            await s.flush()
            s.add(EvaluationAgent(
                evaluation_id=ev.id, agent_id=a.id, created_at=base
            ))
            await s.commit()

            expected = [str(a.id), str(b.id), str(c.id)]

            resp = await evaluations_api._build_evaluation_response(s, ev, caller_can_manage=True)
            assert [ag["id"] for ag in resp["agents"]] == expected

            # A baseline/tag UPDATE must not reorder the list either.
            await evaluations_api.set_baseline(
                str(ev.id), SetBaselineRequest(agent_id=str(a.id)), user=admin, db=s
            )
            await evaluations_api.set_agent_tags(
                str(ev.id),
                str(a.id),
                SetAgentTagsRequest(tags={"variant": "cot"}),
                user=admin,
                db=s,
            )

            resp = await evaluations_api._build_evaluation_response(s, ev, caller_can_manage=True)
            assert [ag["id"] for ag in resp["agents"]] == expected
    finally:
        await cleanup()


# ── Effective-tag merge ───────────────────────────────────────────────────────────────
# ``services/evaluation/tags.py``, no database: precedence ``run > agent > evaluation``
# on a key collision, an empty level never blanks a lower one, the reserved
# ``baseline`` key is stripped and values are stringified.


# ── merge_effective_tags ────────────────────────────────────────────


def test_merge_precedence_run_over_agent_over_evaluation():
    # Same key at all three levels → most-specific (run) value wins.
    merged = merge_effective_tags(
        {"campaign": "eval", "shared": "eval"},
        {"campaign": "agent", "variant": "cot"},
        {"campaign": "run", "note": "smoke"},
    )
    assert merged == {
        "campaign": "run",
        "shared": "eval",
        "variant": "cot",
        "note": "smoke",
    }


def test_merge_agent_over_evaluation_when_no_run_value():
    merged = merge_effective_tags({"k": "eval"}, {"k": "agent"}, {"other": "x"})
    assert merged["k"] == "agent"


def test_empty_or_absent_levels_contribute_nothing():
    # A None/empty level must not blank a lower-priority level.
    assert merge_effective_tags({"a": "1"}, None, None) == {"a": "1"}
    assert merge_effective_tags({"a": "1"}, {}, {}) == {"a": "1"}
    assert merge_effective_tags(None, None, {"a": "1"}) == {"a": "1"}
    assert merge_effective_tags(None, None, None) == {}


def test_merge_strips_reserved_baseline_key_at_every_level():
    merged = merge_effective_tags(
        {"baseline": "x", "ok": "1"},
        {"baseline": "y"},
        {"baseline": "z", "note": "n"},
    )
    assert "baseline" not in merged
    assert merged == {"ok": "1", "note": "n"}


def test_merge_coerces_non_string_values_to_string():
    merged = merge_effective_tags(None, None, {"n": 3, "b": True, "f": 0.5})
    assert merged == {"n": "3", "b": "true", "f": "0.5"}


# ── strip_reserved_keys ─────────────────────────────────────────────


def test_strip_reserved_keys_drops_baseline_and_stringifies():
    assert strip_reserved_keys({"baseline": "x", "ok": "1"}) == {"ok": "1"}
    assert strip_reserved_keys({"n": 2, "b": False}) == {"n": "2", "b": "false"}


def test_strip_reserved_keys_none_and_empty_return_none():
    assert strip_reserved_keys(None) is None
    assert strip_reserved_keys({}) is None
    # A dict that becomes empty after stripping reserved keys collapses to None.
    assert strip_reserved_keys({"baseline": "x"}) is None
