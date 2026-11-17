"""A regenerated corporate fleet draws the same visual blocks as a built-in one (#9979).

Pinned here, without a database: the modules open to every integration provider
of the catalog (Outlook, Teams, GitHub, Notion...) and nothing is remapped; a
model answer with more trigger cards than the page holds is cut with a note,
never the whole block; the computed facts (automations, integrations, schedule
quota) survive any wrong field; triggers, steps and gates are derived from the
configuration when the model gives none, and said; a flat answer asks for the
parts in a second call; the version notes name every block the new version
lost, and only a reader who manages the fleet sees them.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.services import fleet_doc_generation as gen
from app.services.fleet_datasheet import module_label
from app.services.fleet_docs import (
    BASE_MODULES,
    DOC_SECTIONS,
    clean_structured,
    clean_structured_fields,
    doc_modules,
    documentation_history,
    documentation_view,
    field_limit,
)
from app.services.fleets import FleetActionRefusedError


def _assignment(name: str, kind: str, config: dict, *, enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        agent_id=None,
        name=name,
        trigger_type=kind,
        trigger_config=config,
        enabled=enabled,
        run_as_user_id=None,
        run_as_all_users=False,
        human_in_the_loop=None,
    )


def _corporate_assignments() -> list[SimpleNamespace]:
    """About 60 automations: Outlook mail, Teams messages, GitHub events, platform events and schedules."""
    out = [_assignment("New email", "integration_event", {"provider": "microsoft", "event": "new_email"})]
    out.append(_assignment("Teams message", "integration_event", {"provider": "microsoft", "event": "teams_message"}))
    out += [
        _assignment(f"GitHub check {i}", "integration_event", {"provider": "github", "event": "check_run"})
        for i in range(12)
    ]
    out += [_assignment(f"Work item pick-up {i}", "work_item", {"correlation_prefix": f"dev{i}:"}) for i in range(10)]
    out += [_assignment(f"Run watch {i}", "platform_event", {"event": "run.completed"}) for i in range(6)]
    out += [
        _assignment(f"Daily brief {i}", "schedule", {"cron": f"{i % 60} 7 * * *", "timezone": "Europe/Paris"})
        for i in range(30)
    ]
    return out


def _context() -> dict:
    assignments = _corporate_assignments()
    facts = [gen.automation_facts(a, "Assistant") for a in assignments]
    return {
        "members": [
            {
                "agent": "assistant",
                "name": "Assistant",
                "role_label": "Routes the work",
                "orchestrator": True,
                "parent": None,
            },
            {
                "agent": "developer",
                "name": "Developer",
                "role_label": "Codes",
                "orchestrator": False,
                "parent": "assistant",
            },
        ],
        "automations": facts,
        "trigger_cards": gen.trigger_cards(assignments, facts, field_limit("triggers") or 20),
        "schedule_quota": [q for a in assignments if (q := gen.schedule_quota(a))],
        "tools": [
            {
                "member": "Assistant",
                "integrations": ["microsoft", "notion", "github"],
                "mcp_servers": [],
                "tool_groups": [],
                "skills": [],
            }
        ],
        "approvals": {"Developer": ["each tool call waits for approval"]},
    }


def test_every_integration_provider_is_a_module_and_nothing_is_remapped() -> None:
    modules = doc_modules()
    assert set(BASE_MODULES) <= modules
    assert {"microsoft", "github", "notion", "slack", "hubspot", "jira", "whatsapp"} <= modules
    parts = clean_structured({"triggers": [{"module": "GitHub", "name": "Check run"}]})
    assert parts == {"triggers": [{"module": "github", "name": "Check run"}]}
    with pytest.raises(FleetActionRefusedError):
        clean_structured({"objects": [{"module": "mars", "objects": ["rock"]}]})


def test_the_datasheet_prints_a_provider_module_by_its_catalog_name() -> None:
    assert module_label("opencti") == "OpenCTI"
    assert module_label("webhook") == "Webhook"
    assert module_label("github") == "GitHub"
    assert module_label("made_up") == "Made up", "an unknown key reads humanized, never 'Platform'"


def test_a_corporate_fleet_gets_one_trigger_card_per_module_past_the_page_limit() -> None:
    cards = _context()["trigger_cards"]
    assert len(cards) <= field_limit("triggers")
    modules = [c["module"] for c in cards]
    assert set(modules) == {"microsoft", "github", "work_item", "platform", "schedule"}
    schedule = next(c for c in cards if c["module"] == "schedule")
    assert schedule["name"] == "Daily brief 0 and 29 more"


def test_a_small_fleet_gets_one_card_per_automation_in_words_switched_off_ones_with_their_state() -> None:
    assignments = [
        _assignment("Weekly review", "schedule", {"cron": "0 8 * * 1", "timezone": "Europe/Paris"}),
        _assignment("Off", "schedule", {"cron": "0 8 * * 1"}, enabled=False),
        _assignment("Unknown", "integration_event", {"provider": "nowhere", "event": "x"}),
    ]
    facts = [gen.automation_facts(a, "Lead") for a in assignments]
    cards = gen.trigger_cards(assignments, facts, 20)
    assert cards[0] == {"module": "schedule", "name": "Weekly review", "filter": "Every Monday at 08:00 (Europe/Paris)"}
    assert cards[1]["name"] == "Off" and cards[1]["enabled"] is False, "a switched-off automation keeps its card (UX r1 B1)"
    assert len(cards) == 2, "an unknown provider gets no card"


def test_too_many_model_triggers_cost_the_extra_items_never_the_block() -> None:
    """The CEO Office case: the model listed one card per automation and the last pass dropped everything."""
    context = _context()
    model_part = {
        "purpose_line": "Runs the CEO's office.",
        "triggers": [{"module": "github", "name": f"Check {i}"} for i in range(45)],
        "steps": [{"title": "Route"}],
        "objects": [{"module": "notion", "objects": ["page"]}, {"module": "microsoft", "objects": ["email"]}],
    }
    notes: list[str] = []
    parts = gen.build_structured(model_part, context, notes)
    assert parts is not None
    assert parts["purpose_line"] == "Runs the CEO's office."
    assert len(parts["triggers"]) == field_limit("triggers")
    assert [o["module"] for o in parts["objects"]] == ["notion", "microsoft"]
    assert len(parts["automations"]) == 60, "the computed automations always survive"
    assert parts["quota"], "the schedule quota is computed from the crons"
    assert parts["integrations"][0]["integrations"] == ["microsoft", "notion", "github"]
    assert {"code": "part_cut", "params": {"block": "triggers", "kept": 20, "total": 45}} in notes
    assert not any(n["code"] == "part_left_out" for n in notes)


def test_a_wrong_field_costs_that_field_only() -> None:
    notes: list[str] = []
    parts = clean_structured_fields(
        {"purpose_line": "x" * 400, "automations": [{"name": "Daily", "trigger": "Schedule"}]}, notes
    )
    assert parts == {"automations": [{"name": "Daily", "trigger": "Schedule"}]}
    assert {"code": "part_left_out", "params": {"block": "purpose_line"}} in notes


def test_without_model_parts_the_triggers_steps_and_gates_come_from_the_configuration() -> None:
    notes: list[str] = []
    parts = gen.build_structured(None, _context(), notes)
    assert parts is not None
    assert parts["triggers"] and parts["automations"] and parts["quota"] and parts["integrations"]
    assert [s["title"] for s in parts["steps"]] == ["Assistant routes the work", "Developer"]
    assert parts["steps"][1]["gate"] is True
    assert parts["gates"] == [{"action": "Developer: each tool call waits for approval", "approver": "a person"}]
    for key in ("triggers", "steps", "gates", "prerequisites"):
        assert {"code": "part_derived", "params": {"block": key}} in notes


def test_a_duplicated_built_in_with_switched_off_automations_keeps_triggers_and_prerequisites() -> None:
    """UX r1 B1 on #9988: a fork's automations are created off; regenerated, it keeps every block it shipped with."""
    assignments = [
        _assignment("Watch alerts", "integration_event", {"provider": "github", "event": "issue"}, enabled=False),
        _assignment("Weekly digest", "schedule", {"cron": "0 8 * * 1"}, enabled=False),
    ]
    facts = [gen.automation_facts(a, "Lead") for a in assignments]
    context = {
        "members": [{"agent": "lead", "name": "Lead", "orchestrator": True, "parent": None}],
        "automations": facts,
        "trigger_cards": gen.trigger_cards(assignments, facts, 20),
        "schedule_quota": [q for a in assignments if (q := gen.schedule_quota(a))],
        "tools": [{"member": "Lead", "integrations": ["github"], "mcp_servers": [], "tool_groups": [], "skills": []}],
        "approvals": {},
    }
    shipped = {"objects": [{"module": "github", "objects": ["issue"]}], "outcome_line": "Shipped outcome."}
    notes: list = []
    parts = gen.build_structured(None, context, notes, previous=shipped)
    assert parts is not None
    assert [t["name"] for t in parts["triggers"]] == ["Watch alerts", "Weekly digest"]
    assert all(t["enabled"] is False for t in parts["triggers"])
    assert parts["prerequisites"] == [
        {"check": "integration:github", "label": "GitHub connected", "fix_route": "/integrations"},
        {"check": "automations:enabled", "label": "Switch on the automations you want (2 switched off)"},
    ]
    assert parts["objects"] == shipped["objects"] and parts["outcome_line"] == "Shipped outcome."
    assert {"code": "part_kept", "params": {"block": "objects"}} in notes
    assert gen.lost_blocks(shipped, parts) == []


