"""The seeded CTEM loop fleets run on cases (#891).

A case event starts exactly one fleet run (the two orchestrators split the
trigger paths), the run's prompts and personas make the case the record and
never keep an open item in shared state, the bindings each role needs are
there, and the one-shot migration leaves ``ctem:f1:state`` / ``ctem:f2:state``
with counters only.
"""

from __future__ import annotations

import importlib.util
import pathlib
from types import ModuleType
from typing import Any

from app.agents.builtin_agents import (
    BUILTIN_AGENTS,
    BUILTIN_ASSIGNMENTS,
    CTEM_F1_CASE_ASSIGNMENT,
    CTEM_F2_CASE_ASSIGNMENT,
)
from app.models.ctem_signal import TRIGGER_PATHS
from app.services import platform_events
from app.services.builtin_tools.ctem_cases import CTEM_CASE_AGENT_SLUGS
from app.services.integration_event_filters import payload_filter_reason

_PERSONAS = pathlib.Path(__file__).resolve().parents[1] / "app/agents/seed_data/agent_persona"
_ORCHESTRATORS = ("ctem-vuln-prioritization-orchestrator", "ctem-threat-response-orchestrator")


def _agent(slug: str) -> dict[str, Any]:
    return next(a for a in BUILTIN_AGENTS if a["slug"] == slug)


def test_the_case_assignment_is_a_valid_platform_event_trigger() -> None:
    for assignment in (CTEM_F1_CASE_ASSIGNMENT, CTEM_F2_CASE_ASSIGNMENT):
        assert assignment in BUILTIN_ASSIGNMENTS
        assert assignment["trigger_type"] == "platform_event"
        assert platform_events.trigger_config_error("platform_event", assignment["trigger_config"]) is None
        assert set(assignment["trigger_config"]["event"]) == {
            "ctem_case.opened",
            "ctem_case.reopened",
            "ctem_case.sla_breached",
            "ctem_case.validation_finished",
            "ctem_case.retest_requested",
            "ctem_case.stalled",
        }
        # Seeded switched off, left to an administrator, like every fleet automation.
        assert assignment["enabled"] is False and assignment["admin_opt_in"] is True
    assert CTEM_F1_CASE_ASSIGNMENT["agent_slug"] == "ctem-threat-response-orchestrator"
    assert CTEM_F2_CASE_ASSIGNMENT["agent_slug"] == "ctem-vuln-prioritization-orchestrator"


def test_every_trigger_path_starts_exactly_one_fleet_run() -> None:
    from app.services.ctem_case_routing import owner_of

    for active in (set(), {"ctem-loop"}, {"ctem-vuln-prioritization"}, {"ctem-loop", "ctem-vuln-prioritization"}):
        for path in TRIGGER_PATHS:
            payload = {"case_id": "c", "trigger_path": path, "owner_fleet": owner_of(path, active)}
            heard = [
                a["agent_slug"]
                for a in (CTEM_F1_CASE_ASSIGNMENT, CTEM_F2_CASE_ASSIGNMENT)
                if payload_filter_reason(payload, a["trigger_config"]["filter"]) is None
            ]
            assert len(heard) == 1, (active, path, heard)


def test_an_unstamped_case_event_goes_to_the_ctem_loop() -> None:
    payload = {"case_id": "c", "trigger_path": "new_vulnerability"}
    assert payload_filter_reason(payload, CTEM_F1_CASE_ASSIGNMENT["trigger_config"]["filter"]) is None
    assert payload_filter_reason(payload, CTEM_F2_CASE_ASSIGNMENT["trigger_config"]["filter"]) is not None


def test_the_case_events_carry_the_routing_key() -> None:
    assert "trigger_path" in platform_events._CTEM_CASE_KEYS
    assert "case_id" in platform_events._CTEM_CASE_KEYS


def test_the_orchestrators_hold_the_case_and_signal_tools() -> None:
    assert set(_ORCHESTRATORS) <= CTEM_CASE_AGENT_SLUGS


def test_the_prompts_make_the_case_the_record() -> None:
    for assignment in (CTEM_F1_CASE_ASSIGNMENT, CTEM_F2_CASE_ASSIGNMENT):
        prompt = assignment["prompt"]
        assert "get_ctem_case" in prompt and "record_ctem_case_validation" in prompt
        assert "sync_ctem_case_ticket" in prompt and "never a second loop" in prompt
        assert "ctem_case.retest_requested" in prompt
        # #1736: a run never ends on a next step written as prose only.
        assert "ctem_case.stalled" in prompt and "`ctem-loop-reminder:`" in prompt
    assert "`ctem:f1:case:`" in CTEM_F1_CASE_ASSIGNMENT["prompt"]
    assert "`ctem:f2:case:`" in CTEM_F2_CASE_ASSIGNMENT["prompt"]
    retests = [a for a in BUILTIN_ASSIGNMENTS if a["name"] == "Re-test due" and a["agent_slug"] in _ORCHESTRATORS]
    assert len(retests) == 2
    for assignment in retests:
        assert "case_id" in assignment["prompt"] and "record_ctem_case_validation" in assignment["prompt"]


