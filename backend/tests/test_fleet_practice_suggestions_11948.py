# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
"""Good-practice suggestions of a fleet computed from its configuration (#11948 S2).

Each kind on a fleet "Alert Desk" (orchestrator Desk Lead, members Triage, Enrich, Report wired
orchestrator -> members only), the one-click apply of the back-consult, the handover type map
kept for every other target, dismissal and restore through the findings path, no model call.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import fleet_docs
from app.services import fleet_practice_suggestions as practice
from app.services.fleet_doc_findings import FINDING_KINDS

_computed = practice.practice_suggestions


def _agent(name: str, **fields) -> SimpleNamespace:
    base = {"id": uuid.uuid4(), "name": name, "handover_agent_ids": [], "handover_types": {}, "disable_chat": True}
    return SimpleNamespace(**{**base, **fields})


def _assignment(agent, trigger_type: str, config: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(agent_id=agent.id, trigger_type=trigger_type, trigger_config=config or {}, enabled=True)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _Db:
    """Answers the reads of ``practice_suggestions`` in order: agents, assignments, open work items, then
    the names of the working knowledge bases (#11972) when there is one."""

    def __init__(self, agents, assignments, open_owned, kb_names=None):
        self._answers = [agents, assignments, list(open_owned.items()), list((kb_names or {}).items())]

    async def execute(self, _statement):
        return _Result(self._answers.pop(0))


@pytest.fixture
def desk(monkeypatch):
    lead = _agent("Desk Lead")
    members = [_agent("Triage"), _agent("Enrich"), _agent("Report")]
    lead.handover_agent_ids = [str(m.id) for m in members]
    lead.handover_types = {str(m.id): "delegate" for m in members}
    # The working knowledge base (#11972): written by the lead, read by every member, so the knowledge
    # practices stay quiet in the tests of the other kinds.
    kb = str(uuid.uuid4())
    lead.knowledge_base_ids, lead.knowledge_base_permissions = [kb], {kb: "write"}
    for m in members:
        m.knowledge_base_ids, m.knowledge_base_permissions = [kb], None
    fleet = SimpleNamespace(id=uuid.uuid4(), name="Alert Desk", orchestrator_agent_id=lead.id, documentation=None)
    rows = [SimpleNamespace(agent_id=a.id, role="member") for a in (lead, *members)]

    async def _members(_db, _fleet_id):
        return rows

    monkeypatch.setattr("app.services.fleets.fleet_members", _members)
    listeners = [_assignment(lead, "platform_event", {"event": "work_item.completed"})]
    return SimpleNamespace(
        fleet=fleet,
        lead=lead,
        members=members,
        agents=[lead, *members],
        listeners=listeners,
        kb=kb,
        kb_names={kb: "Alert Desk - Working knowledge"},
    )


async def _suggestions(desk, assignments=None, open_owned=None):
    db = _Db(desk.agents, desk.listeners if assignments is None else assignments, open_owned or {}, desk.kb_names)
    return await _computed(db, desk.fleet)


async def test_each_member_wired_one_way_gets_one_back_consult_suggestion(desk) -> None:
    found = await _suggestions(desk)
    assert [(s["kind"], s["member"], s["subject"]) for s in found] == [
        ("member_cannot_consult_orchestrator", m.name, "Desk Lead") for m in desk.members
    ]
    assert all(s["applicable"] and s["source"] == "practice" for s in found)
    assert {s["agent_id"] for s in found} == {str(m.id) for m in desk.members}
    assert "Triage cannot consult Desk Lead" in found[0]["finding"]


async def test_the_id_is_stable_and_independent_of_the_wording(desk) -> None:
    first = await _suggestions(desk)
    again = await _suggestions(desk)
    assert [s["id"] for s in first] == [s["id"] for s in again]
    assert len({s["id"] for s in first}) == 3


async def test_a_member_that_may_consult_gets_no_suggestion(desk) -> None:
    triage, enrich, report = desk.members
    target = str(desk.lead.id)
    triage.handover_agent_ids, triage.handover_types = [target], {target: "consult"}
    enrich.handover_agent_ids, enrich.handover_types = [target], {}  # legacy map: every type
    report.handover_agent_ids, report.handover_types = [target], {target: "delegate"}
    found = await _suggestions(desk)
    assert [s["member"] for s in found] == ["Report"]


async def test_the_other_kinds(desk) -> None:
    triage = desk.members[0]
    target = str(desk.lead.id)
    for m in desk.members:
        m.handover_agent_ids, m.handover_types = [target], {target: "consult"}
    triage.disable_chat = False
    found = await _suggestions(desk, assignments=[], open_owned={desk.members[1].id: 2})
    assert [(s["kind"], s["member"]) for s in found] == [
        ("member_chat_open", "Triage"),
        ("owner_without_inbox", "Enrich"),
        ("orchestrator_without_listeners", "Desk Lead"),
    ]
    assert not any(s["applicable"] for s in found)


async def test_an_inbox_and_a_listener_clear_their_suggestions(desk) -> None:
    target = str(desk.lead.id)
    for m in desk.members:
        m.handover_agent_ids, m.handover_types = [target], {target: "consult"}
    enrich = desk.members[1]
    assignments = [
        _assignment(enrich, "work_item"),
        _assignment(desk.lead, "platform_event", {"event": ["run.failed"]}),
    ]
    assert await _suggestions(desk, assignments=assignments, open_owned={enrich.id: 1}) == []


async def test_a_decision_listener_counts_as_an_inbox(desk) -> None:
    """The seeded "Gate answered" listener (#1388) takes the owner's answered items: no owner_without_inbox."""
    target = str(desk.lead.id)
    for m in desk.members:
        m.handover_agent_ids, m.handover_types = [target], {target: "consult"}
    gate = _assignment(desk.lead, "platform_event", {"event": "work_item.status_changed", "filter": {}})
    found = await _suggestions(desk, assignments=[*desk.listeners, gate], open_owned={desk.lead.id: 3})
    assert [s["kind"] for s in found] == []
    # Any other platform event is still no inbox.
    other = _assignment(desk.lead, "platform_event", {"event": "run.failed"})
    found = await _suggestions(desk, assignments=[*desk.listeners, other], open_owned={desk.lead.id: 3})
    assert [(s["kind"], s["member"]) for s in found] == [("owner_without_inbox", "Desk Lead")]


async def test_a_fleet_without_orchestrator_gets_none() -> None:
    fleet = SimpleNamespace(id=uuid.uuid4(), orchestrator_agent_id=None)
    assert await practice.practice_suggestions(object(), fleet) == []


def test_the_practice_kinds_never_collide_with_the_generated_ones() -> None:
    assert not set(practice.PRACTICE_KINDS) & set(FINDING_KINDS)
    assert practice.APPLICABLE_KINDS.issubset(practice.PRACTICE_KINDS)
    assert practice.KNOWLEDGE_KINDS.issubset(practice.PRACTICE_KINDS)


@pytest.mark.parametrize(
    ("ids", "types", "want_types"),
    [
        (["a"], {"a": "delegate"}, {"a": "delegate", "o": "consult"}),
        (["a"], {}, {}),  # legacy map with targets: the new one joins them with every type
        ([], {}, {"o": "consult"}),
        (["o"], {"o": "delegate"}, {"o": "consult"}),
    ],
)
def test_the_back_consult_keeps_every_other_target_running_as_before(ids, types, want_types) -> None:
    new_ids, new_types = practice.back_consult_handovers("o", ids, types)
    assert new_ids == [*dict.fromkeys([*ids, "o"])]
    assert new_types == want_types
    assert practice.can_consult("o", new_ids, new_types)


async def test_apply_adds_the_consult_once(desk) -> None:
    triage = desk.members[0]
    assert await practice.apply_back_consult(object(), desk.fleet, triage) is True
    assert triage.handover_agent_ids == [str(desk.lead.id)]
    assert triage.handover_types == {str(desk.lead.id): "consult"}
    assert await practice.apply_back_consult(object(), desk.fleet, triage) is False
    assert await practice.apply_back_consult(object(), desk.fleet, desk.lead) is False


@pytest.fixture
def stored_view(monkeypatch, desk):
    async def _prints(_db, _fleet):
        return {"": "p", "triage": "t1", "enrich": "e1", "report": "r1"}

    async def _fp(_db, _fleet):
        return "f"

    computed = {"items": None}

    async def _practice(_db, fleet):
        return computed["items"]

    monkeypatch.setattr(fleet_docs, "current_member_fingerprints", _prints)
    monkeypatch.setattr(fleet_docs, "current_fingerprint", _fp)
    monkeypatch.setattr(practice, "practice_suggestions", _practice)
    desk.fleet.is_builtin = False
    desk.fleet.documentation = {"sections": {"purpose": "Triage alerts."}, "version": 1, "findings": []}
    return computed


async def test_suggestions_show_to_managers_only_and_dismiss_and_restore_like_findings(desk, stored_view) -> None:
    stored_view["items"] = await _suggestions(desk)
    view = await fleet_docs.documentation_view(object(), desk.fleet, can_edit=True)
    assert [f["member"] for f in view.findings] == ["Triage", "Enrich", "Report"]
    assert (await fleet_docs.documentation_view(object(), desk.fleet, can_edit=False)).findings == []

    enrich = view.findings[1]
    done = await fleet_docs.dismiss_findings(object(), desk.fleet, [enrich["id"]], by="u", reason="standing decision")
    assert [d["id"] for d in done] == [enrich["id"]]
    view = await fleet_docs.documentation_view(object(), desk.fleet, can_edit=True)
    assert [f["member"] for f in view.findings] == ["Triage", "Report"]
    assert [d["member"] for d in view.dismissed_findings] == ["Enrich"]
    # The page words a dismissed practice row in the reader's language from its kind (#11961 UX r2 B1).
    assert [(d["kind"], d["source"]) for d in view.dismissed_findings] == [(enrich["kind"], "practice")]
    assert desk.fleet.documentation["findings"] == [], "a practice suggestion is never stored"

    assert fleet_docs.restore_findings(desk.fleet, [enrich["id"]]) == 1
    assert desk.fleet.documentation["findings"] == []
    view = await fleet_docs.documentation_view(object(), desk.fleet, can_edit=True)
    assert len(view.findings) == 3


# Built-in fleets suggest only what the reader can change (#1510).


def _open_everywhere(desk) -> None:
    desk.members[0].disable_chat = False


async def test_a_built_in_fleet_gets_no_practice_suggestion(desk) -> None:
    _open_everywhere(desk)
    desk.fleet.is_builtin = True
    assert await _suggestions(desk, assignments=[], open_owned={desk.members[1].id: 2}) == []


async def test_a_fork_of_a_built_in_fleet_keeps_every_practice(desk) -> None:
    _open_everywhere(desk)
    desk.fleet.is_builtin = False
    found = await _suggestions(desk, assignments=[], open_owned={desk.members[1].id: 2})
    assert {s["kind"] for s in found} == {
        "member_cannot_consult_orchestrator",
        "member_chat_open",
        "owner_without_inbox",
        "orchestrator_without_listeners",
    }


async def test_a_built_in_member_of_a_custom_fleet_gets_no_practice(desk, monkeypatch) -> None:
    from app.core.config import Settings

    monkeypatch.setattr(Settings, "is_xtm_one", property(lambda _self: True))
    triage = desk.members[0]
    triage.is_builtin = True
    found = await _suggestions(desk)
    assert [s["member"] for s in found] == ["Enrich", "Report"]


def test_every_kind_names_its_surface_and_only_the_customer_layer_stays_on_a_built_in() -> None:
    assert set(practice.PRACTICE_KINDS) | set(FINDING_KINDS) <= set(practice.KIND_SURFACES)
    builtin = SimpleNamespace(is_builtin=True)
    custom = SimpleNamespace(is_builtin=False)
    kept = {k for k in practice.KIND_SURFACES if practice.suggestion_editable(builtin, {"kind": k})}
    assert kept == {"missing_integration", "no_gate_on_sensitive_action"}
    assert all(practice.suggestion_editable(custom, {"kind": k}) for k in practice.KIND_SURFACES)
    # Derived from the lock's own field sets, not a second list.
    assert practice.builtin_editable_surfaces() == {"agent_integrations", "fleet_gates"}


async def test_the_chip_the_list_and_the_count_agree_on_a_built_in_fleet(desk, stored_view) -> None:
    stored_view["items"] = []
    desk.fleet.is_builtin = True
    desk.fleet.documentation["findings"] = [
        {"kind": "missing_automation", "member": "Triage", "subject": "schedule", "finding": "No schedule."},
        {"kind": "missing_integration", "member": "Enrich", "subject": "opencti", "finding": "No OpenCTI."},
        {"kind": "member_without_role", "member": "Report", "subject": "role label", "finding": "No role."},
    ]
    view = await fleet_docs.documentation_view(object(), desk.fleet, can_edit=True)
    assert [f["kind"] for f in view.findings] == ["missing_integration"]
    assert await fleet_docs.open_suggestion_count(object(), desk.fleet) == 1
    shown, _held, _prints = await fleet_docs.manager_suggestions(object(), desk.fleet)
    assert [f["kind"] for f in shown] == ["missing_integration"]

    desk.fleet.is_builtin = False  # a fork: every finding again
    assert await fleet_docs.open_suggestion_count(object(), desk.fleet) == 3
