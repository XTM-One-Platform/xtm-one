# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
"""The knowledge practices of a fleet, computed from its configuration (#11972).

Fleet "Alert Desk" (orchestrator Desk Lead, members Triage, Enrich, Report): each kind, the one-click
apply of the write grant and of the read-only binding, the dismissal of an isolated member, the seeded
skill section. Suggestions only: no model call, nothing refused.
"""

from __future__ import annotations

import re
import uuid
from types import SimpleNamespace

import pytest

from app.services import fleet_docs
from app.services import fleet_practice_suggestions as practice

KB_NAME = "Alert Desk - Working knowledge"


def _agent(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        handover_agent_ids=[],
        handover_types={},
        disable_chat=True,
        knowledge_base_ids=[],
        knowledge_base_permissions=None,
    )


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _Db:
    """Agents, assignments, open work items, then the knowledge base names."""

    def __init__(self, agents, listeners, kb_names):
        self._answers = [agents, listeners, [], list(kb_names.items())]

    async def execute(self, _statement):
        return _Result(self._answers.pop(0) if self._answers else [])


@pytest.fixture
def desk(monkeypatch):
    lead = _agent("Desk Lead")
    members = [_agent("Triage"), _agent("Enrich"), _agent("Report")]
    target = str(lead.id)
    for m in members:  # the other practices hold, so only the knowledge ones show
        m.handover_agent_ids, m.handover_types = [target], {target: "consult"}
    fleet = SimpleNamespace(id=uuid.uuid4(), name="Alert Desk", orchestrator_agent_id=lead.id, documentation=None)
    rows = [SimpleNamespace(agent_id=a.id, role="member") for a in (lead, *members)]

    async def _members(_db, _fleet_id):
        return rows

    monkeypatch.setattr("app.services.fleets.fleet_members", _members)
    listener = SimpleNamespace(
        agent_id=lead.id, trigger_type="platform_event", trigger_config={"event": "run.failed"}, enabled=True
    )
    kb = str(uuid.uuid4())
    return SimpleNamespace(fleet=fleet, lead=lead, members=members, listeners=[listener], kb=kb)


def _bind_all(desk, *, lead_writes: bool) -> None:
    for a in (desk.lead, *desk.members):
        a.knowledge_base_ids = [desk.kb]
    desk.lead.knowledge_base_permissions = {desk.kb: "write"} if lead_writes else None


async def _found(desk):
    db = _Db([desk.lead, *desk.members], desk.listeners, {desk.kb: KB_NAME})
    return [s for s in await practice.practice_suggestions(db, desk.fleet) if s["kind"] in practice.KNOWLEDGE_KINDS]


async def test_a_shared_base_the_orchestrator_cannot_write_gives_one_suggestion(desk) -> None:
    _bind_all(desk, lead_writes=False)
    found = await _found(desk)
    assert [(s["kind"], s["member"], s["subject"]) for s in found] == [
        ("orchestrator_cannot_write_knowledge", "Desk Lead", KB_NAME)
    ]
    assert found[0]["applicable"] and found[0]["knowledge_base_id"] == desk.kb
    assert found[0]["agent_id"] == str(desk.lead.id)


async def test_applying_the_grant_writes_it_and_the_suggestion_goes(desk) -> None:
    _bind_all(desk, lead_writes=False)
    assert practice.apply_knowledge_suggestion(desk.fleet, desk.lead, "orchestrator_cannot_write_knowledge", desk.kb)
    assert desk.lead.knowledge_base_permissions == {desk.kb: "write"}
    assert desk.lead.knowledge_base_ids == [desk.kb], "the binding is unchanged"
    assert await _found(desk) == []
    assert not practice.apply_knowledge_suggestion(
        desk.fleet, desk.lead, "orchestrator_cannot_write_knowledge", desk.kb
    ), "applied once"


async def test_a_member_left_out_gets_a_read_only_binding(desk) -> None:
    _bind_all(desk, lead_writes=True)
    report = desk.members[2]
    report.knowledge_base_ids = []
    found = await _found(desk)
    assert [(s["kind"], s["member"]) for s in found] == [("member_missing_working_knowledge", "Report")]
    assert found[0]["applicable"]
    assert practice.apply_knowledge_suggestion(desk.fleet, report, "member_missing_working_knowledge", desk.kb)
    assert report.knowledge_base_ids == [desk.kb]
    assert report.knowledge_base_permissions is None, "read-only"
    assert await _found(desk) == []


