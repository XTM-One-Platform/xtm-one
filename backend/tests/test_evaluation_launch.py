"""Every way of starting evaluation runs applies the same checks.

The schedule, the Run button and the API, replay and bulk "Re-run full" refuse
the same launches and record the same snapshot. ``enabled`` pauses the
automatic launches only, never a run a person asks for.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

import app.models  # noqa: F401 - register model metadata
from app.api.v1 import evaluations as evaluations_api
from app.api.v1.schemas.evaluation import TriggerEvaluationFromRunsRequest
from app.models.agent import Agent
from app.models.evaluation import Evaluation, EvaluationAgent, EvaluationRun
from app.models.orchestration import OrchestrationExecution
from app.models.user import User
from app.services import autonomous
from app.services.evaluation.resume_secret import stored_trigger_config
from tests.evaluation_database import (
    _admin,
    _linked_dataset,
    _make_db_full,
    _stub_audit_and_groups,  # noqa: F401 - autouse fixture
)
from tests.evaluation_helpers import seed_provider

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_SKILLS = {"skill-1": 4}


@pytest.fixture(autouse=True)
def _quiet_launches(monkeypatch):
    async def _no_wake():
        return None

    monkeypatch.setattr("app.services.orchestration_engine.notify_orchestration_wake", _no_wake)
    # Each test starts with no refusal already logged.
    monkeypatch.setattr(autonomous, "_scheduled_launch_refusals", {}, raising=False)


async def _scheduled(
    s,
    *,
    family="STIX_BUNDLE",
    dataset_family="STIX_BUNDLE",
    model_ids=("model-a",),
    disabled_model_ids=(),
    trigger_config=None,
    enabled=True,
    use_agent_default_model=False,
    agent_fields=None,
):
    """An admin-owned evaluation due on every tick, one agent, one entry, a provider offering ``model-a``."""
    admin = await _admin(s)
    await seed_provider(s, ["model-a", "model-b"])
    agent = Agent(user_id=admin.id, name="Agent", **(agent_fields or {}))
    s.add(agent)
    await s.flush()
    evaluation = Evaluation(
        name=f"E-{uuid.uuid4().hex[:8]}",
        user_id=admin.id,
        family=family,
        model_ids=list(model_ids),
        disabled_model_ids=list(disabled_model_ids),
        use_agent_default_model=use_agent_default_model,
        enabled=enabled,
        trigger_config=trigger_config or {"schedule": "* * * * *"},
    )
    s.add(evaluation)
    await s.flush()
    s.add(EvaluationAgent(evaluation_id=evaluation.id, agent_id=agent.id))
    await _linked_dataset(s, evaluation, family=dataset_family)
    return admin, agent, evaluation


async def _runs(sm, evaluation_id) -> list[EvaluationRun]:
    async with sm() as s:
        return list(
            (
                await s.execute(select(EvaluationRun).where(EvaluationRun.evaluation_id == evaluation_id))
            ).scalars()
        )


async def _executions(sm) -> int:
    async with sm() as s:
        return await s.scalar(
            select(func.count(OrchestrationExecution.id)).where(OrchestrationExecution.adapter == "evaluation")
        )


# ── The schedule ─────────────────────────────────────────────────────────────


async def test_a_scheduled_run_leaves_out_disabled_models_and_records_skill_versions(monkeypatch):
    """The schedule runs the enabled models only and records the Run button's snapshot."""

    async def _snapshots(_db, agent_ids):
        return {aid: (1, None, None, dict(_SKILLS)) for aid in agent_ids}

    monkeypatch.setattr("app.services.agent_version_snapshots.resolve_agent_version_snapshots", _snapshots)
    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            _admin_user, _agent, evaluation = await _scheduled(
                s, model_ids=("model-a", "model-b"), disabled_model_ids=("model-b",)
            )
            await s.commit()
            assert await autonomous._process_scheduled_evaluations(s) == 1

        runs = await _runs(sm, evaluation.id)
        assert [r.model_id for r in runs] == ["model-a"]
        assert runs[0].skill_versions == _SKILLS
        assert runs[0].trigger_reason == "schedule"
        assert runs[0].owner_execution_id is not None
    finally:
        await cleanup()


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        pytest.param({"family": None}, "no output family", id="no_family"),
        pytest.param({"dataset_family": None}, "have no family set", id="dataset_without_family"),
        pytest.param({"model_ids": ("gone-model",)}, "Model gone-model", id="model_no_provider_offers"),
        pytest.param(
            {"trigger_config": {"schedule": "* * * * *", "await_callback": True}},
            "needs a resume secret",
            id="callback_without_secret",
        ),
    ],
)
async def test_a_scheduled_launch_the_run_button_refuses_creates_nothing(caplog, setup, reason):
    """No run and no execution, and the reason logged once over two ticks."""
    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            _admin_user, _agent, evaluation = await _scheduled(s, **setup)
            await s.commit()
            with caplog.at_level(logging.WARNING, logger=autonomous.logger.name):
                assert await autonomous._process_scheduled_evaluations(s) == 0
                assert await autonomous._process_scheduled_evaluations(s) == 0

        assert await _runs(sm, evaluation.id) == []
        assert await _executions(sm) == 0
        logged = [r.getMessage() for r in caplog.records if "creates no run" in r.getMessage()]
        assert len(logged) == 1
        assert reason in logged[0]
    finally:
        await cleanup()


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        # The agent's own model is whatever it resolves to, never checked against a provider.
        pytest.param(
            {"model_ids": (), "use_agent_default_model": True, "agent_fields": {"llm_model": "own-model"}},
            [("own-model", True)],
            id="agents_own_model",
        ),
        # A model-independent agent calls no model: one run, whatever the models.
        pytest.param(
            {"model_ids": ("gone-model",), "agent_fields": {"skip_llm": True}},
            [("model-independent", False)],
            id="model_independent_agent",
        ),
    ],
)
async def test_a_scheduled_launch_checks_no_model_the_engine_does_not(setup, expected):
    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            _admin_user, _agent, evaluation = await _scheduled(s, **setup)
            await s.commit()
            assert await autonomous._process_scheduled_evaluations(s) == 1

        assert [(r.model_id, r.is_default_model) for r in await _runs(sm, evaluation.id)] == expected
    finally:
        await cleanup()


