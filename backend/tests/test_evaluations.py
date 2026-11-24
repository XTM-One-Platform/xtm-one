"""Evaluation API routes against a real (throwaway) PostgreSQL database.

Starts with the dataset-family gate: ``_require_dataset_families_set`` sits beside
``_require_runnable_family`` at every trigger, re-run, rescore and replay call site, so
an evaluation cannot run while a linked dataset has no declared family. The trigger,
replay and rescore endpoints are called end to end: refused while the dataset is
family-less, and reaching their own next answer once the family is set (trigger's 400,
replay's pending run, rescore's 409).
``test_evaluations_trigger_from_runs_api.py`` covers ``trigger_evaluation_from_runs``.

Then the reuse/params collision, the execution staged atomically with its runs,
``cancel_run``, and the admin lookups for the prompt guard and the always-ask tools.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

import app.models  # noqa: F401 - register model metadata
from app.api.v1 import evaluations as evaluations_api
from app.api.v1.schemas.evaluation import (
    TriggerEvaluationFromRunsRequest,
    TriggerEvaluationRequest,
)
from app.models.agent import Agent
from app.models.dataset import DatasetEntry
from app.models.dataset_version import DatasetVersion, DatasetVersionEntry
from app.models.evaluation import (
    Evaluation,
    EvaluationAgent,
    EvaluationEpisode,
    EvaluationResult,
    EvaluationRun,
)
from app.models.evaluation_param import EvaluationRunParam
from app.models.user import User
from app.services.evaluation.resume_secret import stored_trigger_config
from tests.evaluation_database import (
    _admin,
    _linked_dataset,
    _make_db,
    _make_db_full,
    _run_status,
    _stranded_fixture,
    _stub_audit_and_groups,  # noqa: F401 - autouse fixture
)
from tests.evaluation_helpers import seed_provider

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _enqueue_noop(monkeypatch):
    async def _noop(*_a, **_k):
        # Must carry an ``id``: callers stamp it onto every run they create as
        # ``owner_execution_id`` in the same transaction, so a double
        # returning None breaks the endpoint rather than the assertion.
        return SimpleNamespace(id=uuid.uuid4())

    # The trigger and replay endpoints stage the execution on the caller's own
    # session.
    monkeypatch.setattr(
        "app.services.evaluation_engine.stage_evaluation_execution", _noop
    )


# ------------------------------------------------------------------- trigger_evaluation


@pytest.mark.parametrize("family", [None, "STIX_BUNDLE"], ids=["family_unset", "family_set"])
@pytest.mark.parametrize("endpoint", ["trigger", "replay", "rescore"])
async def test_every_launch_endpoint_is_gated_on_the_dataset_family(
    monkeypatch, endpoint, family
):
    """Trigger, replay and rescore refuse while a linked dataset has no family.

    The refusal is a 400 naming the dataset. Once the family is set, each endpoint
    reaches its own next answer, which proves the gate cleared: trigger's "no agents"
    400, replay's ``pending`` run, rescore's 409 for a run with no stored predictions.
    """
    await _enqueue_noop(monkeypatch)
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(name="Eval", family="STIX_BUNDLE")
            s.add(evaluation)
            await s.flush()
            run = None
            if endpoint != "trigger":
                agent = Agent(user_id=admin.id, name="Agent")
                s.add(agent)
                await s.flush()
                run = EvaluationRun(
                    evaluation_id=evaluation.id,
                    agent_id=agent.id,
                    model_id="gpt-4.1",
                    # The agent's own model, which no provider check concerns:
                    # this test is about the dataset family gate.
                    is_default_model=True,
                    status="completed",
                    trigger_reason="manual",
                )
                s.add(run)
                await s.flush()
            ds = await _linked_dataset(s, evaluation, family=family)
            await s.commit()

            if endpoint == "trigger":
                call = evaluations_api.trigger_evaluation(
                    evaluation_id=str(evaluation.id), body=None, user=admin, db=s
                )
            elif endpoint == "replay":
                call = evaluations_api.replay_run(run_id=str(run.id), user=admin, db=s)
            else:
                call = evaluations_api.rescore_evaluation_run(
                    run_id=str(run.id), user=admin, db=s
                )

            if family is None:
                with pytest.raises(HTTPException) as exc:
                    await call
                assert exc.value.status_code == 400
                assert ds.name in exc.value.detail
            elif endpoint == "replay":
                resp = await call
                assert resp["status"] == "pending"
            else:
                with pytest.raises(HTTPException) as exc:
                    await call
                if endpoint == "trigger":
                    assert exc.value.status_code == 400
                    assert "no agents" in exc.value.detail.lower()
                else:
                    assert exc.value.status_code == 409
    finally:
        await cleanup()


def _gone(*models: str) -> tuple[int, str]:
    if len(models) > 1:
        return 422, (
            f"Models {', '.join(models)} are not chat models, or no provider this evaluation can use offers them"
        )
    return 422, f"Model {models[0]} is not a chat model, or no provider this evaluation can use offers it"


@pytest.mark.parametrize(
    ("case", "refusal", "runs"),
    [
        ({"models": ["owner-model"]}, None, [("owner-model", False)]),
        ({"models": ["admin-model"]}, _gone("admin-model"), []),
        ({"models": ["admin-model", "gone-model"]}, _gone("admin-model", "gone-model"), []),
        ({"models": ["gpt-5.9-preview"]}, None, [("gpt-5.9-preview", False)]),
        ({"models": ["gpt-image-1"]}, _gone("gpt-image-1"), []),
        ({"models": ["owner-model", "gone-model"], "disabled": ["gone-model"]}, None, [("owner-model", False)]),
        ({"models": ["gone-model"], "agents": [True]}, None, [("model-independent", False)]),
        ({"models": ["gone-model"], "agents": [True, False]}, _gone("gone-model"), []),
        (
            {"models": ["admin-model"], "agent_model": "admin-model", "use_default": True},
            None,
            [("admin-model", True)],
        ),
        ({"models": ["admin-model"], "agent_model": "admin-model"}, _gone("admin-model"), []),
        ({"models": ["owner-model"], "ownerless": True}, _gone("owner-model"), []),
        ({"models": ["admin-model"], "ownerless": True}, _gone("admin-model"), []),
        ({"models": ["open-model"], "ownerless": True}, None, [("open-model", False)]),
        ({"models": ["owner-model"], "extra": ["owner-extra"]}, None, [("owner-extra", False)]),
        (
            {"models": ["owner-model"], "extra": ["admin-model"]},
            (400, "Selected model_ids must be enabled additional models or available platform models: admin-model"),
            [],
        ),
    ],
    ids=[
        "offered-to-the-owner",
        "offered-to-the-admin-only",
        "two-models-no-one-offers",
        "owned-by-its-vendor-prefix",
        "not-a-chat-model",
        "a-disabled-model-not-picked",
        "a-model-independent-agent",
        "a-model-independent-agent-beside-another",
        "the-agents-own-default",
        "the-agents-model-with-its-default-off",
        "ownerless-on-a-restricted-provider",
        "ownerless-on-the-admins-provider",
        "ownerless-on-an-open-provider",
        "an-extra-model-the-owner-is-offered",
        "an-extra-model-only-the-admin-is-offered",
    ],
)
async def test_a_trigger_refuses_a_model_no_provider_of_the_owner_would_answer(monkeypatch, case, refusal, runs):
    """A saved model no provider of the owner would answer is refused up front with
    a 422 naming it, and no run is created. The rule is the resolver's: a
    chat model the owner's providers own, by catalog or vendor prefix. The owner's
    providers decide, not those of the admin who triggers, and an ownerless
    evaluation is checked as nobody, as its runs execute. Checked per agent, as
    the run is: an agent's own default model, flagged so on its run, and a
    model-independent agent are not. An extra model the request adds must be one
    the owner is offered (400)."""
    from app.llm.provider_access import acting_for

    await _enqueue_noop(monkeypatch)
    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin = await _admin(s)
            owner = User(email="o@x.io", full_name="Owner", hashed_password="x", role="admin")
            s.add(owner)
            await s.flush()
            await seed_provider(s, ["owner-model", "owner-extra"], users=[owner])
            await seed_provider(s, ["admin-model"], users=[admin], provider="mistral")
            await seed_provider(s, ["open-model"], provider="groq")
            evaluation = Evaluation(
                name="Eval",
                family="STIX_BUNDLE",
                user_id=None if case.get("ownerless") else owner.id,
                model_ids=case["models"],
                disabled_model_ids=case.get("disabled", []),
                use_agent_default_model=bool(case.get("use_default")),
            )
            s.add(evaluation)
            await s.flush()
            await _linked_dataset(s, evaluation, family="STIX_BUNDLE")
            agents = [
                Agent(user_id=owner.id, name=f"Agent {i}", skip_llm=skip_llm, llm_model=case.get("agent_model"))
                for i, skip_llm in enumerate(case.get("agents", [False]))
            ]
            s.add_all(agents)
            await s.flush()
            s.add_all(EvaluationAgent(evaluation_id=evaluation.id, agent_id=agent.id) for agent in agents)
            await s.commit()

            body = TriggerEvaluationRequest(model_ids=case["extra"]) if "extra" in case else TriggerEvaluationRequest()
            # The HTTP route binds the authenticated admin as the acting user.
            with acting_for(admin.id):
                trigger = evaluations_api.trigger_evaluation(evaluation_id=str(evaluation.id), body=body, user=admin, db=s)
                if refusal:
                    with pytest.raises(HTTPException) as exc:
                        await trigger
                    assert (exc.value.status_code, exc.value.detail) == refusal
                else:
                    await trigger

            created = (
                await s.execute(
                    select(EvaluationRun.model_id, EvaluationRun.is_default_model).where(
                        EvaluationRun.evaluation_id == evaluation.id
                    )
                )
            ).all()
            assert sorted(set(map(tuple, created))) == runs
    finally:
        await cleanup()


@pytest.mark.parametrize("secret", [False, True], ids=["no-secret", "secret-set"])
@pytest.mark.parametrize("endpoint", ["trigger", "replay", "from-runs", "rescore"])
async def test_a_launch_waiting_for_callbacks_needs_a_resume_secret(monkeypatch, endpoint, secret):
    """An evaluation in callback mode with no resume secret cannot be resumed: every
    entry would park until its callback timeout, after the agent's first turn ran
    (an imported evaluation keeps ``await_callback`` and loses its secret).
    Trigger, replay and re-run from runs refuse it with a 400; a rescore calls no
    agent and is not gated. With a secret, each endpoint reaches its own next answer:
    trigger's "no agents" 400, a replay's or a re-run's pending run, rescore's 409."""
    await _enqueue_noop(monkeypatch)
    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(
                name="Eval",
                family="STIX_BUNDLE",
                trigger_config=stored_trigger_config(
                    {"await_callback": True, **({"resume_secret": "s3cret"} if secret else {})}
                ),
            )
            s.add(evaluation)
            await s.flush()
            run = None
            if endpoint != "trigger":
                agent = Agent(user_id=admin.id, name="Agent", skip_llm=True)
                s.add(agent)
                await s.flush()
                s.add(EvaluationAgent(evaluation_id=evaluation.id, agent_id=agent.id))
                run = EvaluationRun(
                    evaluation_id=evaluation.id,
                    agent_id=agent.id,
                    model_id="model-independent",
                    status="completed",
                    trigger_reason="manual",
                )
                s.add(run)
                await s.flush()
            await _linked_dataset(s, evaluation, family="STIX_BUNDLE")
            await s.commit()

            if endpoint == "trigger":
                call = evaluations_api.trigger_evaluation(evaluation_id=str(evaluation.id), body=None, user=admin, db=s)
            elif endpoint == "replay":
                call = evaluations_api.replay_run(run_id=str(run.id), user=admin, db=s)
            elif endpoint == "from-runs":
                call = evaluations_api.trigger_evaluation_from_runs(
                    evaluation_id=str(evaluation.id),
                    body=TriggerEvaluationFromRunsRequest(run_ids=[str(run.id)]),
                    user=admin,
                    db=s,
                )
            else:
                call = evaluations_api.rescore_evaluation_run(run_id=str(run.id), user=admin, db=s)

            if not secret and endpoint != "rescore":
                with pytest.raises(HTTPException) as exc:
                    await call
                assert (exc.value.status_code, exc.value.detail) == (
                    400,
                    "Callback mode needs a resume secret: set trigger_config.resume_secret "
                    "before running this evaluation.",
                )
            elif endpoint == "replay":
                assert (await call)["status"] == "pending"
            elif endpoint == "from-runs":
                assert [r["status"] for r in await call] == ["pending"]
            else:
                with pytest.raises(HTTPException) as exc:
                    await call
                if endpoint == "trigger":
                    assert exc.value.status_code == 400
                    assert "no agents" in exc.value.detail.lower()
                else:
                    assert exc.value.status_code == 409
    finally:
        await cleanup()


# ------------------------------------------------- a model no provider offers, on save


async def _owner_admin_and_providers(s) -> tuple[User, User]:
    """An admin and an owner, each with a restricted provider of their own, and an open one."""
    admin = await _admin(s)
    owner = User(email="o@x.io", full_name="Owner", hashed_password="x", role="admin")
    s.add(owner)
    await s.flush()
    await seed_provider(s, ["owner-model"], users=[owner])
    await seed_provider(s, ["admin-model"], users=[admin], provider="mistral")
    await seed_provider(s, ["open-model"], provider="groq")
    await s.commit()
    return admin, owner


@pytest.mark.parametrize(
    ("models", "refusal"),
    [
        (["admin-model", "open-model"], None),
        (["owner-model"], _gone("owner-model")),
        (["admin-model", "gone-model", "gpt-image-1"], _gone("gone-model", "gpt-image-1")),
    ],
    ids=["offered-to-the-creator", "offered-to-someone-else-only", "unknown-and-not-a-chat-model"],
)
async def test_create_refuses_a_model_no_provider_of_its_creator_offers(models, refusal):
    """The creator owns the evaluation, so their providers decide, with the launch's
    rule and wording; a refused create saves nothing."""
    from sqlalchemy import func

    from app.api.v1.schemas.evaluation import CreateEvaluationRequest
    from app.llm.provider_access import acting_for

    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin, _owner = await _owner_admin_and_providers(s)
            create = evaluations_api.create_evaluation(
                body=CreateEvaluationRequest(name="Eval", model_ids=models, scorer_selection=[]),
                user=admin,
                db=s,
                _agentic_gate=None,
            )
            with acting_for(admin.id):
                if refusal:
                    with pytest.raises(HTTPException) as exc:
                        await create
                    assert (exc.value.status_code, exc.value.detail) == refusal
                    await s.rollback()
                    assert await s.scalar(select(func.count()).select_from(Evaluation)) == 0
                else:
                    created = await create
                    assert created["model_ids"] == models
                    assert created["unavailable_model_ids"] == []
    finally:
        await cleanup()


@pytest.mark.parametrize(
    ("models", "refusal"),
    [
        (["owner-model", "gone-model"], None),
        (["owner-model"], None),
        (["owner-model", "gone-model", "gpt-5.9-preview", "open-model"], None),
        (["owner-model", "gone-model", "admin-model"], _gone("admin-model")),
        (["owner-model", "gone-model", "gone-2"], _gone("gone-2")),
    ],
    ids=[
        "the-saved-models-sent-again",
        "a-saved-model-removed",
        "owned-by-its-vendor-prefix-or-open",
        "offered-to-the-admin-who-saves-only",
        "a-new-model-no-one-offers",
    ],
)
async def test_an_update_refuses_only_a_model_it_adds_that_no_provider_of_the_owner_offers(models, refusal):
    """A saved model is never re-checked, so an evaluation whose provider went away
    stays editable: the Configuration tab sends every model on each save. A model
    the update adds is checked on the owner's providers, not on those of the admin
    who saves; a refused update changes nothing."""
    from app.api.v1.schemas.evaluation import UpdateEvaluationRequest
    from app.llm.provider_access import acting_for

    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin, owner = await _owner_admin_and_providers(s)
            evaluation = Evaluation(name="Eval", user_id=owner.id, model_ids=["owner-model", "gone-model"])
            s.add(evaluation)
            await s.commit()
            evaluation_id = evaluation.id

            update = evaluations_api.update_evaluation(
                evaluation_id=str(evaluation_id),
                body=UpdateEvaluationRequest(name="Renamed", model_ids=models),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            with acting_for(admin.id):
                if refusal:
                    with pytest.raises(HTTPException) as exc:
                        await update
                    assert (exc.value.status_code, exc.value.detail) == refusal
                    await s.rollback()
                    stored = await s.get(Evaluation, evaluation_id)
                    await s.refresh(stored)
                    assert (stored.name, stored.model_ids) == ("Eval", ["owner-model", "gone-model"])
                else:
                    updated = await update
                    assert (updated["name"], updated["model_ids"]) == ("Renamed", models)
    finally:
        await cleanup()


async def test_the_response_flags_the_saved_models_no_provider_of_the_owner_offers():
    """Read with each response, never stored: the owner's providers decide,
    on the detail and on the list, and a provider added later clears the flag.
    A disabled model is flagged too: enabling it would make every launch refuse it."""
    from app.llm.provider_access import acting_for

    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin, owner = await _owner_admin_and_providers(s)
            owned = Evaluation(
                name="Owned",
                user_id=owner.id,
                model_ids=["owner-model", "admin-model", "gone-model", "gpt-5.9-preview"],
                disabled_model_ids=["gone-model"],
            )
            mine = Evaluation(name="Mine", user_id=admin.id, model_ids=["admin-model", "owner-model"])
            no_model = Evaluation(name="No model", user_id=owner.id, model_ids=[])
            s.add_all([owned, mine, no_model])
            await s.commit()
            owned_id = str(owned.id)

            async def flags() -> tuple[dict[str, list[str]], list[str]]:
                listed = await evaluations_api.list_evaluations(agent_id=None, scope="all", user=admin, db=s)
                detail = await evaluations_api.get_evaluation(evaluation_id=owned_id, user=admin, db=s)
                return {e["name"]: e["unavailable_model_ids"] for e in listed}, detail["unavailable_model_ids"]

            with acting_for(admin.id):
                assert await flags() == (
                    {"Owned": ["admin-model", "gone-model"], "Mine": ["owner-model"], "No model": []},
                    ["admin-model", "gone-model"],
                )

                await seed_provider(s, ["gone-model"], provider="groq")
                await s.commit()
                assert await flags() == (
                    {"Owned": ["admin-model"], "Mine": ["owner-model"], "No model": []},
                    ["admin-model"],
                )
    finally:
        await cleanup()


async def test_the_models_an_evaluation_offers_to_add_are_its_owners():
    """What its pickers list: the owner's chat models, those its save and its
    launches accept, not the models of the admin who edits it."""
    from app.llm.provider_access import acting_for

    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin, owner = await _owner_admin_and_providers(s)
            await seed_provider(s, ["gpt-image-1"], users=[owner])
            evaluation = Evaluation(name="Eval", user_id=owner.id, model_ids=["owner-model"])
            s.add(evaluation)
            await s.commit()

            with acting_for(admin.id):
                offered = await evaluations_api.list_evaluation_available_models(
                    evaluation_id=str(evaluation.id), user=admin, db=s
                )
        assert sorted((m.model, m.provider) for m in offered) == [("open-model", "groq"), ("owner-model", "openai")]
    finally:
        await cleanup()


async def test_a_copy_keeps_the_models_its_new_owner_is_not_offered_and_warns():
    """A copy is owned by the admin who copies it: a model only the source's
    owner is offered is kept and flagged, as on import, and the copy says so."""
    from app.llm.provider_access import acting_for

    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin, owner = await _owner_admin_and_providers(s)
            source = Evaluation(name="Eval", user_id=owner.id, model_ids=["owner-model", "open-model"])
            s.add(source)
            await s.commit()

            with acting_for(admin.id):
                copied = await evaluations_api.copy_evaluation(evaluation_id=str(source.id), user=admin, db=s)
        assert copied["model_ids"] == ["owner-model", "open-model"]
        assert copied["unavailable_model_ids"] == ["owner-model"]
        assert copied["warnings"] == ["Model owner-model is kept but cannot run: no provider you can use offers it"]
    finally:
        await cleanup()


# ------------------------------------------------------------------------------ replay_run


async def test_replay_total_entries_comes_from_the_pinned_snapshot_not_the_live_count(
    monkeypatch,
):
    """A replayed run counts its total_entries from the original's pinned snapshot,
    not the live dataset.

    With the live count, completed_entries could never reach total_entries (or
    reach it early), and the replay would stay "running" forever.
    """
    await _enqueue_noop(monkeypatch)
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(name="Eval", family="STIX_BUNDLE")
            s.add(evaluation)
            await s.flush()
            agent = Agent(user_id=admin.id, name="Agent")
            s.add(agent)
            await s.flush()
            ds = await _linked_dataset(s, evaluation, family="STIX_BUNDLE")

            # A 2-entry snapshot, then a live dataset grown to 3: the two counts
            # disagree.
            version = DatasetVersion(
                dataset_id=ds.id,
                version_number=1,
                content_hash="deadbeef",
                entry_count=2,
            )
            s.add(version)
            await s.flush()
            s.add(
                DatasetVersionEntry(
                    version_id=version.id, position=0, expected_answer="a"
                )
            )
            s.add(
                DatasetVersionEntry(
                    version_id=version.id, position=1, expected_answer="b"
                )
            )
            s.add(DatasetEntry(dataset_id=ds.id, position=1, expected_answer="c"))
            await s.commit()

            original = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4.1",
                # The agent's own model, which no provider check concerns.
                is_default_model=True,
                status="completed",
                trigger_reason="manual",
                total_entries=2,
                pinned_dataset_versions={str(ds.id): 1},
            )
            s.add(original)
            await s.commit()

            resp = await evaluations_api.replay_run(
                run_id=str(original.id), user=admin, db=s
            )
            assert resp["status"] == "pending"
            assert resp["total_entries"] == 2
    finally:
        await cleanup()


# ------------------------------------------------------------------- rescore_evaluation_run


@pytest.mark.parametrize("in_progress_status", ["pending", "running"])
async def test_rescore_refused_while_run_already_in_progress(in_progress_status):
    """A ``pending`` or ``running`` run is not re-queued, so clicking rescore twice
    never schedules duplicate work for one run."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(name="Eval", family="STIX_BUNDLE")
            s.add(evaluation)
            await s.flush()
            agent = Agent(user_id=admin.id, name="Agent")
            s.add(agent)
            await s.flush()
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4.1",
                status=in_progress_status,
                trigger_reason="manual",
            )
            s.add(run)
            await s.flush()
            await _linked_dataset(s, evaluation, family="STIX_BUNDLE")
            await s.commit()

            with pytest.raises(HTTPException) as exc:
                await evaluations_api.rescore_evaluation_run(
                    run_id=str(run.id), user=admin, db=s
                )
            assert exc.value.status_code == 409
            assert "already in progress" in exc.value.detail
            # Refused before any mutation -- status is untouched.
            assert run.status == in_progress_status
    finally:
        await cleanup()


