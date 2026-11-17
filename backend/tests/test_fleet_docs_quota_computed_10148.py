"""#10148: the quota of a fleet's documentation is computed only; nothing the AI writes is ever "left out".

Pinned on every built-in fleet, the Concierge fleet and a custom fleet, from
their shipped specs (members and assignments), with an adversarial model part
(an invalid row, an extra row naming no automation, a duplicate): zero
``part_item_left_out`` note, one quota row per home automation (grouped past
:data:`QUOTA_ROWS_MAX` with its total), header automation count = list length.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.builtin_agents import BUILTIN_ASSIGNMENTS
from app.agents.builtin_fleet_docs import builtin_fleet_structured
from app.agents.builtin_fleets import BUILTIN_FLEETS
from app.services import fleet_doc_generation as gen
from app.services.concierge.fleet_seed import FLEET_AGENTS, FLEET_ASSIGNMENTS, ORCHESTRATOR_SLUG
from app.services.fleet_docs import clean_structured


def _agent(slug: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(), name=slug, slug=slug, description="", persona="", handover_agent_ids=[], handover_types={}
    )


def _row(agent_id: uuid.UUID, role: str = "member") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        target_fleet_id=None,
        role=role,
        role_label="",
        parent_member_id=None,
        is_exposed=False,
        context_inclusion=None,
        collaboration_instruction=None,
        organization_scope=None,
    )


def _assignment(
    agent_id: uuid.UUID, name: str, kind: str, config: dict | None, enabled: bool = True
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name=name,
        trigger_type=kind,
        trigger_config=config or {},
        enabled=enabled,
        run_as_user_id=None,
        run_as_all_users=False,
        human_in_the_loop=None,
    )


def _fleet(name: str, orchestrator: uuid.UUID | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        description="",
        skills=[],
        default_context_inclusion="none",
        default_collaboration_instruction="",
        organization_scope={"mode": "primary"},
        orchestrator_agent_id=orchestrator,
    )


def _context(name: str, members: list[str], connected: list[str], specs: list[tuple[str, str, str, dict | None]]):
    agents = {slug: _agent(slug) for slug in [*members, *connected]}
    rows = [_row(agents[s].id) for s in members] + [_row(agents[s].id, "connected") for s in connected]
    assignments = [_assignment(agents[slug].id, n, k, c) for slug, n, k, c in specs if slug in agents]
    orchestrator = agents[members[0]].id if members else None
    context = gen.draft_context(_fleet(name, orchestrator), rows, {a.id: a for a in agents.values()}, assignments)
    home = {agents[s].id for s in members}
    return context, sum(1 for a in assignments if a.agent_id in home)


def _builtin(entry: dict[str, Any]):
    members = [m["agent"] for m in entry.get("members") or []]
    connected = [c["agent"] for c in entry.get("connected") or []]
    specs = [
        (a["agent_slug"], a["name"], a.get("trigger_type") or "manual", a.get("trigger_config"))
        for a in BUILTIN_ASSIGNMENTS
    ]
    return _context(entry["name"], members, connected, specs)


def _concierge():
    members = [ORCHESTRATOR_SLUG, *[s.slug for s in FLEET_AGENTS if s.slug != ORCHESTRATOR_SLUG]]
    specs = [(s.agent_slug, s.name, "manual", {}) for s in FLEET_ASSIGNMENTS]
    return _context("Concierge", members, [], specs)


def _custom():
    specs = [
        ("lead", "Daily brief", "schedule", {"cron": "0 7 * * *"}),
        ("lead", "New issue", "integration_event", {"provider": "github", "event": "issue"}),
        ("helper", "Pick up", "work_item", {"correlation_prefix": "dev:"}),
        ("helper", "Weekly review", "schedule", {"cron": "0 8 * * 1"}),
        ("friend", "Not ours", "schedule", {"cron": "0 9 * * *"}),
    ]
    return _context("Custom desk", ["lead", "helper"], ["friend"], specs)


_CASES = [
    pytest.param(lambda e=entry: _builtin(e), builtin_fleet_structured(entry), id=entry["slug"])
    for entry in BUILTIN_FLEETS
]
_CASES += [pytest.param(_concierge, None, id="concierge"), pytest.param(_custom, None, id="custom")]


def _adversarial(context: dict[str, Any], shipped: dict[str, Any] | None) -> dict[str, Any]:
    """The shipped parts (or a prose line) with every kind of quota row a model has been seen to write."""
    names = [q["trigger"] for q in context["quota"]] or ["none"]
    rows = [
        {"trigger": names[0], "runs_min": -3, "runs_max": "many"},
        {"trigger": names[-1], "basis": "Estimate: one a day", "runs_min": 20, "runs_max": 40},
        {"trigger": names[-1], "basis": "Estimate: duplicate", "runs_min": 1, "runs_max": 2},
        {"trigger": "An automation nobody configured", "basis": "Estimate", "runs_min": 5, "runs_max": 9},
        {"trigger": "", "runs_min": True, "runs_max": 10**9},
        "not a row",
    ]
    return {**(shipped or {"purpose_line": "One line."}), "quota": rows}


def _covered(parts: dict[str, Any], context: dict[str, Any]) -> int:
    """How many automations the quota rows cover, a grouped row counting every row it sums."""
    named = {q["trigger"] for q in context["quota"]}
    plain = sum(1 for q in parts.get("quota") or [] if q["trigger"] in named)
    grouped = len(context["quota"]) - plain if len(context["quota"]) > gen.QUOTA_ROWS_MAX else 0
    return plain + grouped


@pytest.mark.parametrize(("build", "shipped"), _CASES)
def test_a_regenerate_never_leaves_a_quota_or_trigger_row_out(build, shipped) -> None:
    context, automations = build()
    notes: list[dict[str, Any]] = []
    parts = gen.build_structured(_adversarial(context, shipped), context, notes)
    assert parts is not None
    left_out = [
        n for n in notes if n["code"] == "part_item_left_out" and n["params"].get("block") in {"quota", "triggers"}
    ]
    assert not left_out, left_out
    assert len(context["quota"]) == automations, "one computed row per home automation"
    assert len(parts.get("automations") or []) == automations, "the header count and the list share one source"
    if automations:
        assert _covered(parts, context) == automations
        assert len(parts["quota"]) == min(automations, gen.QUOTA_ROWS_MAX)
        assert not any(q["trigger"] == "An automation nobody configured" for q in parts["quota"])
    else:
        assert not parts.get("quota"), "no automation, no quota row: an AI row never stands alone"


@pytest.mark.parametrize(("build", "shipped"), _CASES)
def test_the_seed_merge_writes_the_computed_rows_only(build, shipped) -> None:
    context, automations = build()
    merged = gen.with_computed_facts(_adversarial(context, shipped), context)
    named = {q["trigger"] for q in context["quota"]}
    assert all(q["trigger"] in named or q["trigger"].endswith("more") for q in merged.get("quota") or [])
    assert len(merged.get("quota") or []) == min(automations, gen.QUOTA_ROWS_MAX)
    assert clean_structured({"quota": merged["quota"]}) if merged.get("quota") else True


def test_a_valid_estimate_of_an_event_driven_automation_is_still_read() -> None:
    context, _ = _custom()
    model = {"quota": [{"trigger": "New issue", "basis": "Estimate: one per new issue", "runs_min": 2, "runs_max": 20}]}
    parts = gen.build_structured(model, context, [])
    assert parts is not None
    row = next(q for q in parts["quota"] if q["trigger"] == "New issue")
    assert (row["runs_min"], row["runs_max"], row["basis"]) == (2, 20, "Estimate: one per new issue")
    assert not any(q["trigger"] == "Not ours" for q in parts["quota"]), "a connected agent's automation is not counted"


def test_a_bool_or_out_of_range_estimate_keeps_the_computed_row() -> None:
    context, _ = _custom()
    for low, high in ((True, 5), (0, 10**9)):
        parts = gen.build_structured(
            {"quota": [{"trigger": "New issue", "runs_min": low, "runs_max": high}]}, context, []
        )
        assert parts is not None
        row = next(q for q in parts["quota"] if q["trigger"] == "New issue")
        assert row["basis"] == "Event-driven, no fixed estimate"
