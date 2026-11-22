"""Platform Health level 8 (#8261): the rules that need no database.

Signal thresholds at their boundaries (decision B18), the digest status that
never reads a non-success as a success, the Settings > Agents and automations > Concierge bounds, and
the seeded fleet's declarations.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.services.health import fleet_seed
from app.services.health.digest import digest_status
from app.services.health.settings import HealthSettings, HealthSignalSettings, _read
from app.services.health.signals import (
    SignalReading,
    cost_crossed,
    failure_crossed,
    feedback_crossed,
)

DEFAULTS = HealthSignalSettings()


# ── Feedback drop: -10 points over 3 days, at least 10 ratings ──────────────


@pytest.mark.parametrize(
    ("current", "previous", "ratings", "crossed"),
    [
        (0.70, 0.80, 10, True),  # exactly 10 points, exactly 10 ratings
        (0.71, 0.80, 10, False),  # 9 points
        (0.70, 0.80, 9, False),  # one rating short
        (0.50, 0.90, 100, True),
        (0.90, 0.50, 100, False),  # a rise
        (None, 0.80, 50, False),  # nothing scored now
        (0.70, None, 50, False),  # no baseline
    ],
)
def test_feedback_drop_threshold(current, previous, ratings, crossed):
    assert feedback_crossed(current, previous, ratings, DEFAULTS) is crossed


# ── Failure spike: above 5 % over the last 20 settled runs ──────────────────


def test_failure_spike_needs_a_full_window():
    assert failure_crossed(["failed"] * 19, DEFAULTS) == (False, None)


def test_failure_spike_exactly_five_percent_is_not_above():
    statuses = ["failed"] + ["completed"] * 19
    assert failure_crossed(statuses, DEFAULTS) == (False, 5.0)


def test_failure_spike_above_five_percent():
    statuses = ["failed", "failed"] + ["completed"] * 18
    assert failure_crossed(statuses, DEFAULTS) == (True, 10.0)


def test_failure_spike_reads_the_newest_runs_only():
    statuses = ["completed"] * 20 + ["failed"] * 20  # newest first
    assert failure_crossed(statuses, DEFAULTS) == (False, 0.0)


# ── Cost drift: cost per run up 20 % over 7 days vs the previous 7 ──────────


def test_cost_drift_exactly_twenty_percent():
    assert cost_crossed(12.0, 10, 10.0, 10, DEFAULTS) == (True, 1.2, 1.0)


def test_cost_drift_below_threshold():
    crossed, current, previous = cost_crossed(11.9, 10, 10.0, 10, DEFAULTS)
    assert (crossed, round(current, 2), previous) == (False, 1.19, 1.0)


def test_cost_drift_needs_runs_in_both_windows():
    assert cost_crossed(100.0, 9, 10.0, 10, DEFAULTS) == (False, None, None)
    assert cost_crossed(100.0, 10, 10.0, 9, DEFAULTS) == (False, None, None)


def test_cost_drift_free_baseline_never_crosses():
    assert cost_crossed(5.0, 10, 0.0, 10, DEFAULTS) == (False, 0.5, 0.0)


def test_signal_payload_carries_ids_and_numbers_only():
    target = uuid.uuid4()
    reading = SignalReading("cost_drift", "agent", target, True, 1.2, 1.0, {"runs": 10})
    assert reading.as_signal() == {
        "kind": "cost_drift",
        "target_type": "agent",
        "target_id": str(target),
        "value": 1.2,
        "baseline": 1.0,
        "detail": {"runs": 10},
    }


# ── Digest status ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("cycle_status", "run_status", "skipped", "expected"),
    [
        ("completed", "completed", [], "completed"),
        ("completed", "completed", [{"what": "experiments", "reason": "skipped"}], "partial"),
        ("completed", "failed", [], "failed"),
        ("completed", None, [], "failed"),
        ("budget_reached", "completed", [], "budget_reached"),
        ("failed", "completed", [], "failed"),
        ("cancelled", None, [], "cancelled"),
    ],
)
def test_digest_status_never_shows_a_non_success_as_completed(cycle_status, run_status, skipped, expected):
    assert digest_status(cycle_status, run_status, skipped) == expected


# ── Settings > Agents and automations > Platform Health ────────────────────────────────────────────────────


def test_settings_defaults_are_decision_b18():
    s = HealthSettings()
    assert (
        s.signals.feedback_drop_points,
        s.signals.feedback_window_days,
        s.signals.feedback_min_ratings,
        s.signals.failure_rate_pct,
        s.signals.failure_last_runs,
        s.signals.cost_increase_pct,
        s.signals.cost_window_days,
        s.flow_shadow_days,
        s.flow_watch_days,
        s.proposal_watch_days,
    ) == (10, 3, 10, 5, 20, 20, 7, 3, 7, 7)


@pytest.mark.parametrize(
    "payload",
    [
        {"schedules": {"memory": "not a cron"}},
        {"schedules": {"timezone": "Mars/Olympus"}},
        {"signals": {"failure_rate_pct": 0}},
        {"signals": {"failure_rate_pct": 100}},
        {"signals": {"failure_last_runs": 4}},
        {"signals": {"feedback_window_days": 0}},
        {"flow_watch_days": 0},
        {"flow_shadow_days": 91},
        {"signals": {"unknown": 1}},
    ],
)
def test_settings_refuse_out_of_bounds_values(payload):
    with pytest.raises(ValidationError):
        HealthSettings.model_validate(payload)


def test_stored_settings_drop_a_removed_nested_key():
    stored = {"enabled": True, "schedules": {"memory": "0 1 * * *", "removed_field": 3}}
    settings = _read(stored)
    assert (settings.enabled, settings.schedules.memory) == (True, "0 1 * * *")


def test_stored_settings_that_do_not_validate_read_as_the_defaults():
    assert _read({"enabled": True, "schedules": {"memory": "nope"}}) == HealthSettings()


# ── Seeded fleet ────────────────────────────────────────────────────────────


def test_fleet_has_one_agent_per_role():
    assert sorted(spec.role for spec in fleet_seed.FLEET_AGENTS) == [
        "experimenter",
        "flow_lab",
        "memory_steward",
        "openaev_checker",
        "opencrq_checker",
        "opencti_checker",
        "orchestrator",
        "quality_analyst",
    ]


def test_no_fleet_agent_carries_agent_builder():
    assert [s.slug for s in fleet_seed.FLEET_AGENTS if "agent_builder" in s.builtin_tool_ids] == []


def test_only_the_orchestrator_chats():
    assert [s.slug for s in fleet_seed.FLEET_AGENTS if not s.disable_chat] == [fleet_seed.ORCHESTRATOR_SLUG]


def test_every_assignment_and_handover_names_a_fleet_agent():
    slugs = {s.slug for s in fleet_seed.FLEET_AGENTS}
    assert {a.agent_slug for a in fleet_seed.FLEET_ASSIGNMENTS} <= slugs
    assert {target for s in fleet_seed.FLEET_AGENTS for target, _ in s.handovers} <= slugs


def test_assignment_keys_cover_every_schedule():
    from app.services.health.cycles import SCHEDULE_KEYS

    # The per-target assignments: an investigation, and each module checker's (#101 S14, S15).
    checks = set(fleet_seed.CHECKS_KEY_BY_PROVIDER.values())
    assert set(SCHEDULE_KEYS) | {"investigation"} | checks == set(fleet_seed.ASSIGNMENT_BY_KEY)


@pytest.mark.parametrize("provider", ["opencti", "openaev", "opencrq"])
def test_each_module_checker_writes_no_module_and_hands_nothing_over(provider: str) -> None:
    """Sections 12.3 and 13.1: a checker carries no tool group that writes a module, hands
    nothing over, has its playbook skill and its own checks assignment; only the
    orchestrator delegates to it (the module round of section 13.2).

    ``health_modules`` writes inside XTM One only (recommendations on findings, section 12.4).
    """
    from app.services.health.identity import CHECKER_ROLE_BY_PROVIDER

    spec = next(s for s in fleet_seed.FLEET_AGENTS if s.role == CHECKER_ROLE_BY_PROVIDER[provider])
    assert spec.slug == f"health-{provider}-checker"
    assert spec.disable_chat and spec.handovers == ()
    assert spec.skill_slugs == (f"health-{provider}-checks",)
    assert set(spec.builtin_tool_ids) <= {"health_modules", fleet_seed.IMPROVEMENTS_GROUP}
    assert not set(spec.builtin_tool_ids) & fleet_seed.NON_CONSENT_TOOL_GROUPS
    delegators = [s.slug for s in fleet_seed.FLEET_AGENTS if (spec.slug, "delegate") in s.handovers]
    assert delegators == [fleet_seed.ORCHESTRATOR_SLUG]
    assert fleet_seed.ASSIGNMENT_BY_KEY[fleet_seed.CHECKS_KEY_BY_PROVIDER[provider]].agent_slug == spec.slug


def test_the_opencti_checker_is_the_s14_module_analyst_renamed_in_place() -> None:
    """Section 13.1: same row (former slug), same assignment row (former name)."""
    spec = next(s for s in fleet_seed.FLEET_AGENTS if s.slug == "health-opencti-checker")
    assert "health-module-analyst" in spec.former_slugs
    assert "Platform Health module investigation" in fleet_seed.ASSIGNMENT_BY_KEY["opencti_checks"].former_names
    assert fleet_seed.checks_assignment_names("module_analyst") == fleet_seed.ASSIGNMENT_BY_KEY["opencti_checks"].names
    assert fleet_seed.checks_assignment_names("quality_analyst") == ()


def test_every_playbook_skill_is_shipped() -> None:
    from app.agents.builtin_agents import BUILTIN_SKILLS
    from app.agents.seed import _resolve_seed_content

    slugs = {s["slug"] for s in BUILTIN_SKILLS}
    for provider in ("opencti", "openaev", "opencrq"):
        assert f"health-{provider}-checks" in slugs
        assert _resolve_seed_content("skill_content", f"health-{provider}-checks")


def test_every_persona_file_exists():
    from app.agents.seed import _resolve_seed_content

    assert [s.slug for s in fleet_seed.FLEET_AGENTS if not _resolve_seed_content("agent_persona", s.slug)] == []


# ── Turn scope ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_turn_scope_gives_a_delegated_specialist_its_own_role(monkeypatch):
    """A specialist turn inside an orchestrator's scope acts with the specialist's role, then the scope is restored."""
    from app.services.health import identity
    from app.services.health.turns import health_turn_scoped

    monkeypatch.setattr(identity, "health_enabled", lambda: True)
    orchestrator = SimpleNamespace(id=uuid.uuid4(), health_role="orchestrator", is_company_managed=True)
    specialist = SimpleNamespace(id=uuid.uuid4(), health_role="quality_analyst", is_company_managed=True)
    personal = SimpleNamespace(id=uuid.uuid4(), health_role=None, is_company_managed=False)
    seen: list = []

    @health_turn_scoped
    async def turn(db, user_id, agent):
        seen.append(identity.current_health_principal())

    with identity.acting_as_health_principal(identity.HealthPrincipal(orchestrator.id, "orchestrator")):
        await turn(None, None, specialist)
        await turn(None, None, personal)
        after = identity.current_health_principal()

    assert [(p.agent_id, p.role) if p else None for p in seen] == [(specialist.id, "quality_analyst"), None]
    assert (after.agent_id, after.role) == (orchestrator.id, "orchestrator")


@pytest.mark.asyncio
async def test_a_user_id_that_names_no_user_never_acts_as_the_concierge(monkeypatch):
    """Only a turn with no user at all acts as Platform Health; an id that parses to no user acts as no one."""
    from app.services.health import identity
    from app.services.health.turns import health_turn_scoped

    monkeypatch.setattr(identity, "health_enabled", lambda: True)
    orchestrator = SimpleNamespace(id=uuid.uuid4(), health_role="orchestrator", is_company_managed=True)
    seen: list = []

    @health_turn_scoped
    async def turn(db, user_id, agent):
        seen.append(identity.current_health_principal())

    for user_id in ("telegram:42", "not-a-uuid", None, ""):
        await turn(None, user_id, orchestrator)

    assert [(p.agent_id, p.role) if p else None for p in seen] == [
        None,
        None,
        (orchestrator.id, "orchestrator"),
        (orchestrator.id, "orchestrator"),
    ]


def test_an_untriggered_fleet_run_is_gated_as_no_person():
    """The group AI gate reads the same identity the run takes: nobody for a
    fleet run nobody started, never the assignment's admin owner."""
    from types import SimpleNamespace

    from app.services.assignment_engine import _execution_identity, _run_person

    owner, person = uuid.uuid4(), uuid.uuid4()
    assignment = SimpleNamespace(run_as_user_id=None, user_id=owner)
    fleet = SimpleNamespace(health_role="orchestrator", is_company_managed=True)
    other = SimpleNamespace(health_role=None, is_company_managed=True)
    assert _run_person(assignment, None, fleet) is None
    assert _execution_identity(assignment, None, fleet) is None
    assert _run_person(assignment, person, fleet) == person
    assert _run_person(assignment, None, other) == owner