# --------------------------------------------------- reuse/params collision


async def test_trigger_evaluation_reuse_param_collision_is_deduplicated(monkeypatch):
    """``params={"reuse": ...}`` plus the ``reuse`` override write one row, not two.

    ``body.params`` accepts any key, and ``capture_params_for_new_runs`` is a
    blind batch insert: two inserts under one ``(run_id, "reuse")`` key violate
    the unique constraint. The explicit ``reuse`` field wins.
    """
    await _enqueue_noop(monkeypatch)
    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(
                name="Eval",
                family="STIX_BUNDLE",
                model_ids=["gpt-4.1"],
                use_agent_default_model=False,
            )
            s.add(evaluation)
            await s.flush()
            await _linked_dataset(s, evaluation, family="STIX_BUNDLE")
            # skip_llm collapses the agent to a single sentinel run,
            # so this exercises the real trigger path without depending on
            # any LLM provider/model resolution.
            agent = Agent(user_id=admin.id, name="Agent", skip_llm=True)
            s.add(agent)
            await s.flush()
            s.add(EvaluationAgent(evaluation_id=evaluation.id, agent_id=agent.id))
            await s.commit()

            body = TriggerEvaluationRequest(
                params={"reuse": "on", "note": "manual-note"}, reuse="on"
            )
            await evaluations_api.trigger_evaluation(
                evaluation_id=str(evaluation.id), body=body, user=admin, db=s
            )

            runs = (
                await s.execute(
                    select(EvaluationRun).where(
                        EvaluationRun.evaluation_id == evaluation.id
                    )
                )
            ).scalars().all()
            assert len(runs) == 1

            rows = (
                await s.execute(
                    select(EvaluationRunParam).where(
                        EvaluationRunParam.run_id == runs[0].id
                    )
                )
            ).scalars().all()
            by_key = {r.key: r for r in rows}
            # One row per key. Both values are "on" here; the source must still
            # reflect the single merged write.
            assert by_key["reuse"].value == "on"
            assert by_key["reuse"].source == "manual"
            assert by_key["note"].value == "manual-note"
    finally:
        await cleanup()