def test_prerequisites_derive_nothing_when_the_configuration_states_nothing() -> None:
    assert gen.derived_prerequisites({"tools": [{"integrations": ["nowhere"]}], "automations": [{"enabled": True}]}) == []


def test_an_enabled_automation_keeps_its_state_through_the_stored_dump() -> None:
    """UX r2 B1 on #9988: the dump drops defaults, so an automation switched on is stored without ``enabled``.

    The page reads only an explicit ``False`` as off (``automationOn``); this pins the stored shape it reads.
    """
    stored = clean_structured(
        {
            "automations": [
                {"name": "On one", "trigger": "Schedule", "enabled": True},
                {"name": "Off one", "trigger": "Schedule", "enabled": False},
            ]
        }
    )
    assert stored is not None
    on, off = stored["automations"]
    assert on.get("enabled", True) is True and "enabled" not in on
    assert off["enabled"] is False


def test_prerequisites_past_the_limit_group_every_provider_by_name_with_a_note() -> None:
    """UX r2 B2 on #9988: about 30 providers never stop silently at 20; each is named, the cut is noted."""
    from app.api.v1.integration_catalog import PROVIDER_CATALOG

    known = [k for k in PROVIDER_CATALOG if gen.prerequisite_check_known(f"integration:{k}")]
    limit = gen.field_limit("prerequisites")
    assert limit is not None and len(known) > 30
    thirty = known[:30]
    notes: list = []
    out = gen.derived_prerequisites({"tools": [{"integrations": thirty}], "automations": [{"enabled": True}]}, notes)
    assert [p["check"] for p in out] == [f"integration:{k}" for k in thirty]
    assert notes == []
    many = known[: limit + 5]
    out = gen.derived_prerequisites({"tools": [{"integrations": many}], "automations": [{"enabled": False}]}, notes)
    assert len(out) == limit
    assert out[-1]["label"].startswith("Switch on the automations")
    grouped = out[-2]
    tail = many[limit - 2 :]
    assert grouped["check"] == "manual" and grouped["label"].startswith(f"Connect {len(tail)} more integrations: ")
    assert grouped["fix_route"] == "/integrations"
    assert notes == [], "grouping is a display choice, never a note (#10246)"
    assert clean_structured({"prerequisites": out}) is not None


