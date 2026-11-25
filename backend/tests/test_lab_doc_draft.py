"""The lab's stub model answers the fleet documentation drafter (#9492).

lab.py is standard library only and starts the stores when imported, so its functions are compiled
one by one from its syntax tree (as ``test_lab_stream_script.py`` does). The request is built with the
backend's own ``build_messages`` and the answer parsed with its own ``extract_sections``: a change of
the drafter's prompt that the stub no longer recognises fails here, not in a lab.
dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.fleet_doc_generation import FleetDocDraftError, build_messages, extract_sections
from app.services.fleet_docs import DOC_SECTIONS

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"

_NAMES = (
    "doc_draft_answer",
    "doc_draft_member_names",
    "doc_draft_steps",
    "last_user_text",
    "LAB_DOC_DRAFT_SYSTEM",
    "LAB_DOC_DRAFT_REFUSE",
    "LAB_DOC_DRAFT_HOLD",
    "LAB_DOC_DRAFT_HOLD_DEFAULT_SECONDS",
    "LAB_DOC_DRAFT_HOLD_MAX_SECONDS",
    "doc_draft_hold_seconds",
    "doc_draft_findings",
    "LAB_DOC_DRAFT_FINDINGS",
)


def _lab_namespace(slept: list[float] | None = None) -> dict:
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    body = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in _NAMES)
        or (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in _NAMES for t in node.targets))
    ]
    # A held draft sleeps: the slept seconds are recorded, never waited (#10859).
    clock = SimpleNamespace(sleep=(slept if slept is not None else []).append)
    namespace: dict = {"json": json, "re": re, "time": clock, "log": lambda _line: None}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(LAB), "exec"), namespace)  # noqa: S102 - the repo's own file
    return namespace


def _request(
    wanted: list[str], description: str = "Seeded by the lab.", current: dict | None = None, members: list | None = None
) -> dict:
    context = {
        "fleet": {"name": "Lab sample fleet", "description": description, "skills": []},
        "members": members or [],
    }
    messages = build_messages(context, current or {}, wanted)
    return {"model": "lab-model", "messages": [{"role": m.role, "content": m.content} for m in messages]}


def test_a_whole_draft_answers_every_section_the_parser_keeps():
    answer = _lab_namespace()["doc_draft_answer"](_request(list(DOC_SECTIONS)), 1)
    draft = extract_sections(answer, list(DOC_SECTIONS))
    assert list(draft.sections) == list(DOC_SECTIONS)
    assert draft.notes == []
    assert "Lab sample fleet" in draft.sections["purpose"]


def test_a_regenerate_answers_the_asked_section_alone_with_a_new_text():
    answer = _lab_namespace()["doc_draft_answer"]
    current = {"purpose": "Old purpose.", "flow": "Old flow."}
    first = extract_sections(answer(_request(["flow"], current=current), 2), ["flow"])
    second = extract_sections(answer(_request(["flow"], current=current), 3), ["flow"])
    assert list(first.sections) == ["flow"] and first.notes == []
    assert first.sections["flow"] != second.sections["flow"]


def test_a_whole_draft_with_structured_parts_answers_parts_the_generator_keeps():
    from app.services.fleet_doc_generation import STRUCTURED_GUIDE, build_structured

    request = _request(list(DOC_SECTIONS))
    request["messages"][-1]["content"] += f"\n\n{STRUCTURED_GUIDE}"
    draft = extract_sections(_lab_namespace()["doc_draft_answer"](request, 5), list(DOC_SECTIONS))
    assert list(draft.sections) == list(DOC_SECTIONS)
    notes: list[str] = []
    parts = build_structured(draft.structured, {}, notes)
    assert parts and parts["steps"] and parts["gates"] and parts["objects"]
    assert notes == []


_MEMBERS = [
    {"name": "Lab Analyst", "orchestrator": False},
    {"name": "Lab Orchestrator", "orchestrator": True},
    {"name": "Lab Reviewer", "orchestrator": False},
]


def test_the_structured_steps_follow_the_guide_with_markdown_bodies_from_the_members():
    """#10327: more than five steps (the About fold), each a markdown body of 25 to 40 words."""
    from app.services.fleet_doc_generation import STRUCTURED_GUIDE, build_structured

    request = _request(list(DOC_SECTIONS), members=_MEMBERS)
    request["messages"][-1]["content"] += f"\n\n{STRUCTURED_GUIDE}"
    draft = extract_sections(_lab_namespace()["doc_draft_answer"](request, 6), list(DOC_SECTIONS))
    notes: list[str] = []
    steps = build_structured(draft.structured, {}, notes)["steps"]
    assert notes == []
    assert len(steps) == 7
    for step in steps:
        detail = step["detail"]
        assert 25 <= len(detail.split()) <= 40, (step["title"], len(detail.split()))
        assert "**" in detail
        assert len(step["title"].split()) <= 8
    assert sum(1 for s in steps if s.get("gate")) == 1
    assert sum(1 for s in steps if "\n- " in s["detail"]) == 1
    joined = " ".join(s["detail"] for s in steps)
    assert steps[0]["detail"].startswith("**Lab Orchestrator**")
    assert all(f"**{m['name']}**" in joined for m in _MEMBERS)
    assert "lab draft 6" in steps[-1]["detail"]