async def test_trigger_tags_drop_the_reserved_baseline_key_and_stringify_values(monkeypatch):
    """Tags given at trigger time go through the same rule as every tag write.

    ``baseline`` is set only on the evaluation's agent, so a trigger tag cannot claim it,
    and every value is stored as the string the run's tags are compared as.
    """
    await _enqueue_noop(monkeypatch)
    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(
                name="Eval",
                family="STIX_BUNDLE",
                model_ids=["gpt-4.1"],
                use_agent_default_model=False,
            )
            s.add(evaluation)
            await s.flush()
            await _linked_dataset(s, evaluation, family="STIX_BUNDLE")
            agent = Agent(user_id=admin.id, name="Agent", skip_llm=True)
            s.add(agent)
            await s.flush()
            s.add(EvaluationAgent(evaluation_id=evaluation.id, agent_id=agent.id))
            await s.commit()

            body = TriggerEvaluationRequest(
                tags={"baseline": True, "sprint": 12, "nightly": False}
            )
            await evaluations_api.trigger_evaluation(
                evaluation_id=str(evaluation.id), body=body, user=admin, db=s
            )

            run = (
                await s.execute(
                    select(EvaluationRun).where(
                        EvaluationRun.evaluation_id == evaluation.id
                    )
                )
            ).scalar_one()
            assert run.tags == {"sprint": "12", "nightly": "false"}
    finally:
        await cleanup()


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"params": {"temperature": {"value": 0.2}}}, "params must be a flat key/value object"),
        ({"reuse": "sometimes"}, "reuse must be one of: inherit, on, off"),
    ],
    ids=["nested-params", "unknown-reuse"],
)
async def test_a_trigger_refuses_params_and_reuse_its_contract_does_not_take(monkeypatch, body, detail):
    """The trigger body documented in contracts/api.md refuses nested ``params`` and an
    unknown ``reuse`` (422), and the refused launch leaves no run behind."""
    from fastapi import HTTPException

    await _enqueue_noop(monkeypatch)
    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(
                name="Eval",
                family="STIX_BUNDLE",
                model_ids=["gpt-4.1"],
                use_agent_default_model=False,
            )
            s.add(evaluation)
            await s.flush()
            await _linked_dataset(s, evaluation, family="STIX_BUNDLE")
            agent = Agent(user_id=admin.id, name="Agent", skip_llm=True)
            s.add(agent)
            await s.flush()
            s.add(EvaluationAgent(evaluation_id=evaluation.id, agent_id=agent.id))
            await s.commit()
            evaluation_id = evaluation.id

            with pytest.raises(HTTPException) as refused:
                await evaluations_api.trigger_evaluation(
                    evaluation_id=str(evaluation_id),
                    body=TriggerEvaluationRequest(**body),
                    user=admin,
                    db=s,
                )
            await s.rollback()

        async with sm() as s:
            runs = (
                await s.execute(select(EvaluationRun).where(EvaluationRun.evaluation_id == evaluation_id))
            ).scalars().all()

        assert refused.value.status_code == 422
        assert refused.value.detail == detail
        assert runs == []
    finally:
        await cleanup()


