"""Fleet suggestions: stable ids, dismissals that hold, restore, bulk, no false findings (#10536).

Pinned here, without a database: a finding's id hashes its kind, member and
subject, not the model's sentence; a dismissal holds across an unrelated
configuration change and for 90 days, and goes when its member is reconfigured;
restore brings it back; dismiss with no ids takes every visible finding; a
finding the configuration contradicts is dropped before storage (the two
CEO Office false positives of 9 Nov 2026); dismissals with a reason reach the
generator as decided points; the member listing is no longer cut at 60
assignments; the fleet tool refuses dismiss and restore to a non-manager.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import fleet_doc_generation as gen
from app.services.fleet_doc_findings import (
    DISMISSAL_DAYS,
    clean_findings,
    decided_points,
    dismissal_record,
    drop_contradicted,
    finding_id,
    holding_dismissals,
    visible_findings,
)
from app.services.fleets import FleetActionRefusedError

UX = "UX Director"
ANALYST = "Analyst"
#: The two false positives of the CEO Office fleet (Sam, 10 Nov 2026), as the model wrote them.
FALSE_UX = {
    "kind": "missing_automation",
    "member": UX,
    "subject": "",
    "finding": "UX Director: pick up review work items is described, but the member lists no assignment.",
}
FALSE_ANALYST = {
    "kind": "missing_automation",
    "member": ANALYST,
    "subject": "Weekly company health report",
    "finding": "Weekly company health report (Monday 07:30 Paris) is missing from the Analyst's assignments.",
}
AUTOMATIONS = {
    "ux director": ["UX Director: pick up review work items"],
    "analyst": ["Weekly company health report"],
}


def test_the_id_holds_however_the_model_words_the_finding() -> None:
    one = {"kind": "other", "member": "Communications", "subject": "Newsjacking posts", "finding": "Posts as Sam."}
    two = {
        **one,
        "finding": "The member publishes newsjacking posts under Sam's name.",
        "subject": "newsjacking posts.",
    }
    assert finding_id(one) == finding_id(two)
    assert finding_id(one) != finding_id({**one, "member": "Developer"})
    # Without a subject the line is the key, as before.
    assert finding_id({"kind": "other", "finding": "A."}) != finding_id({"kind": "other", "finding": "B."})


def test_both_ceo_office_false_positives_are_dropped_before_storage() -> None:
    real = {"kind": "member_without_role", "member": UX, "finding": "The UX Director has no role label."}
    other_member = {"kind": "missing_automation", "member": "Legal", "finding": "Legal has no assignment."}
    kept = drop_contradicted(clean_findings([FALSE_UX, FALSE_ANALYST, real, other_member]), AUTOMATIONS)
    assert [f["finding"] for f in kept] == [real["finding"], other_member["finding"]]


def test_a_real_missing_automation_is_kept() -> None:
    real = {
        "kind": "missing_automation",
        "member": ANALYST,
        "subject": "Quarterly board pack",
        "finding": "The quarterly board pack is described but has no automation.",
    }
    assert len(drop_contradicted(clean_findings([real]), AUTOMATIONS)) == 1


def test_the_member_listing_carries_every_assignment() -> None:
    # The 60 cap made the members sorted last read as having no automation.
    assert gen.CONTEXT_MAX_ASSIGNMENTS >= 400
    member = uuid.uuid4()
    fleet = SimpleNamespace(
        name="F",
        description="",
        skills=None,
        orchestrator_agent_id=None,
        default_context_inclusion=None,
        default_collaboration_instruction=None,
        organization_scope=None,
        approval_gates=None,
    )
    row = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=member,
        role="member",
        role_label="",
        parent_member_id=None,
        is_exposed=False,
        context_inclusion=None,
        collaboration_instruction=None,
        organization_scope=None,
    )
    agent = SimpleNamespace(
        id=member, name=UX, slug="ux", description="", persona="", handover_agent_ids=[], handover_types={}
    )
    assignments = [
        SimpleNamespace(
            id=uuid.uuid4(),
            agent_id=member,
            name=f"Automation {i:03}",
            trigger_type="work_item",
            trigger_config={},
            enabled=True,
            run_as_all_users=False,
            run_as_user_id=None,
            human_in_the_loop=False,
        )
        for i in range(70)
    ]
    context = gen.draft_context(fleet, [row], {member: agent}, assignments)
    [listed] = context["members"]
    assert listed["assignment_count"] == 70 and len(listed["assignments"]) == 70
    assert gen.automations_by_member(context)["ux"][-1] == "Automation 069"


def _doc(*raw: dict) -> dict:
    return {"sections": {"purpose": "x"}, "version": 1, "findings": clean_findings(list(raw))}


def test_a_dismissal_survives_an_unrelated_change_and_goes_when_its_member_changes() -> None:
    doc = _doc(FALSE_ANALYST, {"kind": "other", "member": UX, "finding": "Another."})
    target = doc["findings"][0]
    doc["dismissed_findings"] = dismissal_record(doc, target, "analyst-1", by="u", reason="false positive")
    prints = {"analyst": "analyst-1", "ux director": "ux-1", "": "fleet-1"}
    assert [f["member"] for f in visible_findings(doc, prints)] == [UX]
    # Another member and the fleet card change: the dismissal holds.
    assert [f["member"] for f in visible_findings(doc, {**prints, "ux director": "ux-2", "": "fleet-2"})] == [UX]
    # The member it names is reconfigured: it comes back.
    assert len(visible_findings(doc, {**prints, "analyst": "analyst-2"})) == 2


def test_a_dismissal_lapses_after_ninety_days_and_a_former_record_does_not_hold() -> None:
    doc = _doc(FALSE_ANALYST)
    then = datetime.now(UTC) - timedelta(days=DISMISSAL_DAYS + 1)
    doc["dismissed_findings"] = dismissal_record(doc, doc["findings"][0], "p", by="u", now=then)
    assert len(visible_findings(doc, {"analyst": "p"})) == 1
    doc["dismissed_findings"] = [{"id": doc["findings"][0]["id"], "fingerprint": "f" * 16}]
    assert len(visible_findings(doc, {"analyst": "p"})) == 1


def test_decided_points_carry_the_reasoned_dismissals_only() -> None:
    doc = _doc(FALSE_ANALYST, {"kind": "other", "member": UX, "finding": "Unreasoned."})
    records = dismissal_record(doc, doc["findings"][0], "p", by="u", reason="standing decision")
    doc["dismissed_findings"] = dismissal_record({"dismissed_findings": records}, doc["findings"][1], "q", by="u")
    points = decided_points(doc, {"analyst": "p", "ux director": "q"})
    assert points == ["Analyst: Weekly company health report (missing_automation). Decided: standing decision"]


# ── fleet_docs, no database ─────────────────────────────────────────────────


@pytest.fixture
def prints(monkeypatch):
    from app.services import fleet_docs

    state = {"": "fleet-1", "analyst": "a-1", "ux director": "u-1"}

    async def _prints(_db, _fleet):
        return dict(state)

    monkeypatch.setattr(fleet_docs, "current_member_fingerprints", _prints)
    return state


def _fleet(doc: dict) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), name="CEO Office", is_builtin=False, documentation=doc)


async def test_dismiss_all_then_restore_one_then_restore_all(prints) -> None:
    from app.services.fleet_docs import dismiss_findings, documentation_view, restore_findings

    async def _no_fp(_db, _fleet):
        return "x"

    import app.services.fleet_docs as fleet_docs

    fleet_docs_current = fleet_docs.current_fingerprint
    fleet_docs.current_fingerprint = _no_fp  # type: ignore[assignment]
    try:
        fleet = _fleet(_doc(FALSE_ANALYST, FALSE_UX, {"kind": "other", "member": "", "finding": "Fleet-wide."}))
        done = await dismiss_findings(object(), fleet, None, by="u", by_name="Sam", reason="standing decision")
        assert len(done) == 3
        view = await documentation_view(object(), fleet, can_edit=True)
        assert view.findings == [] and len(view.dismissed_findings) == 3
        assert {d["by_name"] for d in view.dismissed_findings} == {"Sam"}
        assert all("member_print" not in d for d in view.dismissed_findings)
        assert restore_findings(fleet, [done[0]["id"]]) == 1
        assert len((await documentation_view(object(), fleet, can_edit=True)).findings) == 1
        assert restore_findings(fleet, None) == 2
        view = await documentation_view(object(), fleet, can_edit=True)
        assert len(view.findings) == 3 and view.dismissed_findings == []
        with pytest.raises(FleetActionRefusedError):
            restore_findings(fleet, ["nope"])
        with pytest.raises(FleetActionRefusedError):
            await dismiss_findings(object(), fleet, ["nope"], by="u")
        reader = await documentation_view(object(), fleet, can_edit=False)
        assert reader.findings == [] and reader.dismissed_findings == []
    finally:
        fleet_docs.current_fingerprint = fleet_docs_current  # type: ignore[assignment]


async def test_a_reasoned_dismissal_reaches_the_generator_and_a_false_finding_never_lands(monkeypatch, prints) -> None:
    from app.services import fleet_docs

    async def _ctx(_db, _fleet):
        return {
            "members": [
                {"name": ANALYST, "agent": "analyst", "assignments": [{"name": "Weekly company health report"}]},
                {"name": UX, "agent": "ux", "assignments": [{"name": "UX Director: pick up review work items"}]},
            ],
            "automations": [],
            "approvals": {},
        }

    monkeypatch.setattr(gen, "load_draft_context", _ctx)
    stored = _doc({"kind": "other", "member": "Communications", "subject": "Newsjacking", "finding": "Posts as Sam."})
    stored["dismissed_findings"] = dismissal_record(
        stored, stored["findings"][0], prints.get("communications"), by="u", reason="standing decision"
    )
    answer = {"sections": {"purpose": "Runs the office."}, "findings": [FALSE_UX, FALSE_ANALYST]}

    class _Model:
        def __init__(self) -> None:
            self.messages: list = []

        async def chat(self, messages, *, model, temperature, max_tokens):
            self.messages.append(messages)
            return SimpleNamespace(content=json.dumps(answer), stop_reason="end_turn")

    model = _Model()
    draft = await gen.draft_fleet_documentation(object(), uuid.uuid4(), _fleet(stored), section="purpose", llm=model)
    assert draft.findings == []
    user = model.messages[0][1].content
    assert '"decided"' in user and "Newsjacking (other). Decided: standing decision" in user
    assert fleet_docs is not None


# ── the fleet tool ──────────────────────────────────────────────────────────


async def test_the_tool_refuses_dismiss_and_restore_to_a_non_manager(monkeypatch) -> None:
    from contextlib import asynccontextmanager

    from app.services.builtin_tools import fleets as fleet_tools

    class _Db:
        async def commit(self) -> None: ...

        async def rollback(self) -> None: ...

    @asynccontextmanager
    async def _factory():
        yield _Db()

    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False, full_name="Someone")

    async def _load(_db, _uid):
        return user

    async def _not_manager(*_a, **_kw):
        return None, "You cannot manage this fleet."

    monkeypatch.setattr(fleet_tools.database, "async_session", _factory)
    monkeypatch.setattr(fleet_tools._builder, "_load_caller_user_for_builder", _load)
    monkeypatch.setattr(fleet_tools._builder, "_load_manageable_entity_for_builder", _not_manager)
    _defs, executor = fleet_tools.build_fleet_tools(str(user.id))
    for action in ("dismiss", "restore", "list"):
        answer = await executor.handler("list_fleet_doc_findings")(
            fleet_id=str(uuid.uuid4()), action=action, ids=["abc"], reason="x"
        )
        assert "cannot manage" in answer.lower() or "refused" in answer.lower(), answer
    assert holding_dismissals(None, {}) == []
