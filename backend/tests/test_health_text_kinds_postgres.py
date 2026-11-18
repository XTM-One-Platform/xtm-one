"""Level 6's text kinds on PostgreSQL (#8259): a persona, a skill's content and
an assignment's prompt rewritten, lower levels stubbed at ``quality_deps``. The
variant runs on sandbox copies and no production row changes; a human edit
makes the experiment stale; what level 4 cannot run yet is stated."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.assignment import Assignment
from app.models.health_experiment import (
    STATUS_COMPLETED,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    STATUS_STALE,
    ConciergeExperiment,
)
from app.services.health import (
    experiments,
    quality_deps,
    text_kinds,  # noqa: F401 - registers the kinds under test
)
from app.services.health.variants import VariantError
from tests.health_experiment_helpers import (
    PERSONA,
    PROMPT,
    SKILL,
    VARIANT,
    Deps,
    draft_variant,
    run_experiment,
    seed,
)
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_a_persona_variant_runs_on_sandbox_copies_and_changes_nothing(monkeypatch):
    deps = Deps(monkeypatch)
    async with _temp_database("ce_persona") as sessions, sessions() as db:
        agent, skill, assignment, experimenter, finding = await seed(db)
        experiment, _ = await draft_variant(db, agent, finding)
        assert (experiment.target_type, experiment.base_text) == ("agent", PERSONA)
        await run_experiment(db, agent, experiment)
        assert experiment.status == STATUS_RUNNING
        assert [c["persona"] for c in deps.calls["create_agent_copy"]] == [None, VARIANT]
        assert {c["agent_id"] for c in deps.calls["create_agent_copy"]} == {agent.id}
        (launch,) = deps.calls["launch"]
        assert (launch["baseline"].overrides, launch["variant"].overrides) == (None, None)
        await experiments.settle(db, experiment)
        assert experiment.status == STATUS_COMPLETED
        await experiments.propose(db, experiment=experiment, finding=finding, author_agent=experimenter)
        (spec,) = deps.calls["file_proposal"]
        assert (spec["base_content"], spec["proposed_content"]) == (PERSONA, VARIANT)
        await db.commit()
        assert (await db.get(Agent, agent.id)).persona == PERSONA
        assert (await db.get(AgentSkill, skill.id)).content == SKILL
        assert (await db.get(Assignment, assignment.id)).prompt == PROMPT


@pytest.mark.asyncio
async def test_a_human_edit_makes_the_experiment_stale(monkeypatch):
    deps = Deps(monkeypatch)
    async with _temp_database("ce_text_stale") as sessions, sessions() as db:
        agent, _skill, _assignment, _experimenter, finding = await seed(db)
        experiment, _ = await draft_variant(db, agent, finding)
        agent.persona = PERSONA + " Be brief."
        await db.flush()
        await run_experiment(db, agent, experiment)
        assert experiment.status == STATUS_STALE
        assert "launch" not in deps.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("overlays", [False, True])
async def test_a_skill_variant_runs_as_the_variant_arm_s_stand_in(monkeypatch, overlays):
    deps = Deps(monkeypatch)
    monkeypatch.setattr(quality_deps, "SUBJECT_OVERLAYS_AVAILABLE", overlays)
    async with _temp_database(f"ce_skill_{overlays}") as sessions, sessions() as db:
        agent, skill, _assignment, _experimenter, finding = await seed(db)
        experiment, _ = await draft_variant(
            db, agent, finding, change_type="skill", target_id=skill.id, text=SKILL + " Quote its number."
        )
        assert (experiment.target_type, experiment.base_text) == ("skill", SKILL)
        await run_experiment(db, agent, experiment)
        if not overlays:
            # A Concierge launch takes no per-arm overlay yet: stated, nothing copied or launched.
            assert (experiment.status, experiment.status_reason) == (STATUS_SKIPPED, "evaluation_unavailable")
            assert "create_agent_copy" not in deps.calls
            assert "launch" not in deps.calls
            return
        assert [c["persona"] for c in deps.calls["create_agent_copy"]] == [None, None]
        (launch,) = deps.calls["launch"]
        assert launch["baseline"].overrides is None
        (substitute,) = launch["variant"].overrides["skill_substitutes"].values()
        assert (substitute.name, substitute.content) == ("Invoices", SKILL + " Quote its number.")
        assert (await db.get(AgentSkill, skill.id)).content == SKILL


@pytest.mark.asyncio
async def test_an_assignment_prompt_variant_is_not_measured_yet(monkeypatch):
    deps = Deps(monkeypatch)
    async with _temp_database("ce_prompt") as sessions, sessions() as db:
        agent, _skill, assignment, _experimenter, finding = await seed(db)
        with pytest.raises(VariantError):
            await draft_variant(db, agent, finding, change_type="assignment_prompt", target_id=assignment.id, text="No placeholder.")
        experiment, _ = await draft_variant(
            db, agent, finding, change_type="assignment_prompt", target_id=assignment.id, text=PROMPT + " Be brief."
        )
        await run_experiment(db, agent, experiment)
        assert (experiment.status, experiment.status_reason) == (STATUS_SKIPPED, "evaluation_unavailable")
        assert "launch" not in deps.calls
        rows = (await db.execute(select(ConciergeExperiment))).scalars().all()
        assert [r.id for r in rows] == [experiment.id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "drafted"),
    [
        ("unshared", True),
        ("company_managed", True),
        ("managed_by_a_chat_reader", True),
        ("read_only_grant", True),
        ("managed_by_a_config_grantee", False),
        ("another_owner", False),
    ],
)
async def test_a_skill_variant_needs_managers_who_read_the_agent_s_chats(monkeypatch, case, drafted):
    import uuid

    from app.models.user import User
    from app.services.health.experiments import ExperimentError

    Deps(monkeypatch)
    async with _temp_database(f"ce_skill_managers_{case}") as sessions, sessions() as db:
        agent, skill, _assignment, _experimenter, finding = await seed(db)
        other = User(id=uuid.uuid4(), email=f"ce-other-{uuid.uuid4().hex[:8]}@example.com", full_name="CE", hashed_password="x")
        db.add(other)
        await db.flush()
        if case == "company_managed":
            skill.is_company_managed = True
        elif case == "another_owner":
            skill.user_id = other.id
        elif case != "unshared":
            skill.shared_user_ids = [str(other.id)]
            skill.shared_user_permissions = {str(other.id): "read_only" if case == "read_only_grant" else "config"}
            agent.shared_user_ids = [str(other.id)]
            agent.shared_user_permissions = {
                str(other.id): "config_with_chats" if case == "managed_by_a_chat_reader" else "config"
            }
        await db.flush()
        if drafted:
            experiment, _ = await draft_variant(
                db, agent, finding, change_type="skill", target_id=skill.id, text=SKILL + " Quote its number."
            )
            assert experiment.target_id == skill.id
        else:
            with pytest.raises(ExperimentError, match="only the agent's conversation readers manage"):
                await draft_variant(db, agent, finding, change_type="skill", target_id=skill.id, text=SKILL + " Quote it.")
