"""Cost and precision guards over every seeded fleet and built-in assignment.

A new seeded fleet, assignment or skill that breaks one of these fails CI:

* every scheduled or event-triggered built-in assignment carries its own
  ``max_iterations`` (never the agent's or the platform's cap by inheritance);
* every event-triggered one narrows its events before a run exists: a
  ``filter``, a source-side ``provision`` (the OpenCTI / OpenAEV trigger the
  platform creates with its entity and event types) or, for a work-item
  trigger, a ``correlation_prefix``;
* every agent a seeded fleet names (member, orchestrator, connected agent,
  entry handover) and every built-in agent slug a fleet agent's persona or a
  fleet assignment's prompt cites is seeded;
* no seeded skill holds a section over 20,000 characters without a sub-heading
  a run can load alone;
* a fleet desk (an agent tagged ``fleet`` that is a member but not the
  orchestrator) is reached through its orchestrator only: chat disabled, never
  exposed, so the orchestrator is the fleet's one conversational entry point;
* every fix-and-check loop a seeded skill describes ("fix, preview again",
  "save again until") states its round cap ("at most N") in the same line;
* every fleet orchestrator has chat enabled and is never declared unexposed;
* every mail-triggered default assignment of a built-in agent template drops
  meeting messages before a run (``exclude_meeting_messages``);
* a zero-token gate a seed entry declares (``activation_check``,
  ``ignore_self_triggered_events``) parses, and the boot sync writes it on the
  row it creates or rewrites, and leaves the row alone when the entry has none.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from app.agents.builtin_agents import BUILTIN_AGENTS, BUILTIN_ASSIGNMENTS, BUILTIN_SKILLS
from app.agents.builtin_fleets import BUILTIN_FLEETS
from app.agents.seed import _resolve_seed_content, _sync_seeded_gates
from app.agents.templates import BUILTIN_TEMPLATES
from app.services.assignment_activation_check import parse_activation_check
from app.services.integration_event_filters import MAIL_EVENTS, event_filter_reason

_AUTOMATIC = ("schedule", "integration_event", "platform_event", "work_item")
_EVENT = ("integration_event", "platform_event", "work_item")
_EVENT_NARROWING_KEYS = ("filter", "provision", "correlation_prefix")
_SECTION_CEILING = 20_000
_AGENT_SLUGS = {a["slug"] for a in BUILTIN_AGENTS}
#: A slug of a product agent family, as a persona or a prompt cites one.
#: A slug glued to a colon and more (`ctem-loop-reminder:<case>`) is a correlation key prefix, not a slug.
_CITED_SLUG = re.compile(r"(?<![\w/.:-])((?:ctem|opencti|openaev|opencrq)-[a-z0-9]+(?:-[a-z0-9]+)+)(?![\w/.-]|:\S)")


def _key(a: dict) -> str:
    return f"{a['agent_slug']} / {a['name']}"


def _fleet_agent_slugs(fleet: dict) -> set[str]:
    slugs = {fleet["orchestrator"]}
    slugs |= {m["agent"] for m in fleet.get("members", [])}
    slugs |= {m["parent"] for m in fleet.get("members", []) if m.get("parent")}
    slugs |= {c["agent"] for c in fleet.get("connected", [])}
    slugs |= {h["from"] for h in fleet.get("entry_handovers", [])}
    return slugs


def test_every_automatic_assignment_sets_its_own_iteration_cap():
    missing = [
        _key(a)
        for a in BUILTIN_ASSIGNMENTS
        if a["trigger_type"] in _AUTOMATIC and not isinstance(a.get("max_iterations"), int)
    ]
    assert not missing, f"set max_iterations on: {missing}"


def test_every_event_assignment_narrows_its_events_before_a_run():
    unfiltered = [
        _key(a)
        for a in BUILTIN_ASSIGNMENTS
        if a["trigger_type"] in _EVENT
        and not any((a.get("trigger_config") or {}).get(k) for k in _EVENT_NARROWING_KEYS)
    ]
    assert not unfiltered, f"give a filter, provision or correlation_prefix to: {unfiltered}"


@pytest.mark.parametrize("fleet", BUILTIN_FLEETS, ids=lambda f: f["slug"])
def test_every_agent_a_fleet_names_is_seeded(fleet):
    assert _fleet_agent_slugs(fleet) <= _AGENT_SLUGS, sorted(_fleet_agent_slugs(fleet) - _AGENT_SLUGS)


@pytest.mark.parametrize("fleet", BUILTIN_FLEETS, ids=lambda f: f["slug"])
def test_every_agent_slug_a_fleet_persona_or_prompt_cites_is_seeded(fleet):
    members = {m["agent"] for m in fleet.get("members", [])} | {fleet["orchestrator"]}
    agents = {a["slug"]: a for a in BUILTIN_AGENTS}
    texts = [_resolve_seed_content("agent_persona", agents[s].get("persona", s)) or "" for s in members if s in agents]
    texts += [a["prompt"] for a in BUILTIN_ASSIGNMENTS if a["agent_slug"] in members]
    skills = {s["slug"] for s in BUILTIN_SKILLS}
    cited = {m for text in texts for m in _CITED_SLUG.findall(text)}
    unknown = sorted(c for c in cited if c not in _AGENT_SLUGS and c not in skills)
    assert not unknown, f"cited but not seeded: {unknown}"


@pytest.mark.parametrize("fleet", BUILTIN_FLEETS, ids=lambda f: f["slug"])
def test_fleet_desks_are_reached_through_the_orchestrator_only(fleet):
    agents = {a["slug"]: a for a in BUILTIN_AGENTS}
    desks = [
        m
        for m in fleet.get("members", [])
        if m["agent"] != fleet["orchestrator"] and "fleet" in (agents.get(m["agent"], {}).get("tags") or [])
    ]
    # A role lead the generic fleet folded in that answers people in chat keeps it (CTEM-31):
    # the posture lead answers where the organization stands, as before the fold.
    answers_people = set(fleet.get("chat_members") or [])
    chatty = [
        m["agent"] for m in desks if not agents[m["agent"]].get("disable_chat") and m["agent"] not in answers_people
    ]
    exposed = [m["agent"] for m in desks if m.get("exposed")]
    assert not chatty, f"set disable_chat on the fleet desks: {chatty}"
    assert not exposed, f"expose the orchestrator only, not: {exposed}"


@pytest.mark.parametrize("fleet", BUILTIN_FLEETS, ids=lambda f: f["slug"])
def test_every_fleet_orchestrator_is_reachable(fleet):
    # The fleet seed always exposes the orchestrator. A person talks to it only
    # when it answers people (the posture fleet); a run orchestrator (a JSON
    # run or case schema) or one the CTEM Assistant delegates to
    # (entry_handovers) keeps chat off: delegation and assignments never read
    # disable_chat, only the chat picker does.
    agents = {a["slug"]: a for a in BUILTIN_AGENTS}
    root = agents.get(fleet["orchestrator"], {})
    if root.get("disable_chat"):
        assert root.get("output_schema") or fleet.get("entry_handovers"), (
            f"{fleet['orchestrator']} has chat off and nothing reaches it"
        )
    assert all(m.get("exposed", True) for m in fleet.get("members", []) if m["agent"] == fleet["orchestrator"])


def _sections(body: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    head = "(top)"
    for part in re.split(r"(?m)^(#{1,6} .*)$", body):
        if re.match(r"^#{1,6} ", part):
            head = part
        else:
            out.append((head, len(part)))
    return out


def test_every_seeded_activation_check_parses():
    refused = {
        _key(a): error
        for a in BUILTIN_ASSIGNMENTS
        if a.get("activation_check") is not None
        for _value, error, _provided in [parse_activation_check(a["activation_check"])]
        if error is not None
    }
    assert not refused, refused


_CHECK = {"tool": "list_work_items", "arguments": {"open_only": True}, "condition": "non_empty"}


def test_the_boot_sync_writes_the_gates_a_seed_entry_declares():
    row = SimpleNamespace(activation_check=None, ignore_self_triggered_events=False)
    _sync_seeded_gates(row, {"name": "x", "activation_check": _CHECK, "ignore_self_triggered_events": True})
    assert row.activation_check == _CHECK
    assert row.ignore_self_triggered_events is True


def test_the_boot_sync_leaves_a_row_alone_when_the_entry_declares_no_gate():
    row = SimpleNamespace(activation_check=dict(_CHECK), ignore_self_triggered_events=True)
    _sync_seeded_gates(row, {"name": "x"})
    assert row.activation_check == _CHECK
    assert row.ignore_self_triggered_events is True


def test_the_boot_sync_clears_a_check_the_entry_sets_to_none_and_stores_no_invalid_one():
    row = SimpleNamespace(activation_check=dict(_CHECK))
    _sync_seeded_gates(row, {"name": "x", "activation_check": None})
    assert row.activation_check is None
    _sync_seeded_gates(row, {"name": "x", "activation_check": {"tool": "delete_work_item"}})
    assert row.activation_check is None


_LOOP = re.compile(r"(?i)\bfix, (?:preview|save|render|check) again\b|\bagain until\b")
_ROUND_CAP = re.compile(r"(?i)\bat most \d+\b")


@pytest.mark.parametrize("skill", BUILTIN_SKILLS, ids=lambda s: s["slug"])
def test_every_seeded_quality_loop_states_its_round_cap(skill):
    body = _resolve_seed_content("skill_content", skill.get("content", skill["slug"])) or ""
    uncapped = [line[:120] for line in body.splitlines() if _LOOP.search(line) and not _ROUND_CAP.search(line)]
    assert not uncapped, f"give each loop its round cap and what happens at the cap: {uncapped}"


def test_the_video_production_loops_are_capped():
    body = _resolve_seed_content("skill_content", "video-production") or ""
    assert "at most 6 rounds on one video" in body
    assert "at most 3 previews of one scene" in body
    assert "At most 3 regenerations of one shot" in body


@pytest.mark.parametrize("slug", ["video-production", "presentation-design", "report-design"])
def test_every_studio_skill_lets_the_person_choose(slug):
    # Sam, 13 Nov 2026 (#11538): the studio skills make the person aware and never impose a taste.
    body = _resolve_seed_content("skill_content", slug) or ""
    assert "**The person chooses; you make them aware.**" in body
    assert "the default you take" in body


def test_the_video_production_skill_imposes_no_music():
    body = _resolve_seed_content("skill_content", "video-production") or ""
    assert "Every video gets music" not in body
    assert '"no music" as an equal option' in body
    assert 'music: {"enabled": false}' in body


def _template_mail_assignments() -> list[tuple[str, dict]]:
    return [
        (f"{t['slug']} / {a['name']}", a)
        for t in BUILTIN_TEMPLATES
        for a in t.get("default_assignments", [])
        if a.get("trigger_type") == "integration_event"
        and (a.get("trigger_config") or {}).get("event") in MAIL_EVENTS
    ]


def test_every_template_mail_listener_drops_meeting_messages_before_a_run():
    listeners = _template_mail_assignments()
    assert listeners, "the email triage template is expected to seed a mail listener"
    unfiltered = [
        key for key, a in listeners if not (a["trigger_config"].get("filter") or {}).get("exclude_meeting_messages")
    ]
    assert not unfiltered, f"set filter.exclude_meeting_messages on: {unfiltered}"
    invite = {"from": "organizer@example.com", "subject": "Sync", "message_type": "meetingRequest"}
    plain = {"from": "colleague@example.com", "subject": "Question", "message_type": "message"}
    for key, a in listeners:
        event, config = a["trigger_config"]["event"], a["trigger_config"]["filter"]
        assert event_filter_reason(event, invite, config), f"{key} lets a meeting request through"
        assert event_filter_reason(event, plain, config) is None, f"{key} drops a plain mail"


@pytest.mark.parametrize("skill", BUILTIN_SKILLS, ids=lambda s: s["slug"])
def test_no_seeded_skill_section_is_too_long_to_load_alone(skill):
    body = _resolve_seed_content("skill_content", skill.get("content", skill["slug"])) or ""
    too_long = [(h, n) for h, n in _sections(body) if n > _SECTION_CEILING]
    assert not too_long, f"split under sub-headings: {too_long}"