def test_the_quota_past_its_row_limit_is_grouped_and_the_total_kept() -> None:
    """UX r1 B5 on #9988: 30 schedules of 22 runs a month read 660, never 440."""
    context = _context()
    notes: list = []
    parts = gen.with_computed_facts({}, context, notes)
    rows = parts["quota"]
    assert len(rows) == gen.QUOTA_ROWS_MAX
    expected = sum(q["runs_max"] for q in context["schedule_quota"])
    assert sum(q["runs_max"] for q in rows) == expected
    assert rows[-1]["trigger"] == "Daily brief 19 and 10 more"
    assert not any(n["code"] == "quota_grouped" for n in notes), "grouping is a display choice (#10246)"


# ── #10094: the fleet's own automations, every trigger card kept, live prerequisites ──


def _row(agent_id: uuid.UUID, role: str) -> SimpleNamespace:
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


def _agent(agent_id: uuid.UUID, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=agent_id,
        name=name,
        slug=name.lower(),
        description="",
        persona="",
        handover_agent_ids=[],
        handover_types={},
    )


def test_a_connected_agent_s_automations_are_never_counted_in_the_fleet_s_blocks() -> None:
    """#10094: a connected orchestrator's 30-minute schedule (1,440 runs) never reaches the fleet's quota."""
    lead, analyst, outsider = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    rows = [_row(lead, "member"), _row(analyst, "member"), _row(outsider, "connected")]
    agents = {lead: _agent(lead, "Lead"), analyst: _agent(analyst, "Analyst"), outsider: _agent(outsider, "Other")}

    def scheduled(agent_id: uuid.UUID, name: str, cron: str) -> SimpleNamespace:
        a = _assignment(name, "schedule", {"cron": cron})
        a.agent_id = agent_id
        return a

    assignments = [
        scheduled(lead, "Daily brief", "0 7 * * *"),
        scheduled(analyst, "Weekly review", "0 8 * * 1"),
        scheduled(outsider, "Half-hourly sweep", "*/30 * * * *"),
    ]
    context = gen.draft_context(SimpleNamespace(**_FLEET, orchestrator_agent_id=lead), rows, agents, assignments)
    assert [a["name"] for a in context["automations"]] == ["Daily brief", "Weekly review"]
    assert [c["name"] for c in context["trigger_cards"]] == ["Daily brief", "Weekly review"]
    own = [gen.schedule_quota(a) for a in assignments[:2]]
    assert context["schedule_quota"] == own
    parts = gen.build_structured(None, context, [])
    assert parts is not None
    assert sum(q["runs_max"] for q in parts["quota"]) == sum(q["runs_max"] for q in own if q), (
        "the quota headline is the fleet's own estimate"
    )
    assert [c["name"] for c in context["connected"]] == ["Other"], "the connected agent stays in the flow"