async def test_trigger_evaluation_reuse_override_wins_over_conflicting_params_value(
    monkeypatch,
):
    """When ``params["reuse"]`` and the ``reuse`` override disagree, the override
    wins, the precedence ``capture_params``/``upsert_param`` give a manual override.
    """
    await _enqueue_noop(monkeypatch)
    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(
                name="Eval",
                family="STIX_BUNDLE",
                model_ids=["gpt-4.1"],
                use_agent_default_model=False,
            )
            s.add(evaluation)
            await s.flush()
            await _linked_dataset(s, evaluation, family="STIX_BUNDLE")
            agent = Agent(user_id=admin.id, name="Agent", skip_llm=True)
            s.add(agent)
            await s.flush()
            s.add(EvaluationAgent(evaluation_id=evaluation.id, agent_id=agent.id))
            await s.commit()

            body = TriggerEvaluationRequest(params={"reuse": "off"}, reuse="on")
            await evaluations_api.trigger_evaluation(
                evaluation_id=str(evaluation.id), body=body, user=admin, db=s
            )

            runs = (
                await s.execute(
                    select(EvaluationRun).where(
                        EvaluationRun.evaluation_id == evaluation.id
                    )
                )
            ).scalars().all()
            assert len(runs) == 1

            rows = (
                await s.execute(
                    select(EvaluationRunParam).where(
                        EvaluationRunParam.run_id == runs[0].id
                    )
                )
            ).scalars().all()
            by_key = {r.key: r for r in rows}
            assert by_key["reuse"].value == "on"
    finally:
        await cleanup()


# ------------------------------------------------- atomic execution staging


async def test_stage_evaluation_execution_only_flushes_not_commits():
    """``stage_evaluation_execution`` only flushes on the CALLER's session.

    Rolling back the caller's transaction discards the staged execution, with no
    orphaned row left behind.
    """
    from app.models.orchestration import OrchestrationExecution
    from app.services.evaluation_engine import stage_evaluation_execution

    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(name="Eval", family="STIX_BUNDLE")
            s.add(evaluation)
            await s.flush()
            agent = Agent(user_id=admin.id, name="Agent")
            s.add(agent)
            await s.flush()
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4.1",
                status="pending",
                trigger_reason="manual",
            )
            s.add(run)
            await s.flush()

            await stage_evaluation_execution(s, str(evaluation.id))

            # Visible within the same uncommitted transaction (flushed) ...
            staged = (
                await s.execute(
                    select(OrchestrationExecution).where(
                        OrchestrationExecution.adapter == "evaluation"
                    )
                )
            ).scalars().all()
            assert len(staged) == 1

            # ... and discarded with its run on rollback: one transaction, no
            # isolated commit of its own.
            await s.rollback()

        async with sm() as s2:
            evaluations = (
                await s2.execute(select(Evaluation).where(Evaluation.id == evaluation.id))
            ).scalars().all()
            runs = (
                await s2.execute(
                    select(EvaluationRun).where(EvaluationRun.id == run.id)
                )
            ).scalars().all()
            executions = (
                await s2.execute(
                    select(OrchestrationExecution).where(
                        OrchestrationExecution.adapter == "evaluation"
                    )
                )
            ).scalars().all()
            assert evaluations == []
            assert runs == []
            assert executions == []
    finally:
        await cleanup()


async def test_trigger_evaluation_stages_execution_atomically_with_the_run(
    monkeypatch,
):
    """``trigger_evaluation`` commits exactly one ``OrchestrationExecution`` with the
    new run, in the endpoint's own commit, and the run is committed owned by
    that execution.

    A ``pending`` run with no owner would be swept by any other execution's cleanup.
    """
    from app.models.orchestration import OrchestrationExecution
    from app.services.evaluation_engine import (
        stage_evaluation_execution as real_stage_evaluation_execution,
    )

    # notify_orchestration_wake() is best-effort, but this environment has no
    # Redis: stub it.
    async def _noop_wake():
        return None

    monkeypatch.setattr(
        "app.services.orchestration_engine.notify_orchestration_wake", _noop_wake
    )

    # The end state alone would also come from a two-phase commit through the
    # isolated ``enqueue_evaluation`` wrapper. The spy proves staging
    # runs on the ENDPOINT's session and that the wrapper is never called.
    stage_calls: list[object] = []
    staged: list = []

    async def _spy_stage_evaluation_execution(db, evaluation_id, **kwargs):
        stage_calls.append(db)
        execution = await real_stage_evaluation_execution(db, evaluation_id, **kwargs)
        staged.append(execution)
        return execution

    monkeypatch.setattr(
        "app.services.evaluation_engine.stage_evaluation_execution",
        _spy_stage_evaluation_execution,
    )

    async def _fail_if_called(*args, **kwargs):
        raise AssertionError(
            "trigger_evaluation must stage the execution on its own session, "
            "not fall back to the isolated enqueue_evaluation wrapper"
        )

    monkeypatch.setattr(
        "app.services.evaluation_engine.enqueue_evaluation", _fail_if_called
    )

    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(
                name="Eval",
                family="STIX_BUNDLE",
                model_ids=["gpt-4.1"],
                use_agent_default_model=False,
            )
            s.add(evaluation)
            await s.flush()
            await _linked_dataset(s, evaluation, family="STIX_BUNDLE")
            agent = Agent(user_id=admin.id, name="Agent", skip_llm=True)
            s.add(agent)
            await s.flush()
            s.add(EvaluationAgent(evaluation_id=evaluation.id, agent_id=agent.id))
            await s.commit()

            await evaluations_api.trigger_evaluation(
                evaluation_id=str(evaluation.id), body=None, user=admin, db=s
            )

            assert stage_calls == [s]

            runs = (
                await s.execute(
                    select(EvaluationRun).where(
                        EvaluationRun.evaluation_id == evaluation.id
                    )
                )
            ).scalars().all()
            assert len(runs) == 1

            executions = (
                await s.execute(
                    select(OrchestrationExecution).where(
                        OrchestrationExecution.adapter == "evaluation"
                    )
                )
            ).scalars().all()
            assert len(executions) == 1
            assert executions[0].payload["evaluation_id"] == str(evaluation.id)
            assert executions[0].deadline_at is not None

        # Both rows and the owner are committed: visible from a fresh session.
        async with sm() as s2:
            executions = (
                await s2.execute(
                    select(OrchestrationExecution).where(
                        OrchestrationExecution.adapter == "evaluation"
                    )
                )
            ).scalars().all()
            assert len(executions) == 1
            runs = (
                await s2.execute(
                    select(EvaluationRun).where(
                        EvaluationRun.evaluation_id == evaluation.id
                    )
                )
            ).scalars().all()
            assert runs
            assert all(r.owner_execution_id == staged[0].id for r in runs)
    finally:
        await cleanup()


async def test_rescore_evaluation_run_stages_execution_atomically_with_the_run(
    monkeypatch,
):
    """``rescore_evaluation_run`` commits the run's ``"running"`` status and the
    execution's ``rescore_runs`` payload in one commit, and hands the run
    to the new execution.

    The fake-DB tests in ``test_evaluation_rescore.py`` mock
    ``stage_rescore_execution`` away, so only this real endpoint call can see a
    split commit or a wrong payload. A run left owned by its previous execution
    would be failed by that execution's later dead-letter cleanup, over the
    rescore's correct scores.
    """
    from app.models.orchestration import OrchestrationExecution
    from app.services.evaluation_engine import (
        stage_rescore_execution as real_stage_rescore_execution,
    )

    async def _noop_wake():
        return None

    monkeypatch.setattr(
        "app.services.orchestration_engine.notify_orchestration_wake", _noop_wake
    )

    # The end state alone would also come from a two-phase commit through the
    # isolated ``enqueue_rescore`` wrapper. The spy proves staging runs
    # on the ENDPOINT's session.
    stage_calls: list[object] = []
    staged: list = []

    async def _spy_stage_rescore_execution(db, *args, **kwargs):
        stage_calls.append(db)
        execution = await real_stage_rescore_execution(db, *args, **kwargs)
        staged.append(execution)
        return execution

    monkeypatch.setattr(
        "app.services.evaluation_engine.stage_rescore_execution",
        _spy_stage_rescore_execution,
    )
    previous_owner = uuid.uuid4()

    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(
                name="Eval",
                family="STIX_BUNDLE",
                model_ids=["gpt-4.1"],
                use_agent_default_model=False,
            )
            s.add(evaluation)
            await s.flush()
            await _linked_dataset(s, evaluation, family="STIX_BUNDLE")
            agent = Agent(user_id=admin.id, name="Agent", skip_llm=True)
            s.add(agent)
            await s.flush()
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4.1",
                status="completed",
                trigger_reason="manual",
                owner_execution_id=previous_owner,
            )
            s.add(run)
            await s.flush()
            s.add(
                EvaluationResult(
                    run_id=run.id, agent_output="hi", score=80.0
                )
            )
            await s.commit()

            await evaluations_api.rescore_evaluation_run(
                run_id=str(run.id), user=admin, db=s
            )

            assert stage_calls == [s]

        # Fresh session: the status flip, the owner and the execution are all
        # committed.
        async with sm() as s2:
            reloaded_run = (
                await s2.execute(
                    select(EvaluationRun).where(EvaluationRun.id == run.id)
                )
            ).scalar_one()
            assert reloaded_run.status == "running"
            assert reloaded_run.owner_execution_id == staged[0].id
            assert reloaded_run.owner_execution_id != previous_owner

            executions = (
                await s2.execute(
                    select(OrchestrationExecution).where(
                        OrchestrationExecution.adapter == "evaluation"
                    )
                )
            ).scalars().all()
            assert len(executions) == 1
            assert executions[0].payload["kind"] == "rescore_runs"
            assert executions[0].payload["run_ids"] == [str(run.id)]
            assert executions[0].deadline_at is not None
    finally:
        await cleanup()