_OWNER_LOST_ADMIN = [
    pytest.param({"role": "user"}, id="demoted"),
    pytest.param({"is_active": False}, id="deactivated"),
]


@pytest.mark.parametrize("change", _OWNER_LOST_ADMIN)
async def test_a_schedule_stops_once_its_owner_may_no_longer_run_it(caplog, change):
    """The runs act as the owner: an owner no longer an active admin starts none."""
    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            owner, _agent, evaluation = await _scheduled(s)
            for column, value in change.items():
                setattr(owner, column, value)
            await s.commit()
            with caplog.at_level(logging.WARNING, logger=autonomous.logger.name):
                assert await autonomous._process_scheduled_evaluations(s) == 0

        assert await _runs(sm, evaluation.id) == []
        assert await _executions(sm) == 0
        assert any("no longer an active administrator" in r.getMessage() for r in caplog.records)
    finally:
        await cleanup()


@pytest.mark.parametrize("change", _OWNER_LOST_ADMIN)
async def test_another_admin_s_run_button_refuses_it_too(change):
    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            owner, _agent, evaluation = await _scheduled(s)
            for column, value in change.items():
                setattr(owner, column, value)
            other = User(email="b@x.io", full_name="Other", hashed_password="x", role="admin")
            s.add(other)
            await s.commit()
            with pytest.raises(HTTPException) as refused:
                await evaluations_api.trigger_evaluation(evaluation_id=str(evaluation.id), body=None, user=other, db=s)

        assert refused.value.status_code == 409
        assert await _runs(sm, evaluation.id) == []
    finally:
        await cleanup()


async def test_an_ownerless_evaluation_has_no_owner_to_check():
    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            _admin_user, _agent, evaluation = await _scheduled(s)
            evaluation.user_id = None
            await s.commit()
            assert await autonomous._process_scheduled_evaluations(s) == 1
    finally:
        await cleanup()


async def test_a_refused_window_starts_once_its_cause_is_fixed():
    """The window stays due: the tick after the family is set starts it."""
    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            _admin_user, _agent, evaluation = await _scheduled(s, family=None)
            await s.commit()
            assert await autonomous._process_scheduled_evaluations(s) == 0
            stored = await s.get(Evaluation, evaluation.id)
            assert stored.last_triggered_at is None
            stored.family = "STIX_BUNDLE"
            await s.commit()
            assert await autonomous._process_scheduled_evaluations(s) == 1

        assert len(await _runs(sm, evaluation.id)) == 1
    finally:
        await cleanup()


