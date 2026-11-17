"""Fleet About: configuration findings become actions, generator notes leave the page (#10246).

Pinned here, without a database: the prompt forbids meta-comments and asks for
findings; a meta-comment still written in a section is moved out as a finding;
the findings schema keeps only valid entries; a missing section is asked once
more on its own; a dismissed finding stays hidden until the configuration
fingerprint changes; generator notes are written to the version history only.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.services import fleet_doc_generation as gen
from app.services.fleet_doc_findings import (
    FINDINGS_MAX,
    clean_findings,
    dismissal_record,
    is_meta_comment,
    move_meta_comments,
    visible_findings,
)
from app.services.fleet_docs import DOC_SECTIONS, documentation_history, documentation_view, save_documentation
from app.services.fleets import FleetActionRefusedError

# The two sentences of the CEO Office fleet's version 8 Sam pointed at (9 Nov 2026).
V8_DESCRIPTION = "Its description also names the weekly company health report."
V8_LISTED = "No separate assignment for that report is listed."
V8_CONFIRM = "The owner should confirm which actions, if any, need approval."


def test_the_prompt_forbids_meta_comments_and_asks_for_findings() -> None:
    system = gen.build_messages({"fleet": {"name": "x"}}, {}, ["purpose"])[0].content
    assert "never comments on its configuration" in system
    assert '"findings"' in system
    for kind in ("description_mismatch", "missing_automation", "no_gate_on_sensitive_action"):
        assert kind in system


@pytest.mark.parametrize("sentence", [V8_DESCRIPTION, V8_LISTED, V8_CONFIRM])
def test_the_v8_sentences_read_as_meta_comments(sentence: str) -> None:
    assert is_meta_comment(sentence)


@pytest.mark.parametrize(
    "sentence",
    [
        "The Assistant routes each request to the right desk.",
        "Every Monday the fleet writes the company health report.",
        "Sam approves every email before it is sent.",
    ],
)
def test_a_plain_explanation_is_not_a_meta_comment(sentence: str) -> None:
    assert not is_meta_comment(sentence)


def test_a_meta_comment_is_moved_out_of_its_section_into_a_finding() -> None:
    sections = {
        "purpose": f"Runs the CEO's office. {V8_DESCRIPTION} {V8_LISTED}",
        "approvals": f"- Sam approves outgoing mail.\n- {V8_CONFIRM}",
        "flow": "The Assistant hands work to the desks.",
    }
    cleaned, moved = move_meta_comments(sections)
    assert cleaned["purpose"] == "Runs the CEO's office."
    assert cleaned["approvals"] == "- Sam approves outgoing mail."
    assert cleaned["flow"] == sections["flow"]
    assert [f["finding"] for f in moved] == [V8_DESCRIPTION, V8_LISTED, V8_CONFIRM]
    assert all(f["kind"] == "other" for f in moved)


def test_a_section_made_only_of_meta_comments_keeps_its_text() -> None:
    cleaned, moved = move_meta_comments({"purpose": V8_CONFIRM})
    assert cleaned == {"purpose": V8_CONFIRM}
    assert moved == []


def test_the_findings_schema_keeps_valid_entries_only() -> None:
    raw = [
        {"kind": "missing_automation", "member": "Assistant", "finding": "No weekly report.", "suggested_fix": "Add."},
        {"kind": "invented", "finding": "Unknown kind becomes other."},
        {"kind": "missing_automation", "member": "Assistant", "finding": "No weekly report.", "suggested_fix": "Add."},
        {"kind": "other", "finding": "   "},
        "not an object",
        {"kind": "other", "finding": "x" * 1000},
    ]
    out = clean_findings(raw)
    assert [f["kind"] for f in out] == ["missing_automation", "other", "other"]
    assert len(out[2]["finding"]) == 300
    assert all(len(f["id"]) == 12 for f in out)
    assert clean_findings(None) == [] and clean_findings({"kind": "other"}) == []
    many = [{"finding": f"Finding {i}."} for i in range(FINDINGS_MAX + 5)]
    assert len(clean_findings(many)) == FINDINGS_MAX


def test_a_finding_id_is_stable_across_regenerations() -> None:
    one = clean_findings([{"kind": "other", "member": "A", "finding": "Same line."}])[0]
    two = clean_findings([{"kind": "other", "member": "a", "finding": "  Same line. ", "suggested_fix": "x"}])[0]
    assert one["id"] == two["id"]


def test_a_dismissed_finding_is_hidden_until_its_member_changes() -> None:
    # #10536: the dismissal is keyed on the member it names, no longer on the whole configuration.
    findings = clean_findings([{"finding": "One."}, {"finding": "Two."}])
    doc = {"findings": findings}
    doc["dismissed_findings"] = dismissal_record(doc, findings[0], "fp-1", by="u")
    assert [f["finding"] for f in visible_findings(doc, {"": "fp-1"})] == ["Two."]
    assert [f["finding"] for f in visible_findings(doc, {"": "fp-2"})] == ["One.", "Two."]
    again = dismissal_record(doc, findings[0], "fp-2", by="u")
    assert len(again) == 1 and again[0]["member_print"] == "fp-2"


def test_the_answer_s_findings_and_moved_sentences_reach_the_draft() -> None:
    answer = {
        "sections": {"purpose": f"Runs the office. {V8_DESCRIPTION}"},
        "structured": {},
        "findings": [{"kind": "description_mismatch", "member": "", "finding": "Report named, not configured."}],
    }
    draft = gen.extract_sections(json.dumps(answer), ["purpose"])
    assert draft.sections == {"purpose": "Runs the office."}
    assert [f["kind"] for f in draft.findings] == ["description_mismatch", "other"]


# ── With the generator, no database ─────────────────────────────────────────


class _Model:
    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.messages: list = []

    async def chat(self, messages, *, model, temperature, max_tokens):
        self.messages.append(messages)
        return SimpleNamespace(content=self.answers.pop(0), stop_reason="end_turn")


def _fleet(**fields) -> SimpleNamespace:
    base = {"id": uuid.uuid4(), "name": "CEO Office", "is_builtin": False, "documentation": None}
    return SimpleNamespace(**{**base, **fields})


@pytest.fixture
def no_database(monkeypatch):
    from app.services import fleet_docs

    async def _ctx(_db, _fleet):
        return {"members": [], "automations": [], "approvals": {}}

    calls = {"fp": "f" * 16}

    async def _fp(_db, _fleet):
        return calls["fp"]

    async def _prints(_db, _fleet):
        return {"": calls["fp"]}

    monkeypatch.setattr(gen, "load_draft_context", _ctx)
    monkeypatch.setattr(fleet_docs, "current_fingerprint", _fp)
    monkeypatch.setattr(fleet_docs, "current_member_fingerprints", _prints)
    return calls


async def test_a_missing_section_is_asked_once_more_on_its_own(no_database) -> None:
    # #10123 retired switch_off from DOC_SECTIONS: the missing section is a current one.
    first = {k: f"Text of {k}." for k in DOC_SECTIONS if k != "prerequisites"}
    model = _Model(
        json.dumps({"sections": first, "structured": {"purpose_line": "Runs the office."}}),
        json.dumps({"prerequisites": "OpenCTI connected."}),
    )
    draft = await gen.draft_fleet_documentation(object(), uuid.uuid4(), _fleet(), llm=model)
    assert len(model.messages) == 2
    assert "- prerequisites:" in model.messages[1][1].content and "- purpose:" not in model.messages[1][1].content
    assert draft.sections["prerequisites"] == "OpenCTI connected."
    assert not any(n["code"] == "sections_not_drafted" for n in draft.notes)


async def test_generator_notes_go_to_the_version_history_and_findings_to_managers(no_database) -> None:
    sections = {k: f"Text of {k}." for k in DOC_SECTIONS}
    answer = {
        "sections": sections,
        "structured": {"purpose_line": "Runs the office.", "unknown": 1},
        "findings": [{"kind": "missing_automation", "member": "Assistant", "finding": "No weekly report."}],
    }
    fleet = _fleet()
    await gen.regenerate_fleet_documentation(
        object(), uuid.uuid4(), fleet, base_version=0, llm=_Model(json.dumps(answer))
    )
    assert fleet.documentation["notes"], "the unknown part is a generator note"
    manager = await documentation_view(object(), fleet, can_edit=True)
    reader = await documentation_view(object(), fleet, can_edit=False)
    assert [f["finding"] for f in manager.findings] == ["No weekly report."]
    assert reader.findings == []
    assert documentation_history(fleet, with_notes=True)[0].notes == fleet.documentation["notes"]
    assert documentation_history(fleet, with_notes=False)[0].notes == []


async def test_a_one_section_regenerate_keeps_the_findings_and_dismissals(no_database) -> None:
    findings = clean_findings([{"finding": "One."}])
    stored = {
        "sections": {"purpose": "Old."},
        "version": 2,
        "findings": findings,
        "dismissed_findings": dismissal_record({}, findings[0], "f" * 16, by="u"),
    }
    fleet = _fleet(documentation=stored)
    await save_documentation(object(), fleet, {"purpose": "New."}, author=uuid.uuid4(), base_version=2)
    assert fleet.documentation["findings"] == findings
    assert fleet.documentation["dismissed_findings"] == stored["dismissed_findings"]
    assert (await documentation_view(object(), fleet, can_edit=True)).findings == []
    no_database["fp"] = "0" * 16
    assert len((await documentation_view(object(), fleet, can_edit=True)).findings) == 1


async def test_dismissing_an_unknown_finding_is_refused(no_database) -> None:
    from app.services.fleet_docs import dismiss_finding

    fleet = _fleet(documentation={"sections": {"purpose": "x"}, "version": 1, "findings": []})
    with pytest.raises(FleetActionRefusedError) as refused:
        await dismiss_finding(object(), fleet, "abc")
    assert refused.value.status_code == 404
    findings = clean_findings([{"finding": "One."}])
    fleet.documentation = {**fleet.documentation, "findings": findings}
    await dismiss_finding(object(), fleet, findings[0]["id"], by="u", reason="false positive")
    [record] = fleet.documentation["dismissed_findings"]
    assert (record["id"], record["member_print"], record["by"], record["reason"]) == (
        findings[0]["id"],
        "f" * 16,
        "u",
        "false positive",
    )