async def test_trigger_evaluation_commit_failure_leaves_nothing_persisted(
    monkeypatch,
):
    """When the trigger's one commit fails, neither the run nor its execution
    survives.

    A successful call cannot tell one commit from two in sequence; a failing
    commit can. ``trigger_evaluation`` has exactly one ``await db.commit()``;
    run creation and ``stage_evaluation_execution`` before it only flush.
    """
    from app.models.orchestration import OrchestrationExecution

    async def _noop_wake():
        return None

    monkeypatch.setattr(
        "app.services.orchestration_engine.notify_orchestration_wake", _noop_wake
    )

    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(
                name="Eval",
                family="STIX_BUNDLE",
                model_ids=["gpt-4.1"],
                use_agent_default_model=False,
            )
            s.add(evaluation)
            await s.flush()
            await _linked_dataset(s, evaluation, family="STIX_BUNDLE")
            agent = Agent(user_id=admin.id, name="Agent", skip_llm=True)
            s.add(agent)
            await s.flush()
            s.add(EvaluationAgent(evaluation_id=evaluation.id, agent_id=agent.id))
            await s.commit()
            evaluation_id = evaluation.id

            # An instance override, not a class-level monkeypatch, so only this
            # session's commit fails.
            async def _failing_commit():
                raise RuntimeError("simulated commit failure")

            s.commit = _failing_commit

            with pytest.raises(RuntimeError, match="simulated commit failure"):
                await evaluations_api.trigger_evaluation(
                    evaluation_id=str(evaluation_id), body=None, user=admin, db=s
                )

        # Had the run been committed on its own first, it would be visible here.
        async with sm() as s2:
            runs = (
                await s2.execute(
                    select(EvaluationRun).where(
                        EvaluationRun.evaluation_id == evaluation_id
                    )
                )
            ).scalars().all()
            assert runs == []

            executions = (
                await s2.execute(
                    select(OrchestrationExecution).where(
                        OrchestrationExecution.adapter == "evaluation"
                    )
                )
            ).scalars().all()
            assert executions == []
    finally:
        await cleanup()


async def test_rescore_evaluation_run_commit_failure_leaves_nothing_persisted(
    monkeypatch,
):
    """When the rescore's one commit fails, the run keeps its prior status and no
    execution survives.

    ``rescore_evaluation_run`` has exactly one ``await db.commit()``, after
    ``stage_rescore_execution``; everything before it only flushes.
    """
    from app.models.orchestration import OrchestrationExecution

    async def _noop_wake():
        return None

    monkeypatch.setattr(
        "app.services.orchestration_engine.notify_orchestration_wake", _noop_wake
    )

    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(
                name="Eval",
                family="STIX_BUNDLE",
                model_ids=["gpt-4.1"],
                use_agent_default_model=False,
            )
            s.add(evaluation)
            await s.flush()
            await _linked_dataset(s, evaluation, family="STIX_BUNDLE")
            agent = Agent(user_id=admin.id, name="Agent", skip_llm=True)
            s.add(agent)
            await s.flush()
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4.1",
                status="completed",
                trigger_reason="manual",
            )
            s.add(run)
            await s.flush()
            s.add(EvaluationResult(run_id=run.id, agent_output="hi", score=80.0))
            await s.commit()
            run_id = run.id

            async def _failing_commit():
                raise RuntimeError("simulated commit failure")

            s.commit = _failing_commit

            with pytest.raises(RuntimeError, match="simulated commit failure"):
                await evaluations_api.rescore_evaluation_run(
                    run_id=str(run_id), user=admin, db=s
                )

        # A separate commit of the status flip would show "running" here; the
        # run must still be "completed".
        async with sm() as s2:
            reloaded_run = (
                await s2.execute(
                    select(EvaluationRun).where(EvaluationRun.id == run_id)
                )
            ).scalar_one()
            assert reloaded_run.status == "completed"

            executions = (
                await s2.execute(
                    select(OrchestrationExecution).where(
                        OrchestrationExecution.adapter == "evaluation"
                    )
                )
            ).scalars().all()
            assert executions == []
    finally:
        await cleanup()


async def test_scheduled_evaluation_staging_failure_does_not_kill_the_loop(
    monkeypatch,
):
    """One scheduled evaluation's staging failure is logged and the tick goes on
    to the next.

    ``_process_scheduled_evaluations`` shares ONE session across a tick. The
    rollback after a failure EXPIRES every loaded object, so reading
    ``evaluation.id`` afterwards lazy-refreshes and raises ``MissingGreenlet``,
    which aborts the loop and skips every remaining evaluation. A ``MagicMock``
    evaluation never expires, so this needs real rows and a real session.
    """
    from app.services import autonomous
    from app.services.evaluation_engine import (
        stage_evaluation_execution as real_stage,
    )

    async def _noop_wake():
        return None

    monkeypatch.setattr(
        "app.services.orchestration_engine.notify_orchestration_wake", _noop_wake
    )

    calls: list[str] = []
    fail_for: dict[str, str] = {}

    async def _stage_fail_first(db, evaluation_id, trigger_reason="manual"):
        calls.append(str(evaluation_id))
        if str(evaluation_id) == fail_for["id"]:
            raise RuntimeError("orchestration execution could not be created")
        return await real_stage(db, evaluation_id, trigger_reason=trigger_reason)

    monkeypatch.setattr(
        "app.services.evaluation_engine.stage_evaluation_execution",
        _stage_fail_first,
    )

    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin = await _admin(s)
            agent = Agent(user_id=admin.id, name="Agent", skip_llm=True)
            s.add(agent)
            await s.flush()

            evaluation_a = Evaluation(
                name="Eval A",
                family="STIX_BUNDLE",
                trigger_config={"schedule": "* * * * *"},
                enabled=True,
            )
            evaluation_b = Evaluation(
                name="Eval B",
                family="STIX_BUNDLE",
                trigger_config={"schedule": "* * * * *"},
                enabled=True,
            )
            s.add_all([evaluation_a, evaluation_b])
            await s.flush()
            for evaluation in (evaluation_a, evaluation_b):
                await _linked_dataset(s, evaluation, family="STIX_BUNDLE")
                s.add(
                    EvaluationAgent(evaluation_id=evaluation.id, agent_id=agent.id)
                )
            await s.commit()
            evaluation_a_id, evaluation_b_id = str(evaluation_a.id), str(evaluation_b.id)
            # Keyed to evaluation A's id, not call order: the query has no ORDER
            # BY, and an UPDATE can shift the scan order.
            fail_for["id"] = evaluation_a_id

            with patch.object(autonomous.logger, "error") as mock_error:
                count = await autonomous._process_scheduled_evaluations(s)

            # A's failure is logged with its real id, and B is still processed.
            mock_error.assert_called_once()
            logged_id = mock_error.call_args.args[1]
            assert str(logged_id) == evaluation_a_id
        assert count == 1
        assert sorted(calls) == sorted([evaluation_a_id, evaluation_b_id])
    finally:
        await cleanup()


async def test_a_bulk_re_run_replays_each_run_as_its_own_re_run_does(monkeypatch):
    """The bulk "Re-run full" replays each run with its own versions, as the run page's
    does: one whose persona snapshot is gone cannot be replayed, and is refused
    with a 409 naming it rather than run on the agent's current persona."""
    await _enqueue_noop(monkeypatch)
    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(name="Eval", family="STIX_BUNDLE")
            agent = Agent(user_id=admin.id, name="Agent", skip_llm=True)
            s.add_all([evaluation, agent])
            await s.flush()
            s.add(EvaluationAgent(evaluation_id=evaluation.id, agent_id=agent.id))
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="model-independent",
                status="completed",
                trigger_reason="manual",
                persona_version_number=7,  # no such version is stored
            )
            s.add(run)
            await s.flush()
            await _linked_dataset(s, evaluation, family="STIX_BUNDLE")

            with pytest.raises(HTTPException) as exc:
                await evaluations_api.trigger_evaluation_from_runs(
                    evaluation_id=str(evaluation.id),
                    body=TriggerEvaluationFromRunsRequest(run_ids=[str(run.id)]),
                    user=admin,
                    db=s,
                )
            assert exc.value.status_code == 409
            assert str(run.id) in exc.value.detail
    finally:
        await cleanup()


async def test_a_bulk_re_run_of_no_baseline_is_refused_before_creating_anything(monkeypatch):
    """A bulk "Re-run full" naming no run is a 400 and creates no run: the refusal
    comes before the runs are built, so a later "no runs created" check cannot be reached."""
    await _enqueue_noop(monkeypatch)
    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(name="Eval", family="STIX_BUNDLE")
            s.add(evaluation)
            await s.flush()
            await _linked_dataset(s, evaluation, family="STIX_BUNDLE")
            evaluation_id = evaluation.id
            await s.commit()

            with pytest.raises(HTTPException) as exc:
                await evaluations_api.trigger_evaluation_from_runs(
                    evaluation_id=str(evaluation_id),
                    body=TriggerEvaluationFromRunsRequest(run_ids=[]),
                    user=admin,
                    db=s,
                )
            assert exc.value.status_code == 400
            assert exc.value.detail == "No baseline runs selected"
            await s.rollback()

            runs = (
                await s.execute(
                    select(EvaluationRun.id).where(EvaluationRun.evaluation_id == evaluation_id)
                )
            ).all()
            assert runs == []
    finally:
        await cleanup()


# ── cancel_run ──


