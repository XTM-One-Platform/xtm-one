"""Fleet suggestions: restore puts the finding back, regenerations are stable (retest of #10546).

Pinned here, without a database: a restored finding a regeneration had dropped
comes back as active with the same id (it was in neither list); three whole
regenerations of an unchanged fleet store the same finding set whatever the
model answers; a member reconfigured takes the model's new findings for that
member only; a gate added on the fleet clears its gate finding; a first
generation (no stored prints) takes the model's set whole.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.services import fleet_doc_generation as gen
from app.services.fleet_doc_findings import (
    GATES_PRINT_KEY,
    clean_findings,
    dismissal_record,
    gates_print,
    stable_findings,
    with_restored,
)
from app.services.fleet_docs import DOC_SECTIONS

ROLE = {"kind": "member_without_role", "member": "", "finding": "Members have no role label."}
GATE = {"kind": "no_gate_on_sensitive_action", "member": "Assistant", "subject": "send_email", "finding": "No gate."}
LEGAL = {"kind": "missing_automation", "member": "Legal", "subject": "NDA review", "finding": "No NDA review."}
PER_MEMBER = [
    {"kind": "member_without_role", "member": name, "finding": f"{name} has no role label."}
    for name in ("Assistant", "Legal", "Developer")
]


def _prints(**changed: str) -> dict[str, str]:
    base = {"": "fleet-1", "assistant": "a-1", "legal": "l-1", "developer": "d-1", GATES_PRINT_KEY: "g-1"}
    return {**base, **changed}


def test_restore_puts_back_a_finding_a_regeneration_dropped() -> None:
    [finding] = clean_findings([LEGAL])
    records = dismissal_record({}, {**finding, "suggested_fix": "Add it."}, "l-1", by="u", reason="later")
    back = with_restored([], records)
    assert [f["id"] for f in back] == [finding["id"]]
    assert back[0]["suggested_fix"] == "Add it."
    # Already stored: nothing is added twice.
    assert with_restored([finding], records) == [finding]


def test_three_regenerations_of_an_unchanged_fleet_store_the_same_set() -> None:
    first = stable_findings([], clean_findings([ROLE, GATE]), None, _prints())
    answers = [[], PER_MEMBER, [LEGAL, *PER_MEMBER]]
    stored = first
    for answer in answers:
        stored = stable_findings(stored, clean_findings(answer), _prints(), _prints())
        assert [f["id"] for f in stored] == [f["id"] for f in first]


def test_a_reconfigured_member_takes_the_new_findings_for_that_member_only() -> None:
    stored = clean_findings([ROLE, GATE])
    after = stable_findings(stored, clean_findings([LEGAL, *PER_MEMBER]), _prints(), _prints(legal="l-2"))
    assert [(f["kind"], f["member"]) for f in after] == [
        ("member_without_role", ""),
        ("no_gate_on_sensitive_action", "Assistant"),
        ("missing_automation", "Legal"),
        ("member_without_role", "Legal"),
    ]
    # The fleet card changes: a fleet-level finding the model no longer returns goes.
    assert [f["kind"] for f in stable_findings(stored, [], _prints(), _prints(**{"": "fleet-2"}))] == [
        "no_gate_on_sensitive_action"
    ]


def test_a_gate_added_on_the_fleet_clears_the_gate_finding() -> None:
    stored = clean_findings([ROLE, GATE])
    after = stable_findings(stored, [], _prints(), _prints(**{GATES_PRINT_KEY: "g-2"}))
    assert [f["kind"] for f in after] == ["member_without_role"]
    assert gates_print("full_gates", []) != gates_print("full_gates", [{"tools": ["send_email"]}])


def test_a_first_generation_takes_the_model_set_whole() -> None:
    assert stable_findings(clean_findings([ROLE]), clean_findings([LEGAL]), None, _prints()) == clean_findings([LEGAL])
    assert stable_findings(clean_findings([ROLE]), clean_findings([LEGAL]), {}, _prints()) == clean_findings([LEGAL])


# ── through fleet_docs and the generator, no database ───────────────────────


@pytest.fixture
def no_database(monkeypatch):
    from app.services import fleet_docs

    async def _ctx(_db, _fleet):
        return {"members": [], "automations": [], "approvals": {}}

    async def _fp(_db, _fleet):
        return "f" * 16

    async def _member_prints(_db, _fleet):
        return {"": "fleet-1", "assistant": "a-1", "legal": "l-1", "developer": "d-1"}

    monkeypatch.setattr(gen, "load_draft_context", _ctx)
    monkeypatch.setattr(fleet_docs, "current_fingerprint", _fp)
    monkeypatch.setattr(fleet_docs, "current_member_fingerprints", _member_prints)


class _Model:
    def __init__(self, *answers: list[dict]) -> None:
        self.answers = list(answers)

    async def chat(self, messages, *, model, temperature, max_tokens):
        findings = self.answers.pop(0)
        body = {
            "sections": {k: f"Text of {k}." for k in DOC_SECTIONS},
            "structured": {"purpose_line": "Runs the office."},
            "findings": findings,
        }
        return SimpleNamespace(content=json.dumps(body), stop_reason="end_turn")


def _fleet() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="CEO Office",
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


async def test_regenerate_three_times_without_a_change_keeps_the_set(no_database) -> None:
    fleet = _fleet()
    model = _Model([ROLE, GATE], [], PER_MEMBER, [LEGAL])
    ids = []
    for _ in range(4):
        await gen.regenerate_fleet_documentation(object(), uuid.uuid4(), fleet, llm=model)
        ids.append(sorted(f["id"] for f in fleet.documentation.get("findings") or []))
    assert ids[0] and ids == [ids[0]] * 4
    assert fleet.documentation["finding_prints"][GATES_PRINT_KEY]


async def test_restore_after_a_regeneration_keeps_the_finding_active(no_database) -> None:
    from app.services.fleet_docs import dismiss_findings, documentation_view, restore_findings

    fleet = _fleet()
    await gen.regenerate_fleet_documentation(object(), uuid.uuid4(), fleet, llm=_Model([LEGAL, ROLE]))
    [legal] = [f for f in fleet.documentation["findings"] if f["member"] == "Legal"]
    await dismiss_findings(object(), fleet, [legal["id"]], by="u", reason="later")
    # A stored list without the finding (a regeneration of the former code dropped it).
    fleet.documentation = {
        **fleet.documentation,
        "findings": [f for f in fleet.documentation["findings"] if f["id"] != legal["id"]],
    }
    assert restore_findings(fleet, [legal["id"]]) == 1
    view = await documentation_view(object(), fleet, can_edit=True)
    assert legal["id"] in [f["id"] for f in view.findings]
    assert view.dismissed_findings == []
    # It can be dismissed again: the id names a stored finding.
    assert [f["id"] for f in await dismiss_findings(object(), fleet, [legal["id"]], by="u")] == [legal["id"]]
