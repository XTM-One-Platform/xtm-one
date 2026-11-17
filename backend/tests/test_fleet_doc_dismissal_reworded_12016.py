"""Fleet suggestions: a dismissal follows the point, not the model's words (#12016).

Pinned here, without a database: a dismissed finding retaken by a whole
regeneration under a reworded subject (the gates' print changed, or its scope
retaken) stays hidden, keeps its original id and counts as dismissed in the
fleet tool; a one-per-member kind matches whatever its subject; a genuinely
different point of the same kind on the same member is not hidden; a member
reconfigured lapses the dismissal and the fleet tool says why; a gates change
never lapses it; decided points still carry the matched dismissal.
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
    GATES_PRINT_KEY,
    anchored,
    clean_findings,
    decided_points,
    dismissal_record,
    lapse_reason,
    lapsed_dismissals,
    same_point,
    stable_findings,
    visible_findings,
)
from app.services.fleet_docs import DOC_SECTIONS

GATE = {
    "kind": "no_gate_on_sensitive_action",
    "member": "Assistant",
    "subject": "send_email",
    "finding": "The Assistant sends email with no approval gate.",
}
GATE_REWORDED = {
    "kind": "no_gate_on_sensitive_action",
    "member": "assistant",
    "subject": "Send email action",
    "finding": "Outgoing email from the Assistant is not gated.",
}
GATE_OTHER = {
    "kind": "no_gate_on_sensitive_action",
    "member": "Assistant",
    "subject": "delete_github_file",
    "finding": "The Assistant deletes repository files with no approval gate.",
}


def _prints(**changed: str) -> dict[str, str]:
    return {**{"": "fleet-1", "assistant": "a-1", GATES_PRINT_KEY: "g-1"}, **changed}


def test_same_point_matches_a_reworded_subject_and_not_another_point() -> None:
    assert same_point(GATE, GATE_REWORDED)
    assert not same_point(GATE, GATE_OTHER)
    assert not same_point(GATE, {**GATE_REWORDED, "member": "Developer"})
    role = {"kind": "member_without_role", "member": "Legal", "subject": "role label", "finding": "No role."}
    assert same_point(role, {"kind": "member_without_role", "member": "legal", "finding": "Legal lacks a label."})
    # One side without a subject: its line must name the other's subject.
    assert same_point(GATE, {**GATE_REWORDED, "subject": "", "finding": "No gate on send email."})
    assert not same_point(GATE, {**GATE_REWORDED, "subject": "", "finding": "Something else entirely."})


def test_a_retaken_finding_keeps_the_dismissed_id_and_stays_hidden() -> None:
    [first] = clean_findings([GATE])
    doc = {"findings": [first]}
    doc["dismissed_findings"] = dismissal_record(doc, first, "a-1", by="u", reason="gated upstream")
    # The fleet's gates changed: the gate finding is retaken and the model rewords its subject.
    after = stable_findings(
        [first],
        clean_findings([GATE_REWORDED, GATE_OTHER]),
        _prints(),
        _prints(**{GATES_PRINT_KEY: "g-2"}),
        dismissed=doc["dismissed_findings"],
    )
    assert after[0]["id"] == first["id"]
    doc["findings"] = after
    shown = visible_findings(doc, _prints(**{GATES_PRINT_KEY: "g-2"}))
    assert [f["subject"] for f in shown] == ["delete_github_file"]
    assert decided_points(doc, _prints(**{GATES_PRINT_KEY: "g-2"})) == [
        "Assistant: send_email (no_gate_on_sensitive_action). Decided: gated upstream"
    ]


def test_an_old_stored_finding_under_a_new_id_is_hidden_by_the_dismissal() -> None:
    [first] = clean_findings([GATE])
    [reworded] = clean_findings([GATE_REWORDED])
    assert reworded["id"] != first["id"]
    doc = {"findings": [reworded], "dismissed_findings": dismissal_record({}, first, "a-1", by="u")}
    assert visible_findings(doc, _prints()) == []
    assert anchored([reworded], doc["dismissed_findings"])[0]["id"] == first["id"]


def test_a_reconfigured_member_lapses_the_dismissal_and_says_why() -> None:
    [first] = clean_findings([GATE])
    doc = {"findings": [first], "dismissed_findings": dismissal_record({}, first, "a-1", by="u")}
    assert lapse_reason(doc["dismissed_findings"][0], _prints(**{GATES_PRINT_KEY: "g-2"})) is None
    assert [f["id"] for f in visible_findings(doc, _prints(assistant="a-2"))] == [first["id"]]
    [(record, why)] = lapsed_dismissals(doc, _prints(assistant="a-2"))
    assert record["id"] == first["id"] and why == "Assistant was reconfigured since"
    old = dismissal_record({}, first, "a-1", by="u", now=datetime.now(UTC) - timedelta(days=DISMISSAL_DAYS + 1))
    assert lapse_reason(old[0], _prints()) == f"older than {DISMISSAL_DAYS} days"


# ── through the generator and the fleet tool, no database ───────────────────


@pytest.fixture
def no_database(monkeypatch):
    from app.services import fleet_docs

    state = {"": "fleet-1", "assistant": "a-1"}

    async def _ctx(_db, _fleet):
        return {"members": [], "automations": [], "approvals": {}}

    async def _fp(_db, _fleet):
        return "f" * 16

    async def _member_prints(_db, _fleet):
        return dict(state)

    monkeypatch.setattr(gen, "load_draft_context", _ctx)
    monkeypatch.setattr(fleet_docs, "current_fingerprint", _fp)
    monkeypatch.setattr(fleet_docs, "current_member_fingerprints", _member_prints)
    return state


class _Model:
    def __init__(self, *answers: list[dict]) -> None:
        self.answers = list(answers)

    async def chat(self, messages, *, model, temperature, max_tokens):
        body = {
            "sections": {k: f"Text of {k}." for k in DOC_SECTIONS},
            "structured": {"purpose_line": "Runs the office."},
            "findings": self.answers.pop(0),
        }
        return SimpleNamespace(content=json.dumps(body), stop_reason="end_turn")


def _fleet() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Office",
        description="",
        skills=[],
        is_builtin=False,
        documentation=None,
        orchestrator_agent_id=None,
        default_context_inclusion="none",
        default_collaboration_instruction="",
        organization_scope={"mode": "primary"},
        autonomy_level="full_gates",
        approval_gates=[],
    )


async def _tool_list(monkeypatch, fleet) -> str:
    from contextlib import asynccontextmanager

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

    monkeypatch.setattr(fleet_tools.database, "async_session", _factory)
    monkeypatch.setattr(fleet_tools._builder, "_load_caller_user_for_builder", _load)
    monkeypatch.setattr(fleet_tools._builder, "_load_manageable_entity_for_builder", _manager)
    _defs, executor = fleet_tools.build_fleet_tools(str(user.id))
    return await executor.handler("list_fleet_doc_findings")(fleet_id=str(fleet.id), action="list")


async def test_dismiss_then_regenerate_with_a_reworded_subject_keeps_it_dismissed(monkeypatch, no_database) -> None:
    from app.services.fleet_docs import dismiss_findings, documentation_view

    fleet = _fleet()
    await gen.regenerate_fleet_documentation(object(), uuid.uuid4(), fleet, llm=_Model([GATE]))
    [gate] = fleet.documentation["findings"]
    await dismiss_findings(object(), fleet, [gate["id"]], by="u", reason="gated upstream")
    # A gate added on the fleet retakes the gate scope; the model words the same point differently.
    fleet.approval_gates = [{"tools": ["create_github_pr"]}]
    await gen.regenerate_fleet_documentation(object(), uuid.uuid4(), fleet, llm=_Model([GATE_REWORDED]))
    assert [f["id"] for f in fleet.documentation["findings"]] == [gate["id"]]
    view = await documentation_view(object(), fleet, can_edit=True)
    assert view.findings == [] and [d["id"] for d in view.dismissed_findings] == [gate["id"]]
    answer = await _tool_list(monkeypatch, fleet)
    assert "0 suggestion(s), 1 dismissed." in answer, answer
    assert f"`{gate['id']}` [dismissed by" in answer


async def test_the_tool_says_why_a_dismissal_lapsed(monkeypatch, no_database) -> None:
    from app.services.fleet_docs import dismiss_findings

    fleet = _fleet()
    await gen.regenerate_fleet_documentation(object(), uuid.uuid4(), fleet, llm=_Model([GATE]))
    [gate] = fleet.documentation["findings"]
    await dismiss_findings(object(), fleet, [gate["id"]], by="u")
    no_database["assistant"] = "a-2"
    answer = await _tool_list(monkeypatch, fleet)
    assert "1 suggestion(s), 0 dismissed." in answer, answer
    assert f"`{gate['id']}` no_gate_on_sensitive_action" in answer, answer
    assert "[dismissal lapsed: Assistant was reconfigured since]" in answer, answer