async def test_cancelling_a_run_cancels_the_execution_doing_the_work():
    """Cancelling a run cancels the execution doing its work.

    Marking the run alone leaves the worker executing and judging every
    remaining entry. Asserted on the EXECUTION, the thing that stops the work.
    From there, ``extend_lease`` raises ``LeaseLostError`` for a non-running
    state, the heartbeat fires ``cancel_event``, and the fan-out cancels
    in-flight calls (``test_a_fired_cancel_event_stops_the_run_batch``).
    """
    from app.api.v1.evaluations import cancel_run
    from app.models.orchestration import OrchestrationExecution

    sm, cleanup = await _make_db()
    try:
        # A live run owned by a running execution: the state a user cancels.
        run_id, execution_id, _ = await _stranded_fixture(
            sm, execution_state="running", execution_age_seconds=10
        )
        async with sm() as s:
            run = await s.get(EvaluationRun, run_id)
            run.status = "running"
            await s.commit()

        async with sm() as s:
            await cancel_run(run_id=str(run_id), user=None, db=s)

        async with sm() as s:
            run = await s.get(EvaluationRun, run_id)
            execution = await s.get(OrchestrationExecution, execution_id)

        assert run.status == "cancelled", f"the run reads {run.status!r}"
        assert execution.state == "cancelled", (
            f"the execution is still {execution.state!r} after the user "
            "cancelled the run, so nothing tells the worker to stop: it keeps "
            "calling the agent and the judge for every remaining entry, and the "
            "user is billed for work they cancelled"
        )
        assert execution.lease_owner is None, (
            "the cancelled execution still carries a lease, so it can be "
            "heartbeated as though it were live"
        )
    finally:
        await cleanup()


@pytest.mark.parametrize(
    ("previous_status", "expected_status", "finished"),
    [
        pytest.param("completed", "completed", True, id="a_completed_run"),
        pytest.param("failed", "failed", True, id="a_failed_run"),
        # A run imported or failed with no ``completed_at`` is rescored too: the
        # execution, not the timing, says a rescore is running.
        pytest.param("failed", "failed", False, id="a_run_with_no_recorded_end"),
    ],
)
async def test_cancelling_a_rescore_hands_the_run_back_as_it_was(
    previous_status, expected_status, finished
):
    """A rescore re-judges a finished run's stored answers: cancelling it leaves the run
    as it was, its status, the timing of the run it re-judged and the failure of
    an earlier rescore included."""
    from app.api.v1.evaluations import cancel_run
    from app.models.orchestration import OrchestrationExecution

    finished = datetime.now(UTC) - timedelta(hours=1) if finished else None
    sm, cleanup = await _make_db()
    try:
        run_id, execution_id, _ = await _stranded_fixture(
            sm, execution_state="running", execution_age_seconds=10, run_completed_at=finished
        )
        async with sm() as s:
            run = await s.get(EvaluationRun, run_id)
            run.duration_seconds = 42.0
            run.rescore_error = "an earlier rescore failed"
            execution = await s.get(OrchestrationExecution, execution_id)
            execution.payload = {
                "kind": "rescore_runs",
                "run_ids": [str(run_id)],
                "previous_statuses": {str(run_id): previous_status},
            }
            await s.commit()

        async with sm() as s:
            await cancel_run(run_id=str(run_id), user=None, db=s)

        async with sm() as s:
            run = await s.get(EvaluationRun, run_id)
            execution = await s.get(OrchestrationExecution, execution_id)

        assert run.status == expected_status
        assert (run.completed_at, run.duration_seconds) == (finished, 42.0), (
            "cancelling the rescore rewrote the timing of the run it re-judged"
        )
        assert run.rescore_error == "an earlier rescore failed", (
            "the scores shown are still from before the failed rescore"
        )
        assert execution.state == "cancelled", "the rescore's work was not stopped"
    finally:
        await cleanup()


async def test_cancelling_one_run_of_a_bulk_rescore_leaves_the_others_rescoring():
    """A bulk rescore's execution owns its sibling runs, so a cancel of one run hands
    that run back and leaves the execution, and the others, at work."""
    from app.api.v1.evaluations import cancel_run
    from app.models.orchestration import OrchestrationExecution

    finished = datetime.now(UTC) - timedelta(hours=1)
    sm, cleanup = await _make_db()
    try:
        run_id, execution_id, _ = await _stranded_fixture(
            sm, execution_state="running", execution_age_seconds=10, run_completed_at=finished
        )
        async with sm() as s:
            run = await s.get(EvaluationRun, run_id)
            sibling = EvaluationRun(
                evaluation_id=run.evaluation_id,
                agent_id=run.agent_id,
                model_id="gpt-4.1",
                status="running",
                trigger_reason="manual",
                completed_at=finished,
                owner_execution_id=execution_id,
            )
            s.add(sibling)
            await s.flush()
            sibling_id = sibling.id
            execution = await s.get(OrchestrationExecution, execution_id)
            execution.payload = {
                "kind": "rescore_runs",
                "run_ids": [str(run_id), str(sibling_id)],
                "previous_statuses": {str(run_id): "completed", str(sibling_id): "completed"},
            }
            await s.commit()

        async with sm() as s:
            await cancel_run(run_id=str(run_id), user=None, db=s)

        async with sm() as s:
            statuses = {
                rid: (await s.get(EvaluationRun, rid)).status for rid in (run_id, sibling_id)
            }
            state = (await s.get(OrchestrationExecution, execution_id)).state
        assert statuses == {run_id: "completed", sibling_id: "running"}
        assert state == "running", "the cancel of one run stopped the whole batch"
    finally:
        await cleanup()


async def test_deleting_a_run_being_rescored_stops_the_rescore():
    """The deletion goes through the cancel path: the rescore's execution is
    cancelled before the run goes."""
    from app.models.orchestration import OrchestrationExecution

    sm, cleanup = await _make_db()
    try:
        run_id, execution_id, _ = await _stranded_fixture(
            sm,
            execution_state="running",
            execution_age_seconds=10,
            run_completed_at=datetime.now(UTC) - timedelta(hours=1),
        )
        async with sm() as s:
            execution = await s.get(OrchestrationExecution, execution_id)
            execution.payload = {
                "kind": "rescore_runs",
                "run_ids": [str(run_id)],
                "previous_statuses": {str(run_id): "completed"},
            }
            await s.commit()

        async with sm() as s:
            admin = (await s.execute(select(User).where(User.email == "a@x.io"))).scalar_one()
            await evaluations_api.delete_run(run_id=str(run_id), user=admin, db=s)

        async with sm() as s:
            assert await s.get(EvaluationRun, run_id) is None
            assert (await s.get(OrchestrationExecution, execution_id)).state == "cancelled"
    finally:
        await cleanup()


async def test_deleting_a_run_stops_the_work_it_shares_with_no_other_run():
    """A deletion goes through the cancel path first: the execution doing the
    run's work is cancelled, rather than calling models for a run that is gone."""
    from app.models.orchestration import OrchestrationExecution

    sm, cleanup = await _make_db()
    try:
        run_id, execution_id, _ = await _stranded_fixture(
            sm, execution_state="running", execution_age_seconds=10
        )
        async with sm() as s:
            admin = (await s.execute(select(User).where(User.email == "a@x.io"))).scalar_one()
            await evaluations_api.delete_run(run_id=str(run_id), user=admin, db=s)

        async with sm() as s:
            assert await s.get(EvaluationRun, run_id) is None
            assert (await s.get(OrchestrationExecution, execution_id)).state == "cancelled"
    finally:
        await cleanup()


@pytest.mark.parametrize("what", ["the_evaluation", "all_its_runs"])
async def test_deleting_an_evaluation_or_all_its_runs_stops_their_work(what):
    """Every execution working on the evaluation is cancelled with it: the
    one owning an active run, and a dispatch no run is claimed by yet."""
    from app.models.orchestration import OrchestrationExecution

    sm, cleanup = await _make_db()
    try:
        run_id, owner_id, _ = await _stranded_fixture(
            sm, execution_state="running", execution_age_seconds=10
        )
        dispatch_id = uuid.uuid4()
        async with sm() as s:
            evaluation_id = (await s.get(EvaluationRun, run_id)).evaluation_id
            s.add(
                OrchestrationExecution(
                    id=dispatch_id,
                    adapter="evaluation",
                    state="pending",
                    payload={"evaluation_id": str(evaluation_id)},
                    attempt_count=0,
                )
            )
            await s.commit()

        async with sm() as s:
            admin = (await s.execute(select(User).where(User.email == "a@x.io"))).scalar_one()
            if what == "the_evaluation":
                await evaluations_api.delete_evaluation(
                    evaluation_id=str(evaluation_id), user=admin, db=s, _ai_gate=None
                )
            else:
                await evaluations_api.delete_all_runs(
                    evaluation_id=str(evaluation_id), user=admin, db=s
                )

        async with sm() as s:
            states = {
                eid: (await s.get(OrchestrationExecution, eid)).state
                for eid in (owner_id, dispatch_id)
            }
        assert states == {owner_id: "cancelled", dispatch_id: "cancelled"}
    finally:
        await cleanup()


async def test_cancelling_a_run_that_no_execution_owns_still_succeeds():
    """Cancelling a run no execution owns still marks it cancelled.

    ``owner_execution_id`` has no backfill, so a run may name no
    execution: there is nothing to stop.
    """
    from app.api.v1.evaluations import cancel_run

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(sm, owner=False)
        async with sm() as s:
            run = await s.get(EvaluationRun, run_id)
            run.status = "running"
            await s.commit()

        async with sm() as s:
            await cancel_run(run_id=str(run_id), user=None, db=s)

        status, error = await _run_status(sm, run_id)
        assert (status, error) == ("cancelled", "Cancelled by user"), (
            f"an unowned run could not be cancelled: {status!r} / {error!r}"
        )
    finally:
        await cleanup()