def test_the_loop_raises_decisions_instead_of_notes() -> None:
    """#1828: an SLA breach, contradicting answers, a re-plan and a no-option answer are decisions."""
    for assignment in (CTEM_F1_CASE_ASSIGNMENT, CTEM_F2_CASE_ASSIGNMENT):
        prompt = assignment["prompt"]
        breach = prompt.split("On ctem_case.sla_breached:", 1)[1]
        assert "needs_human decision keyed `ctem:remediation:`" in breach
        assert "recommendation" in breach and "contradict" in breach
        assert "`Verdict PHASE: proven affected | proven not affected | unproven`" in prompt
    gates = [a for a in BUILTIN_ASSIGNMENTS if a["name"] == "Gate answered" and a["agent_slug"] in _ORCHESTRATORS]
    assert {a["agent_slug"] for a in gates} == set(_ORCHESTRATORS)
    for assignment in gates:
        prompt = assignment["prompt"]
        assert "file the review decision again under the same key with the new draft link" in prompt
        assert "picks no option and decides nothing" in prompt
        assert "file the same decision again under the same key" in prompt


def test_no_open_item_lives_only_in_shared_state() -> None:
    for slug in _ORCHESTRATORS:
        persona = (_PERSONAS / f"{slug}.md").read_text()
        assert "counters only" in persona and "get_ctem_case" in persona, slug
        assert "open cases by threat id" not in persona and "one entry per CVE" not in persona, slug
    for assignment in BUILTIN_ASSIGNMENTS:
        if assignment.get("agent_slug") in _ORCHESTRATORS:
            prompt = assignment["prompt"]
            assert "Read ctem:f2:state" not in prompt and "already open in ctem:f1:state" not in prompt


def test_each_role_binds_the_modules_it_needs() -> None:
    assert set(_agent("ctem-vuln-triage-analyst")["integration_template_bindings"]) >= {"opencti", "openaev"}
    assert set(_agent("ctem-remediation-coordinator")["integration_template_bindings"]) >= {"openaev", "opencrq"}


def _migration() -> ModuleType:
    path = next(pathlib.Path(__file__).resolve().parents[1].glob("alembic/versions/c891ctemfleetcase1_*.py"))
    spec = importlib.util.spec_from_file_location("c891ctemfleetcase1", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_keeps_counters_only() -> None:
    module = _migration()
    value = {
        "open": [{"cve": "CVE-1"}, {"cve": "CVE-2"}],
        "closed": [{"cve": "CVE-0"}],
        "last_pass": "2026-11-20T06:00:00Z",
        "cve_triaged": 12,
        "cve_validated": 3,
        "flag": True,
        "counters": {"resolved": 4, "note": "x"},
    }
    kept, opened, dropped = module.counters_only(value)
    assert kept == {
        "last_pass": "2026-11-20T06:00:00Z",
        "cve_triaged": 12,
        "cve_validated": 3,
        "counters": {"resolved": 4},
    }
    assert opened == 2 and dropped == ["closed", "flag", "open"]
    assert module.counters_only({"cve_triaged": 1}) == ({"cve_triaged": 1}, 0, [])


def test_the_migration_note_says_the_open_items_are_discarded_and_lists_them() -> None:
    module = _migration()
    value = {"open": [{"cve": "CVE-1"}, {"threat_id": "T-9"}, "CVE-3", {}], "cases": {"CVE-4": {"state": "open"}}}
    ids = module.open_item_ids(value)
    assert ids == ["CVE-1", "T-9", "CVE-3", "CVE-4"]
    note = module.discard_note(5, ["cases", "open"], ids)
    assert "5 open item(s) discarded" in note and "not re-recorded" in note
    assert note.endswith("Discarded (first 4 of 5): CVE-1, T-9, CVE-3, CVE-4.")
    assert "re-records each" not in module.__doc__ and "DISCARDED" in module.__doc__
    many = module.open_item_ids({"open": [{"cve": f"CVE-{i}"} for i in range(80)]})
    assert len(many) == module.MAX_LISTED
    assert module.discard_note(0, ["closed"], []).endswith("new signal.")


def test_the_migration_matches_the_two_fleet_keys_only() -> None:
    module = _migration()
    assert module.is_fleet_state_key("ctem:f1:state")
    assert module.is_fleet_state_key("workspace:abc:ctem:f2:state")
    assert not module.is_fleet_state_key("ctem:f3:state")
    assert not module.is_fleet_state_key("ctem:f2:state:archive")