def test_the_steps_fall_back_to_generic_names_without_members():
    names = _lab_namespace()["doc_draft_member_names"]
    assert names({"members": []})[0] == "the orchestrator"
    assert names(None) == names({})
    steps = _lab_namespace()["doc_draft_steps"](["Solo"], 1)
    assert len(steps) == 7 and all("**Solo**" in s["detail"] for s in steps)


def test_the_refuse_marker_keeps_the_refusal():
    answer = _lab_namespace()["doc_draft_answer"](_request(list(DOC_SECTIONS), description="x lab:draft-refuse"), 4)
    with pytest.raises(FleetDocDraftError, match="answered no documentation"):
        extract_sections(answer, list(DOC_SECTIONS))


@pytest.mark.parametrize(
    ("description", "seconds"),
    [
        ("Seeded by the lab.", 0),
        ("x (lab:draft-hold)", 60),
        ("x (lab:draft-hold 15).", 15),
        ("x lab:draft-hold 600", 110),
        ("x lab:draft-hold 0", 1),
    ],
)
def test_the_hold_marker_reads_its_seconds_with_a_default_and_a_ceiling(description, seconds):
    assert _lab_namespace()["doc_draft_hold_seconds"](description) == seconds


def test_a_held_draft_sleeps_the_hold_then_answers_a_draft_the_parser_keeps():
    slept: list[float] = []
    request = _request(list(DOC_SECTIONS), description="x (lab:draft-hold 15)")
    answer = _lab_namespace(slept)["doc_draft_answer"](request, 5)
    assert slept == [15]
    assert list(extract_sections(answer, list(DOC_SECTIONS)).sections) == list(DOC_SECTIONS)


def test_a_draft_without_the_hold_never_sleeps_and_the_refusal_wins_over_the_hold():
    slept: list[float] = []
    _lab_namespace(slept)["doc_draft_answer"](_request(list(DOC_SECTIONS)), 6)
    refused = _lab_namespace(slept)["doc_draft_answer"](
        _request(list(DOC_SECTIONS), description="x lab:draft-refuse lab:draft-hold"), 7
    )
    assert slept == []
    with pytest.raises(FleetDocDraftError, match="answered no documentation"):
        extract_sections(refused, list(DOC_SECTIONS))


def _whole_draft(description: str):
    from app.services.fleet_doc_generation import STRUCTURED_GUIDE

    request = _request(list(DOC_SECTIONS), description=description, members=_MEMBERS)
    request["messages"][-1]["content"] += f"\n\n{STRUCTURED_GUIDE}"
    return extract_sections(_lab_namespace()["doc_draft_answer"](request, 8), list(DOC_SECTIONS))


def test_the_findings_marker_gives_a_whole_draft_findings_the_parser_keeps():
    """A Regenerate in the lab keeps the Suggestions chip: the parser keeps three findings of three kinds."""
    draft = _whole_draft("x (lab:draft-findings).")
    assert [f["kind"] for f in draft.findings] == [
        "missing_automation",
        "no_gate_on_sensitive_action",
        "description_mismatch",
    ]
    assert draft.findings[0]["member"] == "Lab Orchestrator"
    assert all(f["id"] and f["suggested_fix"] for f in draft.findings)


def test_the_corporate_fleet_carries_the_findings_marker():
    marker = _lab_namespace()["LAB_DOC_DRAFT_FINDINGS"]
    assert f"nothing in it can start a run ({marker})." in LAB.read_text(encoding="utf-8")


def test_a_whole_draft_without_the_findings_marker_raises_none():
    assert _whole_draft("Seeded by the lab.").findings == []


def test_any_other_request_is_not_a_draft():
    answer = _lab_namespace()["doc_draft_answer"]
    assert answer({"messages": [{"role": "user", "content": "Sections to write:\n- purpose: x"}]}, 1) is None
    chat = [{"role": "system", "content": "You are a helpful assistant."}, {"role": "user", "content": "hello"}]
    assert answer({"messages": chat}, 1) is None
    assert answer({}, 1) is None