async def test_a_fleet_without_a_shared_base_gets_the_informational_suggestion_only(desk) -> None:
    found = await _found(desk)
    assert [(s["kind"], s["member"], s["applicable"]) for s in found] == [
        ("fleet_without_working_knowledge", "Desk Lead", False)
    ]
    # A base only some members read and the orchestrator does not write is no working base either.
    desk.lead.knowledge_base_ids = [desk.kb]
    desk.members[0].knowledge_base_ids = [desk.kb]
    assert [s["kind"] for s in await _found(desk)] == ["fleet_without_working_knowledge"]


async def test_a_built_in_fleet_gets_no_knowledge_suggestion_and_its_fork_does(desk) -> None:
    """The knowledge wiring of a built-in fleet is seeded mechanics (#1510): its fork keeps the suggestion."""
    desk.fleet.is_builtin = True
    assert await _found(desk) == []
    desk.fleet.is_builtin = False
    assert [s["kind"] for s in await _found(desk)] == ["fleet_without_working_knowledge"]


async def test_a_second_writer_is_named(desk) -> None:
    _bind_all(desk, lead_writes=True)
    desk.members[1].knowledge_base_permissions = {desk.kb: "write"}
    found = await _found(desk)
    assert [(s["kind"], s["applicable"]) for s in found] == [("several_writers_on_working_knowledge", False)]
    assert "Enrich" in found[0]["finding"]


def test_apply_refuses_the_wrong_agent_or_kind(desk) -> None:
    _bind_all(desk, lead_writes=False)
    triage = desk.members[0]
    assert not practice.apply_knowledge_suggestion(desk.fleet, triage, "orchestrator_cannot_write_knowledge", desk.kb)
    assert not practice.apply_knowledge_suggestion(desk.fleet, desk.lead, "member_missing_working_knowledge", desk.kb)
    assert not practice.apply_knowledge_suggestion(desk.fleet, triage, "several_writers_on_working_knowledge", desk.kb)
    assert triage.knowledge_base_permissions is None and desk.lead.knowledge_base_permissions is None


@pytest.mark.parametrize(
    ("lead", "members", "writes", "expected"),
    [
        (["k"], [["k"], ["k"]], False, ["k"]),
        (["k"], [["k"], []], True, ["k"]),
        (["k"], [["k"], []], False, []),
        (["k"], [[], []], True, []),
        (["k"], [], True, []),
    ],
)
def test_working_knowledge(lead, members, writes, expected) -> None:
    orchestrator = SimpleNamespace(
        knowledge_base_ids=lead, knowledge_base_permissions={"k": "write"} if writes else None
    )
    others = [SimpleNamespace(knowledge_base_ids=ids, knowledge_base_permissions=None) for ids in members]
    assert practice.working_knowledge(orchestrator, others) == expected


async def test_an_isolated_member_dismissed_with_a_reason_stays_dismissed(monkeypatch, desk) -> None:
    _bind_all(desk, lead_writes=True)
    report = desk.members[2]
    report.knowledge_base_ids = []
    items = await _found(desk)

    async def _practice(_db, _fleet):
        return items

    async def _prints(_db, _fleet):
        return {"": "p", "report": "r1"}

    async def _fp(_db, _fleet):
        return "f"

    monkeypatch.setattr(practice, "practice_suggestions", _practice)
    monkeypatch.setattr(fleet_docs, "current_member_fingerprints", _prints)
    monkeypatch.setattr(fleet_docs, "current_fingerprint", _fp)
    desk.fleet.is_builtin = False
    desk.fleet.documentation = {"sections": {"purpose": "Triage alerts."}, "version": 1, "findings": []}

    view = await fleet_docs.documentation_view(object(), desk.fleet, can_edit=True)
    assert [f["kind"] for f in view.findings] == ["member_missing_working_knowledge"]
    done = await fleet_docs.dismiss_findings(
        object(), desk.fleet, [items[0]["id"]], by="u", reason="isolated, untrusted callers"
    )
    assert [d["id"] for d in done] == [items[0]["id"]]
    view = await fleet_docs.documentation_view(object(), desk.fleet, can_edit=True)
    assert view.findings == []
    assert [d["member"] for d in view.dismissed_findings] == ["Report"]


def test_the_seeded_skill_carries_the_knowledge_and_records_section_as_suggestions() -> None:
    from app.agents.builtin_agents import BUILTIN_SKILLS
    from app.agents.seed import _resolve_seed_content

    entry = next(s for s in BUILTIN_SKILLS if s["slug"] == "fleet-design-practices")
    content = _resolve_seed_content("skill_content", entry["content"])
    section = content.split("## 10. Knowledge and records", 1)[1].split("\n## ", 1)[0]
    for words in ("Working knowledge", "write grant", "read-only", "10 Fleet runbook", "90 Lessons log", "KNOWLEDGE:"):
        assert words in section
    assert not re.search(r"\b(must|never)\b", section, re.I)