@pytest.mark.parametrize("episode_status", ["running", "waiting_callback", "resumed", "pending"])
async def test_cancelling_a_run_closes_the_entries_it_stops(episode_status):
    """Cancelling a run closes every non-terminal episode it stops.

    An open episode on a cancelled run would:

    * still ACCEPT a late callback, which ``_upsert_resumed_result`` scores, so
      the user pays a judge call on a run they cancelled;
    * keep ``completed_entries`` climbing, since ``_reconcile_run_status``
      freezes a terminal run's status but not its progress;
    * strand a ``running`` episode: the sweep reaches only ``"running"``
      runs with no ``completed_at``.

    Parametrised over every non-terminal status: one left out stays open.
    """
    from app.api.v1.evaluations import cancel_run

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(
            sm, execution_state="running", episodes=(episode_status,)
        )
        async with sm() as s:
            run = await s.get(EvaluationRun, run_id)
            run.status = "running"
            episode = (
                await s.execute(
                    select(EvaluationEpisode).where(EvaluationEpisode.run_id == run_id)
                )
            ).scalar_one()
            episode.callback_deadline = datetime.now(UTC) + timedelta(hours=1)
            episode.waiting_since = datetime.now(UTC)
            await s.commit()

        async with sm() as s:
            await cancel_run(run_id=str(run_id), user=None, db=s)

        async with sm() as s:
            episode = (
                await s.execute(
                    select(EvaluationEpisode).where(EvaluationEpisode.run_id == run_id)
                )
            ).scalar_one()

        assert episode.status == "failed", (
            f"an episode left {episode.status!r} on a cancelled run: a late "
            "callback is still accepted and billed, progress can still climb "
            "against a frozen score, and nothing will ever close it"
        )
        assert episode.error == "Cancelled by user", (
            f"the episode records {episode.error!r}, so an operator cannot tell "
            "a user's cancellation from a worker that died"
        )
        assert episode.completed_at is not None
        assert episode.callback_deadline is None, (
            "a terminal episode still carries a callback deadline, so the "
            "timeout janitor can pick it up afterwards"
        )
    finally:
        await cleanup()


async def test_a_callback_arriving_after_a_cancellation_is_told_it_was_discarded():
    """A callback arriving after a cancellation is told DISCARDED, not accepted and
    scored at the user's expense.
    """
    from app.api.v1.evaluations import cancel_run
    from app.services.evaluation_continuation import (
        CallbackOutcome,
        ConversationContinuationService,
    )

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(
            sm, execution_state="running", episodes=("waiting_callback",)
        )
        async with sm() as s:
            run = await s.get(EvaluationRun, run_id)
            run.status = "running"
            episode = (
                await s.execute(
                    select(EvaluationEpisode).where(EvaluationEpisode.run_id == run_id)
                )
            ).scalar_one()
            episode.callback_deadline = datetime.now(UTC) + timedelta(hours=1)
            correlation_id = episode.correlation_id
            await s.commit()

        async with sm() as s:
            await cancel_run(run_id=str(run_id), user=None, db=s)

        async with sm() as s:
            _, outcome = await ConversationContinuationService.resume_from_callback(
                s,
                correlation_id=correlation_id,
                idempotency_key="arrives-after-cancel",
                payload={"final_output": "an answer nobody asked for any more"},
            )

        assert outcome is CallbackOutcome.DISCARDED, (
            f"the callback reported {outcome!r}: accepting it after the user "
            "cancelled stores a result and pays for a judge call on a run that "
            "is over"
        )
    finally:
        await cleanup()


async def test_a_claim_landing_during_a_cancellation_is_cancelled_too():
    """A dispatch claim landing during a cancellation has its execution cancelled
    too.

    ``execute_evaluation`` claims unowned rows with
    ``UPDATE ... WHERE status = 'pending' AND owner_execution_id IS NULL``, which
    can commit between ``cancel_run``'s unlocked read and its commit. Locking the
    run first would be run-then-execution order, an ABBA deadlock against the
    worker's fence. So ``cancel_run`` re-reads the owner after its commit, when
    the claim's WHERE can no longer match. The claim is run from another session
    in that window, through the endpoint's own session hook.
    """
    from app.api.v1.evaluations import cancel_run
    from app.models.orchestration import OrchestrationExecution
    from app.services.evaluation.run_ownership import runs_owned_by_nobody

    sm, cleanup = await _make_db()
    try:
        # A legacy shape: pending, owned by nobody (no backfill).
        run_id, _, _ = await _stranded_fixture(sm, owner=False)
        claimant = uuid.uuid4()
        async with sm() as s:
            run = await s.get(EvaluationRun, run_id)
            run.status = "pending"
            evaluation_id = run.evaluation_id
            s.add(
                OrchestrationExecution(
                    id=claimant,
                    adapter="evaluation",
                    state="running",
                    payload={"kind": "evaluation", "evaluation_id": str(evaluation_id)},
                    attempt_count=1,
                )
            )
            await s.commit()

        async with sm() as request_session:
            inner_execute = request_session.execute
            claimed = []

            async def execute_then_let_the_claim_land(statement, *args, **kwargs):
                result = await inner_execute(statement, *args, **kwargs)
                rendered = str(statement).lower()
                # Right after the endpoint's own unlocked read of the run.
                if not claimed and "from evaluation_runs" in rendered and (
                    "for update" not in rendered
                ):
                    claimed.append(True)
                    async with sm() as worker:
                        await worker.execute(
                            update(EvaluationRun)
                            .where(
                                EvaluationRun.evaluation_id == evaluation_id,
                                EvaluationRun.status == "pending",
                                runs_owned_by_nobody(),
                            )
                            .values(owner_execution_id=claimant)
                        )
                        await worker.commit()
                return result

            request_session.execute = execute_then_let_the_claim_land
            await cancel_run(run_id=str(run_id), user=None, db=request_session)

        assert claimed, (
            "the claim never ran — this test is not exercising the race it names"
        )

        async with sm() as s:
            run = await s.get(EvaluationRun, run_id)
            execution = await s.get(OrchestrationExecution, claimant)

        assert run.status == "cancelled", f"the run reads {run.status!r}"
        assert execution.state == "cancelled", (
            f"the claiming execution is still {execution.state!r}: it took the "
            "run while the user was cancelling it, and nothing stopped it — so "
            "the work carries on under a run that reads cancelled"
        )
    finally:
        await cleanup()


async def test_a_rescore_landing_after_a_cancellation_is_not_mistaken_for_the_claim_race():
    """``cancel_run``'s post-commit re-read never cancels a rescore landing after it.

    A rescore puts the cancelled run back to ``"running"`` with a NEW owner,
    the same changed-owner signal as a dispatch claim. The ``status_now ==
    "cancelled"`` guard separates them: a claim needs ``pending``, a rescore
    writes ``running``, so only a claim leaves the status ``cancelled``.
    """
    from app.api.v1.evaluations import cancel_run
    from app.models.orchestration import OrchestrationExecution

    sm, cleanup = await _make_db()
    try:
        run_id, original_execution, _ = await _stranded_fixture(
            sm, execution_state="running"
        )
        rescorer = uuid.uuid4()
        async with sm() as s:
            run = await s.get(EvaluationRun, run_id)
            run.status = "running"
            s.add(
                OrchestrationExecution(
                    id=rescorer,
                    adapter="evaluation",
                    state="running",
                    payload={"kind": "rescore_runs", "run_ids": [str(run_id)]},
                    attempt_count=1,
                )
            )
            await s.commit()

        async with sm() as request_session:
            inner_commit = request_session.commit
            rescored = []

            async def commit_then_let_the_rescore_land():
                await inner_commit()
                # The repair's window: after the cancel commits, before it
                # re-reads the owner.
                if not rescored:
                    rescored.append(True)
                    async with sm() as other:
                        run = await other.get(EvaluationRun, run_id)
                        run.status = "running"
                        run.owner_execution_id = rescorer
                        await other.commit()

            request_session.commit = commit_then_let_the_rescore_land
            await cancel_run(run_id=str(run_id), user=None, db=request_session)

        assert rescored, "the rescore never landed — the window was not exercised"

        async with sm() as s:
            rescore_execution = await s.get(OrchestrationExecution, rescorer)
            original = await s.get(OrchestrationExecution, original_execution)

        assert original.state == "cancelled", (
            "the run's own execution was not cancelled, so this test is not "
            "exercising a real cancellation"
        )
        assert rescore_execution.state == "running", (
            f"the deliberate rescore's execution was {rescore_execution.state!r}: "
            "the repair mistook it for the claim race and stopped it, leaving a "
            "running run with no worker"
        )
    finally:
        await cleanup()


async def test_cancel_takes_the_execution_lock_before_it_touches_the_run():
    """``cancel_run`` takes the execution lock before the run's.

    The worker holds the execution and reaches for the run (``_fence_on_lease``),
    so the opposite order is an ABBA deadlock. Asserted on the SQL the database
    received, not the call order: a run mutated first is autoflushed as
    ``UPDATE evaluation_runs`` ahead of ``cancel_execution``'s SELECT.
    """
    from app.api.v1.evaluations import cancel_run

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(sm, execution_state="running")
        async with sm() as s:
            run = await s.get(EvaluationRun, run_id)
            run.status = "running"
            await s.commit()

        emitted: list[str] = []

        # Listened for at the CONNECTION: an autoflushed UPDATE goes straight to
        # the connection, never through ``session.execute``.
        def record(conn, cursor, statement, params, context, executemany):
            sql = statement.lower()
            locks = "for update" in sql
            if "orchestration_executions" in sql and locks:
                emitted.append("EXECUTION")
            elif "evaluation_episodes" in sql and locks:
                emitted.append("EPISODE")
            elif "evaluation_runs" in sql and (
                locks or sql.lstrip().startswith("update evaluation_runs")
            ):
                emitted.append("RUN")

        async with sm() as request_session:
            import sqlalchemy

            # ``get_bind()`` already hands back the sync Engine here.
            bind = request_session.get_bind()
            sync_engine = getattr(bind, "sync_engine", bind)
            sqlalchemy.event.listen(sync_engine, "before_cursor_execute", record)
            try:
                await cancel_run(run_id=str(run_id), user=None, db=request_session)
            finally:
                sqlalchemy.event.remove(
                    sync_engine, "before_cursor_execute", record
                )

        # Collapse repeats; only the FIRST acquisition of each matters.
        seen: list[str] = []
        for entity in emitted:
            if entity not in seen:
                seen.append(entity)

        assert seen[:2] == ["EXECUTION", "RUN"], (
            f"lock acquisition order was {seen}, not execution-then-run. The "
            "worker holds the execution and reaches for the run, so taking them "
            f"the other way round is an ABBA deadlock. Statements: {emitted}"
        )
    finally:
        await cleanup()