def test_an_invalid_trigger_entry_of_the_ai_is_repaired_from_the_automations_never_left_out() -> None:
    """#10094: one card per automation of the home members, no part_item_left_out on triggers."""
    assignments = [
        _assignment("Watch issues", "integration_event", {"provider": "github", "event": "issue"}),
        _assignment("Weekly digest", "schedule", {"cron": "0 8 * * 1"}),
        _assignment("Pick up", "work_item", {"correlation_prefix": "dev:"}),
    ]
    facts = [gen.automation_facts(a, "Lead") for a in assignments]
    context = {
        "automations": facts,
        "trigger_cards": gen.trigger_cards(assignments, facts, 20),
        "schedule_quota": [q for a in assignments if (q := gen.schedule_quota(a))],
    }
    model_part = {
        "triggers": [
            {"module": "github", "name": "Watch issues", "filter": "New issue"},
            {"module": "mars", "name": "Weekly digest"},
            {"module": "work_item", "name": ""},
        ]
    }
    notes: list = []
    parts = gen.build_structured(model_part, context, notes)
    assert parts is not None
    assert [t["name"] for t in parts["triggers"]] == ["Watch issues", "Weekly digest", "Pick up"]
    assert parts["triggers"][0]["filter"] == "New issue", "the AI's valid card is kept as it wrote it"
    assert not any(n["code"] == "part_item_left_out" for n in notes)


def test_a_prerequisite_naming_an_integration_is_a_live_check() -> None:
    """#10094: "OpenCTI connected" written manual by the AI resolves to met true or false, never null."""
    typed = gen.typed_prerequisite({"check": "manual", "label": "OpenCTI connected with an analyst account"})
    assert typed["check"] == "integration:opencti" and typed["fix_route"] == "/integrations"
    assert gen.typed_prerequisite({"check": "manual", "label": "Agree on the triage policy"})["check"] == "manual"
    known = {"check": "profile:sector", "label": "Sector set"}
    assert gen.typed_prerequisite(known) == known
    parts = gen.build_structured(
        {"prerequisites": [{"check": "manual", "label": "GitHub integration connected"}]}, {}, []
    )
    assert parts is not None and parts["prerequisites"][0]["check"] == "integration:github"


def test_the_quota_grouping_keeps_the_largest_rows() -> None:
    """#10094: the largest rows stay on their own line; the smallest are grouped, the total kept."""
    rows = [{"trigger": f"A{i}", "runs_min": 1, "runs_max": 1} for i in range(25)]
    rows.append({"trigger": "Zulu sweep", "runs_min": 1440, "runs_max": 1440})
    out, grouped = gen.group_quota(rows, 20)
    assert grouped == 7 and out[0]["trigger"] == "Zulu sweep"
    assert sum(q["runs_max"] for q in out) == 1465


_FLEET = {
    "id": uuid.uuid4(),
    "name": "Alert Desk",
    "description": "",
    "skills": [],
    "default_context_inclusion": "none",
    "default_collaboration_instruction": "",
    "organization_scope": {"mode": "primary"},
}


