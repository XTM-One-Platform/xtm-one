"""A lapsed dismissal is a live suggestion, counted once everywhere (#12099).

Pinned here, without a database:

1. A lapsed dismissal whose finding a regeneration dropped from the stored list is
   counted: the fleet tool's header, the About page chip (``documentation_view``)
   and ``open_suggestion_count`` (which ``get_fleet`` and ``get_fleet_health`` read)
   give the same number on the same fleet; never "0 suggestion(s)" over a lapsed row.
2. A dismissal lapses only when the part of the member its finding is about
   changes: a role label edit lapses none; a description edit lapses a
   ``description_mismatch`` dismissal and not a gate one; a tools change lapses a
   gate dismissal and not a description one; 90 days lapse any.
3. The documentation draft reads a connected agent's effective collaboration
   instruction, and the prompt says the fleet's facts win over the current text.
4. A lapsed suggestion is dismissed by the id the list shows.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import fleet_doc_generation as gen
from app.services import fleet_docs
from app.services import fleet_practice_suggestions as practice
from app.services.fleet_doc_findings import (
    DISMISSAL_DAYS,
    clean_findings,
    dismissal_print,
    dismissal_record,
    lapse_reason,
)

DESC = {
    "kind": "description_mismatch",
    "member": "Analyst",
    "subject": "description",
    "finding": "Analyst's description says it sends email; it has no mail tool.",
}
GATE = {
    "kind": "no_gate_on_sensitive_action",
    "member": "Analyst",
    "subject": "create_github_pr",
    "finding": "Analyst opens pull requests with no approval gate.",
}


def _agent(**over) -> SimpleNamespace:
    base = {
        "id": uuid.UUID(int=1),
        "name": "Analyst",
        "slug": "analyst",
        "description": "Reads alerts.",
        "persona": "You triage.",
        "builtin_tool_ids": ["web"],
        "integration_bindings": [],
        "mcp_server_ids": [],
        "skill_ids": [],
        "handover_agent_ids": [],
        "handover_types": {},
        "human_in_the_loop": False,
        "approve_tool_calls": None,
    }
    return SimpleNamespace(**{**base, **over})


def _row(**over) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "agent_id": uuid.UUID(int=1),
        "role": "member",
        "role_label": "Analyst",
        "parent_member_id": None,
        "is_exposed": False,
        "context_inclusion": None,
        "collaboration_instruction": None,
        "organization_scope": None,
    }
    return SimpleNamespace(**{**base, **over})


def _fleet_row() -> SimpleNamespace:
    return SimpleNamespace(
        name="Office",
        description="",
        skills=[],
        default_context_inclusion="none",
        default_collaboration_instruction="",
        orchestrator_agent_id=None,
    )


def _prints(agent=None, row=None) -> dict[str, str]:
    agent = agent or _agent()
    return fleet_docs.member_fingerprints(_fleet_row(), [row or _row()], {agent.id: agent}, [])


def _dismissed(finding: dict, prints: dict[str, str], **kw) -> dict:
    [clean] = clean_findings([finding])
    facet, member_print = dismissal_print(clean, prints)
    [record] = dismissal_record({}, clean, member_print, by="u", facet=facet, **kw)
    return record


# ── 2. the lapse rule, per facet ───────────────────────────────────────────


def test_a_role_label_edit_lapses_no_dismissal() -> None:
    before = _prints()
    after = _prints(row=_row(role_label="Senior analyst"))
    for finding in (DESC, GATE):
        assert lapse_reason(_dismissed(finding, before), after) is None, finding["kind"]


def test_a_description_edit_lapses_the_description_dismissal_only() -> None:
    before = _prints()
    after = _prints(agent=_agent(description="Reads alerts and sends email."))
    assert lapse_reason(_dismissed(DESC, before), after) == "Analyst was reconfigured since (description)"
    assert lapse_reason(_dismissed(GATE, before), after) is None


def test_a_tools_change_lapses_the_gate_dismissal_only() -> None:
    before = _prints()
    after = _prints(agent=_agent(builtin_tool_ids=["web", "github"]))
    assert lapse_reason(_dismissed(GATE, before), after) == "Analyst was reconfigured since (tools or automations)"
    assert lapse_reason(_dismissed(DESC, before), after) is None


def test_ninety_days_lapse_any_dismissal() -> None:
    prints = _prints()
    old = datetime.now(UTC) - timedelta(days=DISMISSAL_DAYS + 1)
    assert lapse_reason(_dismissed(DESC, prints, now=old), prints) == f"older than {DISMISSAL_DAYS} days"


def test_a_record_of_the_former_shape_still_compares_the_whole_print() -> None:
    prints = _prints()
    [clean] = clean_findings([GATE])
    [record] = dismissal_record({}, clean, prints["analyst"], by="u")
    assert "print_facet" not in record and lapse_reason(record, prints) is None
    assert lapse_reason(record, _prints(row=_row(role_label="Other"))) == "Analyst was reconfigured since"


# ── 1 and 4. one count everywhere, dismissable by the id shown ──────────────


@pytest.fixture
def lapsed_fleet(monkeypatch):
    """A fleet whose only point is a lapsed dismissal whose finding is no longer stored."""
    state = {"prints": {"": "fleet-1", "analyst": "a-2"}}

    async def _no_practice(_db, _fleet):
        return []

    async def _member_prints(_db, _fleet):
        return dict(state["prints"])

    async def _fp(_db, _fleet):
        return "f" * 16

    monkeypatch.setattr(practice, "practice_suggestions", _no_practice)
    monkeypatch.setattr(fleet_docs, "current_member_fingerprints", _member_prints)
    monkeypatch.setattr(fleet_docs, "current_fingerprint", _fp)
    [gate] = clean_findings([GATE])
    # Dismissed against print a-1, the finding then dropped by a regeneration: the dismissal lapsed.
    records = dismissal_record({}, gate, "a-1", by="u", reason="gated upstream")
    fleet = SimpleNamespace(
        id=uuid.uuid4(),
        name="Office",
        slug="office",
        is_builtin=False,
        documentation={
            "sections": {"purpose": "Runs the office."},
            "version": 3,
            "findings": [],
            "dismissed_findings": records,
        },
    )
    return SimpleNamespace(fleet=fleet, gate=gate, state=state)


def _tool(monkeypatch, fleet):
    from app.services import audit
    from app.services.builtin_tools import fleets as fleet_tools

    class _Db:
        async def commit(self) -> None: ...

        async def rollback(self) -> None: ...

    @asynccontextmanager
    async def _factory():
        yield _Db()

    user = SimpleNamespace(id=uuid.uuid4(), is_admin=True, full_name="Owner")

    async def _load(_db, _uid):
        return user

    async def _manager(*_a, **_kw):
        return fleet, None

    async def _log(_db, **_kw):
        return None

    monkeypatch.setattr(fleet_tools.database, "async_session", _factory)
    monkeypatch.setattr(fleet_tools._builder, "_load_caller_user_for_builder", _load)
    monkeypatch.setattr(fleet_tools._builder, "_load_manageable_entity_for_builder", _manager)
    monkeypatch.setattr(audit, "log_audit", _log)
    _defs, executor = fleet_tools.build_fleet_tools(str(user.id))
    return executor.handler("list_fleet_doc_findings")


async def test_tool_header_equals_the_chip_and_the_shared_count(monkeypatch, lapsed_fleet) -> None:
    fleet, gate = lapsed_fleet.fleet, lapsed_fleet.gate
    view = await fleet_docs.documentation_view(object(), fleet, can_edit=True)
    chip = len(view.findings)
    count = await fleet_docs.open_suggestion_count(object(), fleet)
    answer = await _tool(monkeypatch, fleet)(fleet_id=str(fleet.id), action="list")
    assert chip == count == 1, (chip, count)
    assert f"Fleet **Office**: {chip} suggestion(s), 0 dismissed." in answer, answer
    assert f"`{gate['id']}` no_gate_on_sensitive_action (Analyst)" in answer, answer
    assert "[dismissal lapsed: Analyst was reconfigured since]" in answer, answer


async def test_a_lapsed_suggestion_is_dismissed_by_the_id_the_list_shows(monkeypatch, lapsed_fleet) -> None:
    fleet, gate = lapsed_fleet.fleet, lapsed_fleet.gate
    handler = _tool(monkeypatch, fleet)
    done = await handler(fleet_id=str(fleet.id), action="dismiss", ids=gate["id"], reason="gated upstream")
    assert "1 suggestion(s) dismissed." in done, done
    [record] = fleet.documentation["dismissed_findings"]
    assert record["id"] == gate["id"] and record["member_print"] == "a-2" and "lapsed" not in record
    assert await fleet_docs.open_suggestion_count(object(), fleet) == 0
    answer = await handler(fleet_id=str(fleet.id), action="list")
    assert "0 suggestion(s), 1 dismissed." in answer, answer


async def test_a_holding_dismissal_is_not_counted(monkeypatch, lapsed_fleet) -> None:
    lapsed_fleet.state["prints"] = {"": "fleet-1", "analyst": "a-1"}
    fleet = lapsed_fleet.fleet
    assert await fleet_docs.open_suggestion_count(object(), fleet) == 0
    answer = await _tool(monkeypatch, fleet)(fleet_id=str(fleet.id), action="list")
    assert "0 suggestion(s), 1 dismissed." in answer, answer


# ── 3. the draft reads the current connected instruction ───────────────────


def test_the_draft_reads_the_effective_connected_instruction() -> None:
    fleet = SimpleNamespace(
        id=uuid.uuid4(),
        name="Office",
        description="",
        skills=[],
        orchestrator_agent_id=uuid.UUID(int=1),
        default_context_inclusion="summary",
        default_collaboration_instruction="Ask the default way.",
        organization_scope={"mode": "primary"},
        autonomy_level="full_gates",
        approval_gates=[],
    )
    lead = _agent()
    partner = _agent(id=uuid.UUID(int=2), name="Partner", slug="partner")
    rows = [
        _row(),
        _row(agent_id=partner.id, role="connected", collaboration_instruction="Ask Partner for the new scope."),
        _row(agent_id=uuid.UUID(int=3), role="connected", collaboration_instruction=None),
    ]
    third = _agent(id=uuid.UUID(int=3), name="Third", slug="third")
    ctx = gen.draft_context(fleet, rows, {a.id: a for a in (lead, partner, third)}, [])
    by_name = {c["name"]: c for c in ctx["connected"]}
    assert by_name["Partner"]["collaboration_instruction"] == "Ask Partner for the new scope."
    # A row that inherits is given the fleet default, never an empty instruction.
    assert by_name["Third"]["collaboration_instruction"] == "Ask the default way."
    assert by_name["Third"]["conversation_shared"] == "summary"


def test_the_prompt_says_the_facts_win_over_the_current_documentation() -> None:
    messages = gen.build_messages({"name": "Office"}, {"purpose": "Old CISO text."}, ["purpose"])
    user = messages[-1].content
    assert gen.CURRENT_DOC_HEADER in user and "the fleet above wins" in user
    assert "keep it coherent" not in user
