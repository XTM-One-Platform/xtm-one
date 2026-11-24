"""A person's answer to a waiting CTEM gate resumes the case (#1388)."""

from __future__ import annotations

import pathlib
import re
from typing import Any

import pytest

from app.agents import openaev_detection_desk, opencrq_risk_budget_desk, opencti_pir_desk
from app.agents.builtin_agents import (
    BUILTIN_ASSIGNMENTS,
    CTEM_F1_GATE_PREFIXES,
    CTEM_F2_GATE_PREFIXES,
    CTEM_GATE_ANSWERED,
    CTEM_KEY_PREFIX,
)
from app.agents.gate_answered import INHERIT_SWITCH_FROM_KEY
from app.services.integration_event_filters import payload_filter_reason

_PERSONAS = pathlib.Path(__file__).resolve().parents[1] / "app" / "agents" / "seed_data" / "agent_persona"
_FLEETS = {
    "ctem-threat-response-orchestrator": CTEM_F1_GATE_PREFIXES,
    "ctem-vuln-prioritization-orchestrator": CTEM_F2_GATE_PREFIXES,
}
#: Keys the personas name that are not gates: the case loop, the re-test and the counters.
_NOT_GATES = re.compile(r"^ctem:f[12]:((case|retest|state):)?$")
#: Catalog keys kept for decision items filed before #1832, which the personas no longer name:
#: the launch gate is keyed ``ctem:validation:`` followed by the case id.
_LEGACY_ONLY = frozenset({"ctem:f1:launch:"})


def _gate_assignment(slug: str) -> dict[str, Any]:
    found = [a for a in BUILTIN_ASSIGNMENTS if a["agent_slug"] == slug and a["name"] == CTEM_GATE_ANSWERED]
    assert len(found) == 1, slug
    return found[0]


def _persona_keys(slug: str) -> set[str]:
    text = (_PERSONAS / f"{slug}.md").read_text(encoding="utf-8")
    return {k for k in re.findall(r"`(ctem:[a-z0-9:-]*:)", text) if not _NOT_GATES.match(k)}


@pytest.mark.parametrize("slug", sorted(_FLEETS))
def test_every_gate_key_the_persona_names_is_covered_by_the_trigger(slug: str) -> None:
    prefixes = tuple(_gate_assignment(slug)["trigger_config"]["filter"]["correlation_key"]["starts_with"])
    # The whole ctem: namespace: a key minted outside the catalog resumes the case too (#1388 follow-up).
    assert prefixes == (CTEM_KEY_PREFIX,)
    named = _persona_keys(slug)
    assert named, slug
    # The persona still names its catalog, every key of it under the listened namespace.
    assert set(_FLEETS[slug]) - _LEGACY_ONLY <= named, (slug, set(_FLEETS[slug]) - _LEGACY_ONLY - named)
    assert not _LEGACY_ONLY & named, (slug, _LEGACY_ONLY & named)
    uncovered = {k for k in named if not k.startswith(prefixes)}
    assert uncovered == set(), (slug, uncovered)


@pytest.mark.parametrize("slug", sorted(_FLEETS))
def test_the_trigger_hears_the_answer_only(slug: str) -> None:
    config = _gate_assignment(slug)["trigger_config"]
    assert config["event"] == "work_item.status_changed"
    assert config["scope"] == "own"
    answered = {
        "previous_status": "blocked",
        "status": "open",
        "correlation_key": "ctem:validation:abc",
        "answered": True,
    }
    assert payload_filter_reason(answered, config["filter"]) is None
    # A decision keyed outside the catalog (an evidence request, a blocker) is heard too.
    for off_catalog in ("ctem:f1:version-read:abc", "ctem:f1:jira-reauth:abc", "ctem:f2:evidence:abc"):
        assert payload_filter_reason({**answered, "correlation_key": off_catalog}, config["filter"]) is None
    for other in (
        {**answered, "previous_status": "open", "status": "claimed"},
        {**answered, "status": "done"},
        # A dependency release of a case or re-test item is not an answer.
        {**answered, "correlation_key": "ctem:f1:retest:abc", "answered": None},
        {key: v for key, v in answered.items() if key != "answered"} | {"correlation_key": "ctem:f2:case:abc"},
        {**answered, "correlation_key": "opencti:pir-desk:approval:abc"},
    ):
        assert payload_filter_reason(other, config["filter"]) is not None, other


