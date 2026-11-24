"""CTEM fleet F1, Threat-informed exposure response (dev-docs/ctem-fleets.md s.4.1, #9114).

Since CTEM-31 (#1048) F1 is the core of the generic CTEM loop fleet (``ctem-loop``):
its orchestrator is the loop root, its roles are the loop's own roles, and the
posture and module-desk roles hang below it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.builtin_agents import BUILTIN_AGENTS, BUILTIN_ASSIGNMENTS, CTEM_FLEET_AGENTS, CTEM_LOOP_ROLE_LEADS
from app.agents.builtin_fleets import BUILTIN_FLEETS
from app.agents.builtin_roi_units import BUILTIN_ROI_UNITS
from app.services.opencti_trigger_provisioning import read_provision_spec

_SEED_DATA = Path(__file__).resolve().parents[1] / "app" / "agents" / "seed_data"
_ORCH = "ctem-threat-response-orchestrator"
_SHARED = ("ctem-threat-relevance-analyst", "ctem-risk-quantifier", "ctem-remediation-coordinator")
_CORE_ROLES = {
    "ctem-threat-relevance-analyst",
    "openaev-scenario-creator",
    "ctem-risk-quantifier",
    "ctem-remediation-coordinator",
    "opencti-threat-hunt-creator",
    "opencti-detection-rule-converter",
}


def _fleet() -> dict:
    (fleet,) = [f for f in BUILTIN_FLEETS if f["slug"] == "ctem-loop"]
    return fleet


def _agents() -> dict[str, dict]:
    return {a["slug"]: a for a in BUILTIN_AGENTS}


def _f1_assignments() -> dict[str, dict]:
    return {a["name"]: a for a in BUILTIN_ASSIGNMENTS if a["agent_slug"] == _ORCH}


def test_every_member_and_connected_agent_is_a_builtin():
    fleet, agents = _fleet(), _agents()
    named = [m["agent"] for m in fleet["members"]] + [c["agent"] for c in fleet["connected"]]
    named += [h["from"] for h in fleet["entry_handovers"]]
    assert [slug for slug in named if slug not in agents] == []


def test_the_orchestrator_is_the_single_top_level_member():
    fleet = _fleet()
    assert fleet["orchestrator"] == _ORCH
    top = [m["agent"] for m in fleet["members"] if not m.get("parent")]
    assert top == [_ORCH]
    members = {m["agent"] for m in fleet["members"]}
    assert all(m["parent"] in members for m in fleet["members"] if m.get("parent"))


def test_the_spec_members_are_seeded_with_their_roles():
    roles = {m["agent"]: m["role_label"] for m in _fleet()["members"] if m["agent"] in {_ORCH, *_CORE_ROLES}}
    assert roles == {
        _ORCH: "Orchestrator",
        "ctem-threat-relevance-analyst": "Relevance",
        "openaev-scenario-creator": "Validation",
        "ctem-risk-quantifier": "Risk",
        "ctem-remediation-coordinator": "Mobilization",
        "opencti-threat-hunt-creator": "Hunt",
        "opencti-detection-rule-converter": "Detections",
    }


def test_the_orchestrator_delegates_to_its_direct_reports():
    orch = _agents()[_ORCH]
    direct = {m["agent"] for m in _fleet()["members"] if m.get("parent") == _ORCH}
    assert set(orch["handover_agent_slugs"]) == direct
    assert set(CTEM_LOOP_ROLE_LEADS) <= direct
    assert set(orch["handover_type_slugs"].values()) == {"delegate"}
    # The blackboard and the approval queue of every environment write.
    assert {"shared_state", "work_items"} <= set(orch["builtin_tool_ids"])


@pytest.mark.parametrize("slug", [_ORCH, *_SHARED])
def test_each_new_agent_has_a_persona_file(slug):
    persona = (_SEED_DATA / "agent_persona" / f"{slug}.md").read_text(encoding="utf-8")
    assert "get_organization_context" in persona


@pytest.mark.parametrize("slug", _SHARED)
def test_the_shared_role_agents_declare_their_roi_unit_and_schema(slug):
    agent = _agents()[slug]
    assert agent["output_format"] == "json"
    assert (_SEED_DATA / "agent_output_schema" / f"{agent['output_schema']}.json").is_file()
    assert slug in BUILTIN_ROI_UNITS


def test_the_new_agents_are_seeded_once():
    slugs = [a["slug"] for a in BUILTIN_AGENTS]
    assert all(slugs.count(a["slug"]) == 1 for a in CTEM_FLEET_AGENTS)


def test_the_spec_assignments_live_on_the_orchestrator() -> None:
    assignments = _f1_assignments()
    # The three of the spec, plus the case loop (#891), the PIR score trigger (CTEM-08) and the
    # threat signal run (#1580).
    assert set(assignments) == {
        "New relevant threat",
        "Threat activity flagged in a PIR",
        "Threat signal recorded",
        "Weekly threat sweep",
        "Re-test due",
        "Case opened or reopened",
        "Gate answered",
        "Loop reminder due",
        "Fleet run failed or orphaned",
    }
    # #1837: the loop orchestrator listens to its members' failed and orphaned runs, in batches.
    health = assignments["Fleet run failed or orphaned"]["trigger_config"]
    assert health["event"] == ["run.failed", "run.orphaned"]
    assert health["scope"] == "fleet"
    assert health["batch_window_seconds"] == 60
    # Staggered off the Monday 07:00 of the desks (#1832).
    assert assignments["Weekly threat sweep"]["trigger_config"] == {"cron": "30 6 * * 1"}
    assert assignments["Re-test due"]["trigger_type"] == "work_item"
    assert assignments["Re-test due"]["trigger_config"] == {"correlation_prefix": "ctem:f1:retest:"}


def test_the_new_threat_trigger_is_provisioned_from_the_organization_profile():
    trigger = _f1_assignments()["New relevant threat"]
    assert trigger["trigger_type"] == "integration_event"
    spec = read_provision_spec(trigger["trigger_config"])
    assert spec is not None
    assert set(spec["entity_types"]) == {"Intrusion-Set", "Campaign", "Report"}
    assert spec["event_types"] == ["create"]
    assert set(spec["organization_context"]) == {"sector", "regions"}


def test_the_pir_threat_trigger_is_a_pir_playbook_held_to_the_policy_threshold() -> None:
    trigger = _f1_assignments()["Threat activity flagged in a PIR"]
    spec = read_provision_spec(trigger["trigger_config"])
    assert spec is not None
    assert spec["mode"] == "pir_playbook"
    assert spec["organization_context"] == ["pir_ids"]
    assert "Vulnerability" not in spec["entity_types"]


# ── #1580: the OpenCTI seeds are the intake, the signal event starts the run ──


@pytest.mark.parametrize("name", ["New relevant threat", "Threat activity flagged in a PIR"])
def test_the_opencti_threat_seeds_provision_and_only_listen(name):
    from app.agents.builtin_agents import CTEM_LISTEN_ONLY_FILTER
    from app.services.integration_event_filters import payload_filter_reason

    trigger = _f1_assignments()[name]
    assert trigger["trigger_type"] == "integration_event"
    assert read_provision_spec(trigger["trigger_config"]) is not None
    assert trigger["trigger_config"]["filter"] == CTEM_LISTEN_ONLY_FILTER
    assert "pir_min_score" not in trigger["trigger_config"]
    # A real OpenCTI delivery never passes: the signal is recorded by the webhook route.
    delivery = {"event_type": "create", "data": {"type": "intrusion-set", "id": "intrusion-set--1"}}
    assert payload_filter_reason(delivery, trigger["trigger_config"]["filter"]) is not None
    assert "record_ctem_signal" not in trigger["prompt"]


def test_the_threat_signal_run_listens_to_both_signal_events_with_an_explicit_cap():
    from app.agents.gate_answered import INHERIT_SWITCH_FROM_KEY
    from app.services.assignment_dispatch import assignment_concurrency_cap, seeded_trigger_config
    from app.services.ctem_signals import SIGNAL_EVENTS

    run = _f1_assignments()["Threat signal recorded"]
    assert run["trigger_type"] == "platform_event"
    config = run["trigger_config"]
    assert tuple(config["event"]) == SIGNAL_EVENTS
    assert config["scope"] == "all"
    assert config["ordering_key"] == "signal_id"
    # Explicit, never the seeded event default of 1 (#1460).
    assert config["max_concurrent_runs"] > 1
    assert (
        assignment_concurrency_cap(seeded_trigger_config(run["trigger_type"], config)) == config["max_concurrent_runs"]
    )
    assert run[INHERIT_SWITCH_FROM_KEY] == "New relevant threat"
    assert (run["enabled"], run["admin_opt_in"]) == (False, True)


def _signal_event(**overrides):
    return {
        "signal_id": "s1",
        "trigger_path": "relevant_threat",
        "lens": "world",
        "source_provider": "opencti",
        "subject_kind": "threat",
        "verdict": "to_validate",
        "case_id": None,
        "pir_floor_met": True,
        **overrides,
    }


@pytest.mark.parametrize(
    ("overrides", "runs"),
    [
        ({}, True),
        ({"verdict": None}, True),
        ({"verdict": "confirmed"}, False),  # the case loop's
        ({"verdict": "watchlist"}, False),
        ({"verdict": "discarded"}, False),
        ({"trigger_path": "new_vulnerability"}, False),
        ({"source_provider": "manual"}, False),
        ({"pir_floor_met": False}, False),  # below the relevance floor (#1678): the weekly sweep's
    ],
)
def test_the_threat_signal_filter_keeps_the_signals_no_case_loop_owns(overrides, runs):
    from app.services.integration_event_filters import payload_filter_reason

    config = _f1_assignments()["Threat signal recorded"]["trigger_config"]
    assert (payload_filter_reason(_signal_event(**overrides), config["filter"]) is None) is runs


# ── #1580 upgrade: an installation seeded with the old run-on-delivery triggers ──


def _seed_db(agent, existing, sibling=None):
    from unittest.mock import AsyncMock, MagicMock

    from tests.test_builtin_assignments_never_disabled import _Result

    results = [
        _Result(scalar=agent),  # phase 1: agent lookup for the stale sweep
        _Result(items=[]),  # phase 1: no stale assignments
        _Result(scalar=agent),  # phase 2: agent lookup
        _Result(scalar=existing, items=[existing] if existing else []),
    ]
    if existing is None:
        results.append(_Result(scalar=sibling, items=[sibling] if sibling else []))
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=results)
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.add = MagicMock()
    return db


def _orchestrator_row():
    import uuid
    from unittest.mock import MagicMock

    agent = MagicMock()
    agent.id = uuid.uuid4()
    agent.slug = _ORCH
    agent.user_id = uuid.uuid4()
    agent.is_company_managed = True
    return agent


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_an_old_intake_row_becomes_listen_only_keeping_its_switch_and_provisioning(monkeypatch, enabled):
    import uuid
    from unittest.mock import MagicMock

    from app.agents.builtin_agents import CTEM_LISTEN_ONLY_FILTER
    from app.agents.seed import seed_builtin_assignments
    from app.services.opencti_trigger_provisioning import STATE_KEY

    monkeypatch.setattr("app.agents.seed.BUILTIN_ASSIGNMENTS", [_f1_assignments()["New relevant threat"]])
    run_as = uuid.uuid4()
    existing = MagicMock()
    existing.enabled = enabled
    existing.run_as_user_id = run_as
    existing.user_id = uuid.uuid4()
    existing.prompt = "OpenCTI created the entity in the trigger below. ... record_ctem_signal ..."
    existing.trigger_config = {"provider": "opencti", "provision": {}, STATE_KEY: {"trigger_ids": ["t1"]}}

    inserted = await seed_builtin_assignments(_seed_db(_orchestrator_row(), existing), platform_mode="xtm_one")

    assert inserted == 0
    assert existing.enabled is enabled
    assert existing.run_as_user_id == run_as
    assert existing.trigger_config["filter"] == CTEM_LISTEN_ONLY_FILTER
    assert existing.trigger_config[STATE_KEY] == {"trigger_ids": ["t1"]}
    assert "record_ctem_signal" not in existing.prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("intake_on", [True, False])
async def test_the_threat_signal_run_takes_the_intake_switch_on_upgrade(monkeypatch, intake_on):
    import uuid
    from unittest.mock import MagicMock

    from app.agents.seed import seed_builtin_assignments

    monkeypatch.setattr("app.agents.seed.BUILTIN_ASSIGNMENTS", [_f1_assignments()["Threat signal recorded"]])
    monkeypatch.setattr("app.agents.seed.is_emergency_stopped", lambda _a: False)
    sibling = MagicMock()
    sibling.enabled = intake_on
    sibling.run_as_user_id = uuid.uuid4()
    db = _seed_db(_orchestrator_row(), None, sibling)

    inserted = await seed_builtin_assignments(db, platform_mode="xtm_one")

    assert inserted == 1
    (seeded,) = [call.args[0] for call in db.add.call_args_list]
    assert seeded.enabled is intake_on
    assert seeded.trigger_type == "platform_event"
    assert seeded.trigger_config["max_concurrent_runs"] > 1
    if intake_on:
        assert seeded.run_as_user_id == sibling.run_as_user_id
    else:
        assert seeded.paused_reason == "created_disabled"
