"""#10108: a quota row per automation, no dropped AI row, one live check per integration, header = list.

Pinned without a database: every trigger kind (schedule, event, platform event,
work item) gets a quota row with its basis; an event-driven row is open-ended and
the range says so; an invalid AI quota row of an automation is replaced by the
computed one, never noted as left out; a prerequisite naming several
integrations becomes one ``integration:<provider>`` check per provider; the
automations list covers every home automation (more than 60).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.services import fleet_doc_generation as gen
from app.services.fleet_datasheet import quota_range
from app.services.fleet_docs import clean_structured


def _assignment(name: str, kind: str, config: dict, *, enabled: bool = True, agent_id=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name=name,
        trigger_type=kind,
        trigger_config=config,
        enabled=enabled,
        run_as_user_id=None,
        run_as_all_users=False,
        human_in_the_loop=None,
    )


def _row(agent_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        target_fleet_id=None,
        role="member",
        role_label="",
        parent_member_id=None,
        is_exposed=False,
        context_inclusion=None,
        collaboration_instruction=None,
        organization_scope=None,
    )


_FLEET = SimpleNamespace(
    id=uuid.uuid4(),
    name="Custom desk",
    description="",
    skills=[],
    default_context_inclusion="none",
    default_collaboration_instruction="",
    organization_scope={"mode": "primary"},
    orchestrator_agent_id=None,
)


def _context(observed: dict | None = None) -> tuple[dict, list[SimpleNamespace]]:
    lead = uuid.uuid4()
    agent = SimpleNamespace(
        id=lead, name="Lead", slug="lead", description="", persona="", handover_agent_ids=[], handover_types={}
    )
    assignments = [
        _assignment("Daily brief", "schedule", {"cron": "0 7 * * *"}, agent_id=lead),
        _assignment("New issue", "integration_event", {"provider": "github", "event": "issue"}, agent_id=lead),
        _assignment("Pick up", "work_item", {"correlation_prefix": "dev:"}, agent_id=lead),
    ]
    seen = {assignments[1].id: observed} if observed else None
    context = gen.draft_context(_FLEET, [_row(lead)], {lead: agent}, assignments, observed_runs=seen)
    return context, assignments


def test_every_automation_gets_a_quota_row_with_its_basis() -> None:
    context, _ = _context()
    parts = gen.build_structured(None, context, [])
    assert parts is not None
    rows = {q["trigger"]: q for q in parts["quota"]}
    assert set(rows) == {"Daily brief", "New issue", "Pick up"}, "one row per automation, never schedules only"
    assert rows["Daily brief"]["runs_max"] == 30 and not rows["Daily brief"].get("open_ended")
    assert rows["New issue"]["open_ended"] is True
    assert rows["New issue"]["basis"] == "Event-driven, no fixed estimate"
    assert len(parts["automations"]) == len(parts["quota"]), "the header count and the list share one source"
    # UX r1 B1, B3 on #10112: the page words each row from its trigger kind, in the reader's language.
    assert rows["Daily brief"]["kinds"] == ["schedule"]
    assert rows["New issue"]["kinds"] == ["event"] and not rows["New issue"].get("observed")


def test_a_hand_started_row_is_typed_manual_and_a_group_keeps_every_kind() -> None:
    manual = _assignment("Ask", "manual", {}, agent_id=uuid.uuid4())
    row = gen.automation_quota(manual)
    assert row["kinds"] == ["manual"] and row["basis"] == "Started by hand, no fixed estimate"
    grouped, count = gen.group_quota(
        [
            {"trigger": "a", "runs_min": 9, "runs_max": 9, "kinds": ["schedule"]},
            {"trigger": "b", "runs_min": 0, "runs_max": 0, "open_ended": True, "kinds": ["manual"]},
            {"trigger": "c", "runs_min": 0, "runs_max": 0, "open_ended": True, "kinds": ["event"]},
        ],
        2,
    )
    assert count == 2 and grouped[-1]["kinds"] == ["event", "manual"]


def test_an_event_driven_row_carries_the_runs_observed_over_30_days() -> None:
    context, _ = _context(observed=1238)
    row = next(q for q in context["quota"] if q["trigger"] == "New issue")
    assert row["runs_min"] == row["runs_max"] == 1238
    assert row["basis"].startswith("Observed: 1,238 runs in the last 30 days")
    assert row["observed"] is True


def test_an_invalid_ai_quota_row_is_replaced_never_left_out() -> None:
    context, _ = _context()
    notes: list = []
    model = {
        "quota": [
            {"trigger": "Pick up", "runs_min": -3, "runs_max": "many"},
            {"trigger": "New issue", "basis": "Estimate: one per new issue", "runs_min": 2, "runs_max": 20},
        ]
    }
    parts = gen.build_structured(model, context, notes)
    assert parts is not None
    assert not any(n["code"] == "part_item_left_out" for n in notes)
    rows = {q["trigger"]: q for q in parts["quota"]}
    assert rows["Pick up"]["basis"] == "Event-driven, no fixed estimate"
    assert (rows["New issue"]["runs_min"], rows["New issue"]["runs_max"]) == (2, 20), "the AI's valid estimate is kept"


def test_a_prerequisite_naming_several_integrations_is_one_live_check_each() -> None:
    lines = gen.split_prerequisite({"check": "manual", "label": "Connect Notion and GitHub"})
    assert [line["check"] for line in lines] == ["integration:notion", "integration:github"]
    assert all(line["fix_route"] == "/integrations" for line in lines)
    parts = gen.build_structured(
        {"prerequisites": [{"check": "manual", "label": "Connect Notion, GitHub and HubSpot"}]}, {}, []
    )
    assert parts is not None
    assert [p["check"] for p in parts["prerequisites"]] == [
        "integration:notion",
        "integration:github",
        "integration:hubspot",
    ]
    single = gen.split_prerequisite({"check": "manual", "label": "OpenCTI connected"})
    assert [line["check"] for line in single] == ["integration:opencti"]
    assert gen.split_prerequisite({"check": "manual", "label": "Agree on the policy"})[0]["check"] == "manual"


def test_more_than_60_automations_are_all_listed_and_the_quota_groups_with_its_total() -> None:
    lead = uuid.uuid4()
    agent = SimpleNamespace(
        id=lead, name="Lead", slug="lead", description="", persona="", handover_agent_ids=[], handover_types={}
    )
    assignments = [_assignment(f"Watch {i:02d}", "platform_event", {"event": "run.completed"}, agent_id=lead) for i in range(61)]
    context = gen.draft_context(_FLEET, [_row(lead)], {lead: agent}, assignments)
    notes: list = []
    parts = gen.build_structured(None, context, notes)
    assert parts is not None
    assert len(parts["automations"]) == 61
    assert len(parts["quota"]) == gen.QUOTA_ROWS_MAX
    assert parts["quota"][-1]["trigger"].endswith("and 41 more") and parts["quota"][-1]["open_ended"] is True
    assert not any(n["code"] == "quota_grouped" for n in notes), "grouping is a display choice (#10246)"


def test_an_older_version_without_open_ended_still_validates_and_reads() -> None:
    legacy = {"quota": [{"trigger": "Daily", "runs_min": 30, "runs_max": 30}]}
    assert clean_structured(legacy) == legacy
    assert quota_range(legacy) == (30, 30)