def test_a_note_is_a_code_with_params_and_a_legacy_sentence_still_reads() -> None:
    """UX r1 B6 on #9988: no English sentence with internal keys reaches the page."""
    from app.services.fleet_docs import doc_note, note_text, stored_note

    note = doc_note("part_derived", block="triggers")
    assert note == {"code": "part_derived", "params": {"block": "triggers"}}
    assert note_text(note) == "trigger cards come from the fleet's configuration: the AI wrote none"
    assert stored_note("an old sentence") == {"code": "text", "params": {"text": "an old sentence"}}
    with pytest.raises(ValueError):
        doc_note("made_up")


class _Model:
    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.messages: list = []

    async def chat(self, messages, *, model, temperature, max_tokens):
        self.messages.append(messages)
        return SimpleNamespace(content=self.answers.pop(0), stop_reason="end_turn")


@pytest.fixture
def corporate(monkeypatch):
    async def _ctx(_db, _fleet):
        return _context()

    monkeypatch.setattr(gen, "load_draft_context", _ctx)


def _fleet(**fields) -> SimpleNamespace:
    base = {"id": uuid.uuid4(), "name": "CEO Office", "is_builtin": False, "documentation": None}
    return SimpleNamespace(**{**base, **fields})


async def test_a_flat_answer_asks_for_the_parts_in_a_second_call(corporate) -> None:
    sections = {k: f"Text of {k}." for k in DOC_SECTIONS}
    second = json.dumps({"structured": {"purpose_line": "Runs the office.", "outcome_line": "Sam decides faster."}})
    model = _Model(json.dumps(sections), second)
    draft = await gen.draft_fleet_documentation(object(), uuid.uuid4(), _fleet(), llm=model)
    assert len(model.messages) == 2
    assert 'Answer with ONE JSON object {"structured"' in model.messages[1][-1].content
    assert draft.structured["purpose_line"] == "Runs the office."
    assert draft.structured["outcome_line"] == "Sam decides faster."
    assert draft.structured["triggers"], "the trigger cards come from the configuration"


async def test_a_second_call_without_parts_still_draws_the_computed_blocks(corporate) -> None:
    sections = {k: f"Text of {k}." for k in DOC_SECTIONS}
    model = _Model(json.dumps(sections), json.dumps(sections))
    draft = await gen.draft_fleet_documentation(object(), uuid.uuid4(), _fleet(), llm=model)
    assert draft.structured is not None
    assert {"triggers", "steps", "gates", "quota", "automations", "integrations"} <= set(draft.structured)
    assert {"code": "second_call_empty", "params": {}} in draft.notes


@pytest.fixture
def fingerprint(monkeypatch):
    from app.services import fleet_docs

    async def fake(_db, _fleet):
        return "f" * 16

    monkeypatch.setattr(fleet_docs, "current_fingerprint", fake)


async def test_a_regenerate_keeps_the_blocks_it_lost_on_the_history_entry_never_on_about(
    monkeypatch, fingerprint
) -> None:
    async def _ctx(_db, _fleet):
        return {"members": [], "automations": [], "approvals": {}}

    monkeypatch.setattr(gen, "load_draft_context", _ctx)
    previous = {
        "purpose_line": "Old",
        "triggers": [{"module": "github", "name": "Check"}],
        "automations": [{"name": "Daily", "trigger": "Schedule"}],
    }
    fleet = _fleet(documentation={"sections": {"purpose": "Old."}, "version": 3, "structured": previous})
    sections = {k: f"Text of {k}." for k in DOC_SECTIONS}
    answer = json.dumps({"sections": sections, "structured": {"purpose_line": "New"}})
    await gen.regenerate_fleet_documentation(object(), uuid.uuid4(), fleet, base_version=3, llm=_Model(answer))
    notes = fleet.documentation["notes"]
    # A computed block follows the configuration: lost and said. A drawn block nothing replaces is kept (UX r1 B1).
    assert {"code": "block_lost", "params": {"block": "automations"}} in notes
    assert fleet.documentation["structured"]["triggers"] == [{"module": "github", "name": "Check"}]
    assert {"code": "part_kept", "params": {"block": "triggers"}} in notes
    assert fleet.documentation["structured"]["purpose_line"] == "New"
    # #10246: the About page carries no generator note; the version history does, for managers only.
    manager = await documentation_view(object(), fleet, can_edit=True)
    assert not hasattr(manager, "notes")
    current = documentation_history(fleet, with_notes=True)[0]
    assert current.notes == notes and current.needs_regenerate is True
    assert documentation_history(fleet)[0].notes == []