@pytest.mark.parametrize("slug", sorted(_FLEETS))
def test_the_run_claims_before_acting_and_stops_on_a_rejection(slug: str) -> None:
    a = _gate_assignment(slug)
    prompt = a["prompt"]
    assert prompt.index("claim_work_item") < prompt.index("update_ctem_case")
    assert "refused" in prompt and "stop there" in prompt
    assert "Rejected" in prompt and "runs nothing the decision covered" in prompt
    assert a["enabled"] is False and a["admin_opt_in"] is True
    assert a[INHERIT_SWITCH_FROM_KEY] == "Case opened or reopened"
    assert any(b["agent_slug"] == slug and b["name"] == a[INHERIT_SWITCH_FROM_KEY] for b in BUILTIN_ASSIGNMENTS)


#: The product desks: each decision key prefix it files for a person, and the persona or
#: assignment text that must name it.
_DESKS = {
    opencti_pir_desk.ORCHESTRATOR: (opencti_pir_desk.APPROVAL_PREFIX,),
    opencrq_risk_budget_desk.ORCHESTRATOR: (
        opencrq_risk_budget_desk.RECOMMENDATION_PREFIX,
        opencrq_risk_budget_desk.APPROVAL_PREFIX,
    ),
    openaev_detection_desk.ORCHESTRATOR: (openaev_detection_desk.LAUNCH_PREFIX,),
}


@pytest.mark.parametrize("slug", sorted(_DESKS))
def test_every_desk_decision_resumes_its_orchestrator(slug: str) -> None:
    a = _gate_assignment(slug)
    config = a["trigger_config"]
    assert config["event"] == "work_item.status_changed" and config["scope"] == "own"
    assert tuple(config["filter"]["correlation_key"]["starts_with"]) == _DESKS[slug]
    # The key the desk files is the key the trigger takes: named in its assignments' prompts.
    filed = " ".join(b["prompt"] for b in BUILTIN_ASSIGNMENTS if b["agent_slug"] == slug and b is not a)
    for prefix in _DESKS[slug]:
        assert prefix in filed, (slug, prefix)
    prompt = a["prompt"]
    assert prompt.index("claim_work_item") < prompt.index("payload.answer")
    assert "stop there" in prompt and "runs nothing the decision covered" in prompt
    assert a["enabled"] is False and a["admin_opt_in"] is True
    assert any(b["agent_slug"] == slug and b["name"] == a[INHERIT_SWITCH_FROM_KEY] for b in BUILTIN_ASSIGNMENTS)


def test_the_risk_desk_persona_keys_its_register_writes() -> None:
    text = (_PERSONAS / f"{opencrq_risk_budget_desk.ORCHESTRATOR}.md").read_text(encoding="utf-8")
    assert f"`{opencrq_risk_budget_desk.APPROVAL_PREFIX}`" in text
    prompt = _gate_assignment(opencrq_risk_budget_desk.ORCHESTRATOR)["prompt"]
    # An approved register write is made by a person: the run files their follow-up, never the write.
    assert opencrq_risk_budget_desk.APPROVAL_PREFIX in prompt and "never by you" in prompt


def test_the_detection_desk_persona_keys_its_launches() -> None:
    text = (_PERSONAS / f"{openaev_detection_desk.ORCHESTRATOR}.md").read_text(encoding="utf-8")
    assert f"`{openaev_detection_desk.LAUNCH_PREFIX}`" in text


def test_the_re_test_keeps_its_single_path() -> None:
    for prefixes in _FLEETS.values():
        assert not any("retest" in p for p in prefixes)


def test_the_case_page_keys_are_gate_keys() -> None:
    # The case page reads ctem:validation:<case> (case-validation.ts) and ctem:review:<case>.
    for prefixes in _FLEETS.values():
        assert "ctem:validation:" in prefixes and "ctem:review:" in prefixes


def test_the_seed_gives_a_new_listener_its_siblings_switch() -> None:
    import inspect

    from app.agents import seed

    src = inspect.getsource(seed.seed_builtin_assignments)
    assert "data.get(INHERIT_SWITCH_FROM_KEY)" in src
    assert "seeded.run_as_user_id = sibling.run_as_user_id" in src
    assert "is_emergency_stopped(sibling)" in src