async def test_automatic_runs_paused_skips_the_schedule():
    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            _admin_user, _agent, evaluation = await _scheduled(s, enabled=False)
            await s.commit()
            assert await autonomous._process_scheduled_evaluations(s) == 0

        assert await _runs(sm, evaluation.id) == []
    finally:
        await cleanup()


# ── A run a person asks for ──────────────────────────────────────────────────


async def test_automatic_runs_paused_never_blocks_the_run_button():
    """``enabled`` pauses automatic runs only: the Run button and the API still start one."""
    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin, _agent, evaluation = await _scheduled(s, enabled=False)
            await s.commit()
            created = await evaluations_api.trigger_evaluation(
                evaluation_id=str(evaluation.id), body=None, user=admin, db=s
            )

        assert [r["model_id"] for r in created] == ["model-a"]
    finally:
        await cleanup()


async def _completed_run(s, evaluation, agent, model_id) -> EvaluationRun:
    run = EvaluationRun(
        evaluation_id=evaluation.id,
        agent_id=agent.id,
        model_id=model_id,
        status="completed",
        trigger_reason="manual",
        total_entries=1,
    )
    s.add(run)
    await s.commit()
    return run


@pytest.mark.parametrize("endpoint", ["replay", "bulk_rerun"])
async def test_a_rerun_on_a_model_no_provider_offers_is_refused_up_front(endpoint):
    """Replay and bulk "Re-run full" refuse it with a 422, as the Run button does."""
    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin, agent, evaluation = await _scheduled(s)
            run = await _completed_run(s, evaluation, agent, "gone-model")
            if endpoint == "replay":
                call = evaluations_api.replay_run(run_id=str(run.id), user=admin, db=s)
            else:
                call = evaluations_api.trigger_evaluation_from_runs(
                    evaluation_id=str(evaluation.id),
                    body=TriggerEvaluationFromRunsRequest(run_ids=[str(run.id)]),
                    user=admin,
                    db=s,
                )
            with pytest.raises(HTTPException) as exc:
                await call

        assert exc.value.status_code == 422
        assert "gone-model" in exc.value.detail
        assert len(await _runs(sm, evaluation.id)) == 1
    finally:
        await cleanup()


@pytest.mark.parametrize("endpoint", ["replay", "bulk_rerun"])
async def test_a_rerun_on_the_agents_own_model_is_not_checked(endpoint):
    """The agent's own model is whatever it resolves to now, as the engine reads it."""
    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin, agent, evaluation = await _scheduled(s)
            run = await _completed_run(s, evaluation, agent, "gone-model")
            run.is_default_model = True
            await s.commit()
            if endpoint == "replay":
                await evaluations_api.replay_run(run_id=str(run.id), user=admin, db=s)
            else:
                await evaluations_api.trigger_evaluation_from_runs(
                    evaluation_id=str(evaluation.id),
                    body=TriggerEvaluationFromRunsRequest(run_ids=[str(run.id)]),
                    user=admin,
                    db=s,
                )

        assert len(await _runs(sm, evaluation.id)) == 2
    finally:
        await cleanup()


async def test_callback_mode_with_a_secret_is_launchable():
    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            config = stored_trigger_config({"schedule": "* * * * *", "await_callback": True, "resume_secret": "s3cret"})
            await _scheduled(s, trigger_config=config)
            await s.commit()
            assert await autonomous._process_scheduled_evaluations(s) == 1
    finally:
        await cleanup()


async def test_the_run_button_reads_the_owners_providers_once(monkeypatch):
    """Extra models are checked against the routing read for them, not a second read."""
    from app.api.v1.schemas.evaluation import TriggerEvaluationRequest
    from app.services import evaluation_engine

    reads: list[object] = []
    real_routing = evaluation_engine.owner_model_routing

    async def _counted(db, owner_id):
        reads.append(owner_id)
        return await real_routing(db, owner_id)

    monkeypatch.setattr(evaluation_engine, "owner_model_routing", _counted)
    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin, _agent, evaluation = await _scheduled(s)
            await s.commit()
            created = await evaluations_api.trigger_evaluation(
                evaluation_id=str(evaluation.id),
                body=TriggerEvaluationRequest(model_ids=["model-a", "model-b"]),
                user=admin,
                db=s,
            )

        assert sorted(r["model_id"] for r in created) == ["model-a", "model-b"]
        assert len(reads) == 1
    finally:
        await cleanup()