async def test_cancelling_one_run_of_a_batch_does_not_stop_its_siblings():
    """Cancelling one run of a batch does not stop its siblings.

    A trigger stamps ONE execution on every run of the agent x model matrix,
    and its ``cancel_event`` cancels the whole batch. So ``cancel_run`` cancels
    the execution only when the run is the only one it owns; otherwise the run
    stops itself at its next entry boundary
    (``test_a_cancelled_run_stops_at_the_next_entry_instead_of_finishing``).
    """
    from app.api.v1.evaluations import cancel_run
    from app.models.orchestration import OrchestrationExecution

    sm, cleanup = await _make_db()
    try:
        run_id, execution_id, _ = await _stranded_fixture(
            sm, execution_state="running", execution_age_seconds=10
        )
        # The second run of the matrix: same evaluation, same execution, a
        # different model. Nobody cancelled this one.
        async with sm() as s:
            run = await s.get(EvaluationRun, run_id)
            run.status = "running"
            sibling = EvaluationRun(
                evaluation_id=run.evaluation_id,
                agent_id=run.agent_id,
                model_id="sibling-model",
                status="running",
                trigger_reason="manual",
                total_entries=run.total_entries,
                owner_execution_id=execution_id,
            )
            s.add(sibling)
            await s.commit()
            sibling_id = sibling.id

        async with sm() as s:
            await cancel_run(run_id=str(run_id), user=None, db=s)

        async with sm() as s:
            run = await s.get(EvaluationRun, run_id)
            sibling = await s.get(EvaluationRun, sibling_id)
            execution = await s.get(OrchestrationExecution, execution_id)

        assert run.status == "cancelled", (
            f"the cancelled run reads {run.status!r} — the endpoint must still "
            "mark the run the user actually cancelled"
        )
        assert execution.state == "running", (
            f"the shared execution was {execution.state!r}. Cancelling it fires "
            "cancel_event for the whole fan-out, so every sibling run in the "
            "matrix is interrupted mid-model-call because ONE of them was "
            "cancelled."
        )
        assert sibling.status == "running", (
            f"the sibling reads {sibling.status!r}; cancelling a different run "
            "must not touch it"
        )
    finally:
        await cleanup()


# --- admin lookups: prompt guard and always-ask tools ----------


@pytest.mark.parametrize("path", ["/prompt-guard", "/always-ask-tools"])
async def test_the_settings_lookups_are_for_admins_only(path):
    """The prompt guard state and the always-ask tools are for admins
    only, and declared before ``/{evaluation_id}``, or the path would be read as an id."""
    from app.api.deps import require_admin

    route = next(r for r in evaluations_api.router.routes if getattr(r, "path", "").endswith(path))
    assert require_admin in {dependency.call for dependency in route.dependant.dependencies}
    paths = [getattr(r, "path", "") for r in evaluations_api.router.routes if "GET" in getattr(r, "methods", set())]
    assert paths.index(route.path) < paths.index(f"{evaluations_api.router.prefix}/{{evaluation_id}}")


async def test_the_prompt_guard_state_says_whether_a_guard_is_connected():
    """The prompt guard state reports the guard's own connection test, shown under the switch."""
    from app.models.integration import Integration

    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin = await _admin(s)
            assert await evaluations_api.get_prompt_guard_state(user=admin, db=s) == {"connected": False}

            s.add(Integration(name="Nova", provider="nova", status="connected", is_company_managed=True))
            await s.commit()
            assert await evaluations_api.get_prompt_guard_state(user=admin, db=s) == {"connected": True}
    finally:
        await cleanup()


# --- the "always ask" tools of the selected agents ------------------------


async def test_the_always_ask_route_reports_the_given_agents_and_skips_unknown_ids(monkeypatch):
    """The always-ask route loads the agents it is given; an unknown or malformed id is left out."""
    from app.services.evaluation import always_ask

    seen: list = []

    async def _report(_db, agents):
        seen.extend(agents)
        return [{"agent_id": str(a.id), "agent_name": a.name, "tools": ["search_knowledge"]} for a in agents]

    monkeypatch.setattr(always_ask, "always_ask_tools", _report)
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            agent = Agent(user_id=admin.id, name="Analyst", slug="analyst-always-ask")
            s.add(agent)
            await s.commit()

            answer = await evaluations_api.get_always_ask_tools(
                agent_ids=[str(agent.id), str(uuid.uuid4()), "not-an-id"], user=admin, db=s
            )

        assert [a.id for a in seen] == [agent.id]
        assert answer == {"agents": [{"agent_id": str(agent.id), "agent_name": "Analyst", "tools": ["search_knowledge"]}]}
    finally:
        await cleanup()


async def test_the_always_ask_route_says_when_the_marks_cannot_be_read(monkeypatch):
    """Not knowing must not read as "nothing marked": a 503, which the card shows as such."""
    from app.services.evaluation import always_ask
    from app.services.hitl_whitelist import WhitelistUnavailableError

    async def _unreadable(_db, _agents):
        raise WhitelistUnavailableError("Could not resolve the HITL re-gates")

    monkeypatch.setattr(always_ask, "always_ask_tools", _unreadable)

    with pytest.raises(HTTPException) as refused:
        await evaluations_api.get_always_ask_tools(agent_ids=[], user=SimpleNamespace(), db=None)  # type: ignore[arg-type]

    assert refused.value.status_code == 503


def test_the_always_ask_route_takes_a_bounded_number_of_agents():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.deps import require_admin
    from app.core.database import get_db

    app = FastAPI()
    app.include_router(evaluations_api.router, prefix="/evaluations")
    app.dependency_overrides[require_admin] = lambda: SimpleNamespace()
    app.dependency_overrides[get_db] = lambda: None
    ids = "&".join(f"agent_ids={uuid.uuid4()}" for _ in range(evaluations_api.MAX_ALWAYS_ASK_AGENT_IDS + 1))

    answer = TestClient(app).get(f"/evaluations/always-ask-tools?{ids}")

    assert answer.status_code == 422


# --- company-managed toggle -----------------------------------------------------------


# (stored before, the change, the sharing after, who owns it after). The evaluation is
# owned by another admin than the one who toggles it, except for the row with no owner.
_TOGGLES = {
    "made-company-managed-drops-its-grants": (
        {"is_company_managed": False},
        {"is_company_managed": True},
        (True, False, None, None),
        "owner",
    ),
    "made-company-managed-and-restricted-keeps-its-grants": (
        {"is_company_managed": False},
        {"is_company_managed": True, "access_restricted": True},
        (True, True, "user", "group"),
        "owner",
    ),
    "restricted-made-private-keeps-its-grants": (
        {"is_company_managed": True, "access_restricted": True},
        {"is_company_managed": False},
        (False, False, "user", "group"),
        "owner",
    ),
    "with-no-owner-made-private-is-owned-by-the-admin": (
        {"is_company_managed": True, "user_id": None},
        {"is_company_managed": False},
        (False, False, "user", "group"),
        "admin",
    ),
}


@pytest.mark.parametrize("case", list(_TOGGLES))
async def test_the_company_managed_toggle_of_an_evaluation(case):
    """Visible to everyone, its grants confer nothing and are cleared, unless it is
    restricted, where they are its audience. Going private gives it back to its
    owner, and a row with none to the admin who toggles it."""
    from app.api.v1.schemas.evaluation import UpdateEvaluationRequest
    from app.models.group import Group

    stored_before, change, sharing, owned_by = _TOGGLES[case]
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            owner = User(email="o@x.io", full_name="Owner", hashed_password="x", role="admin")
            member = User(email="m@x.io", full_name="Member", hashed_password="x", role="user")
            group = Group(name="Analysts")
            s.add_all([owner, member, group])
            await s.flush()
            names = {"user": [str(member.id)], "group": [str(group.id)], None: None}
            evaluation = Evaluation(
                name="Eval",
                **{"user_id": owner.id, **stored_before},
                shared_user_ids=[str(member.id)],
                shared_group_ids=[str(group.id)],
            )
            s.add(evaluation)
            await s.commit()
            resp = await evaluations_api.update_evaluation(
                evaluation_id=str(evaluation.id),
                body=UpdateEvaluationRequest(**change),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert (
                resp["is_company_managed"],
                resp["access_restricted"],
                resp["shared_user_ids"],
                resp["shared_group_ids"],
            ) == (sharing[0], sharing[1], names[sharing[2]], names[sharing[3]])
            expected_owner = owner.id if owned_by == "owner" else admin.id
        async with sm() as s:
            stored = await s.get(Evaluation, evaluation.id)
            assert stored.user_id == expected_owner
    finally:
        await cleanup()