# ── Delegated work acts with no user ────────────────────────────────────────


@pytest.mark.parametrize(
    ("who", "wired"),
    [
        # A task Platform Health delegated with nobody behind it: its own groups, as its principal.
        ("concierge_alone", {"create_sandbox_copy", "list_improvements"}),
        # The same turn with no principal (the flag switched off): an anonymous caller, nothing.
        ("nobody", set()),
    ],
)
def test_a_delegated_concierge_turn_with_no_user_wires_the_concierge_groups_only(monkeypatch, who, wired):
    from app.services import agent_chat
    from app.services.health import identity

    added: list[str] = []
    composite = SimpleNamespace(add_tools=lambda definitions, _executor: added.extend(d.name for d in definitions))
    experimenter = SimpleNamespace(id=uuid.uuid4(), health_role="experimenter", is_company_managed=True)
    principal = identity.health_principal_for(experimenter, None)
    groups = ["health_sandbox", "improvement_proposals", "flow_builder", "platform_insights", "fleets"]
    if who == "concierge_alone":
        with identity.acting_as_health_principal(principal):
            agent_chat._add_platform_builder_tools(composite, None, "", groups, agent_id=str(experimenter.id))
    else:
        agent_chat._add_platform_builder_tools(composite, None, "", groups, agent_id=str(experimenter.id))
    names = set(added)
    assert wired <= names
    if not wired:
        assert names == set()
    # Never a group that acts as a user: the flow builder, platform insights, fleets.
    assert not names & {"create_flow", "update_flow_graph", "get_platform_overview", "list_fleets"}


def test_a_fleet_run_with_no_user_delegates_under_its_owner_of_record_and_its_tasks_act_with_none():
    """Source contracts on the two giant bodies (the repo's pattern for them):
    the engine offers delegation to the Concierge acting alone and records the
    tasks under ``health_delegation_owner_id``; the worker runs a marked task's
    turn with no user, never as that owner."""
    import inspect

    from app.services import assignment_engine, sub_agent

    engine = inspect.getsource(assignment_engine.execute_assignment_run)
    assert "_delegation_user_id = effective_user_id or await health_delegation_owner_id(db, agent)" in engine
    assert "delegate_available=_delegation_user_id is not None" in engine
    assert "user_id=str(_delegation_user_id)" in engine
    created = inspect.getsource(sub_agent.create_sub_agent_task)
    assert "_health_alone = health_acts_alone()" in created
    assert "user_id=uuid.UUID(user_id) if user_id and not _health_alone else None" in created
    executed = inspect.getsource(sub_agent._execute_sub_agent_task)
    assert "_turn_user_id = task_turn_user_id(task.metadata_json, user_id)" in executed
    # The target agent is reloaded with the turn's identity, never the owner of record.
    assert "_target_user_uuid = uuid.UUID(_turn_user_id) if _turn_user_id else None" in executed
    turn = executed.split("run_agent_chat(", 1)[1]
    assert turn.split(",", 2)[1].strip() == "_turn_user_id"
