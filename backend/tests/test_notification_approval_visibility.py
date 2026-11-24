"""A per-tool gate pause must be answerable from the notification list.

Phase 1 of `tasks-3289.md`, and mostly a verification: `Header.tsx` lists a
paused run only when `human_input_question` is truthy, while the per-tool gate's
distinguishing field is `pending_approvals`. If the gate ever paused without
supplying a question, every per-tool-gate approval would vanish from the header and
FR-015d's notification would be landing in a list that discards it.

It does supply one today. These tests are what stop that becoming untrue —
the frontend has no test framework at all (no vitest config, no test script), so
the invariant can only be held from this side.

Also covers FR-030: the reasons a stale approval cannot be answered.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api.v1.assignments import _refuse_not_awaiting
from app.tools.base import HumanInputRequestedError
from tests import app_sources

BACKEND = Path(__file__).resolve().parent.parent


class _Run:
    def __init__(self, status, question=None, proposals=None):
        self.status = status
        self.human_input_question = question
        self.pending_approvals = proposals


# --- T183: a gate pause is notification-worthy ----------------------------


def test_a_gate_pause_carries_both_a_question_and_its_proposals():
    err = HumanInputRequestedError(
        question="Approval required before running: delete_entity",
        tool_call_id="toolu_01",
        conversation=[],
        proposals=[{"tool_call_id": "toolu_01", "tool_name": "delete_entity"}],
    )
    # The question is what makes it visible; the proposals are what make it an
    # approval rather than a free-text question. The header needs the first and
    # the decision path needs the second, so neither may be dropped.
    assert err.question
    assert err.proposals


def test_every_proposal_raise_supplies_a_question():
    """The invariant behind T182, asserted rather than left to happen to work.

    Walks every ``raise HumanInputRequestedError(...)`` in the app: any call
    passing ``proposals`` must also pass a non-empty ``question``. A future
    edit that drops the question — as redundant, since the proposals say what
    is being asked — would silently un-list every approval in the header.
    """
    offenders = []
    for path in app_sources.python_files():
        for node in app_sources.walk(app_sources.tree(path)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name != "HumanInputRequestedError":
                continue
            kwargs = {k.arg: k.value for k in node.keywords}
            if "proposals" not in kwargs:
                continue
            question = kwargs.get("question")
            empty = question is None or (
                isinstance(question, ast.Constant) and not question.value
            )
            if empty:
                offenders.append(f"{path.relative_to(BACKEND)}:{node.lineno}")

    assert not offenders, (
        "these raise a gated pause with no question, so the run never appears "
        f"in the notification list: {offenders}"
    )


def test_the_sweep_actually_finds_the_gated_raise():
    """Guards the guard: if the walk stopped matching, the test above would
    pass by finding nothing — which is how this kind of check goes stale."""
    found = 0
    for path in app_sources.python_files():
        for node in app_sources.walk(app_sources.tree(path)):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "HumanInputRequestedError"
                and "proposals" in {k.arg for k in node.keywords}
            ):
                found += 1
    assert found >= 1, "no gated pause found — the sweep has been narrowed"


# --- T185: why a stale approval cannot be answered (FR-030) ---------------


@pytest.mark.parametrize("status", ["running", "completed", "failed"])
def test_a_resumed_run_refuses_with_409_and_says_why(status):
    with pytest.raises(HTTPException) as excinfo:
        _refuse_not_awaiting(_Run(status))
    assert excinfo.value.status_code == 409
    assert "no longer awaiting approval" in excinfo.value.detail
    assert status in excinfo.value.detail


@pytest.mark.parametrize("status", ["delayed", "skipped", "pending"])
def test_a_run_that_never_awaited_input_stays_a_400(status):
    """A client error, not a race — the distinction FR-030 asks for."""
    with pytest.raises(HTTPException) as excinfo:
        _refuse_not_awaiting(_Run(status))
    assert excinfo.value.status_code == 400


def test_the_refusal_never_claims_who_decided_or_when():
    """The resume path clears every pause field, so the run keeps no record of
    having been answered. Naming a decider or a time would be inventing it."""
    with pytest.raises(HTTPException) as excinfo:
        _refuse_not_awaiting(_Run("completed", question="q", proposals=[{"a": 1}]))
    detail = excinfo.value.detail.lower()
    assert "most likely" in detail, "the wording must stay hedged"
    for overclaim in ("answered by", "at 1", "user", "you"):
        assert overclaim not in detail


# --- T192/T193: the surface reuses one path and needs no second fetch -----


def test_the_listing_the_header_reads_carries_the_full_proposals():
    """FR-027's disclosure rests entirely on this.

    The notification list shows the complete proposed arguments without a
    second request, because `GET /assignments/runs/all` already returns them.
    Drop the field from the listing model and the dropdown silently has nothing
    to render — while the run page, which fetches per run, keeps working. That
    asymmetry is why this is asserted here rather than trusted.
    """
    from app.api.v1.schemas.assignment import AssignmentRunDetailResponse

    fields = AssignmentRunDetailResponse.model_fields
    assert "pending_approvals" in fields, (
        "the run listing no longer carries proposals — the notification list "
        "cannot disclose arguments it does not receive (FR-027)"
    )
    # The question is what lists the run at all; both halves are load-bearing.
    assert "human_input_question" in fields
    assert "status" in fields


def test_the_listing_endpoint_still_returns_that_model():
    """A lighter response model would be an easy, invisible optimisation."""
    import inspect

    from app.api.v1 import assignments

    src = inspect.getsource(assignments)
    assert 'response_model=list[AssignmentRunDetailResponse]' in src, (
        "the run listing changed shape; check the notification list still "
        "receives pending_approvals"
    )


def test_there_is_exactly_one_standing_approval_grant():
    """FR-038b: an exception granted from the notification list must be the
    same act as one granted on the run page.

    It is, because both post to the same endpoint — so the guarantee holds only
    for as long as nobody adds a second grant path for the new surface. That is
    the thing worth asserting; the grant mechanics themselves are covered in
    `test_tool_approval_preferences.py` and do not need repeating.
    """
    grants = []
    for path in app_sources.python_files():
        for node in app_sources.walk(app_sources.tree(path)):
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and (
                node.name == "_grant_standing_approvals"
            ):
                grants.append(f"{path.relative_to(BACKEND)}:{node.lineno}")

    assert len(grants) == 1, (
        f"expected one standing-approval grant, found {grants} — a second one "
        "means the two surfaces can drift (FR-029, FR-038b)"
    )


# --- T206/T208: discarding a pause is not the same as answering it --------


class _DismissRun:
    def __init__(self, proposals=None, question="Approval required"):
        self.human_input_question = question
        self.pending_approvals = proposals
        self.human_input_history = None


def _dismiss(run):
    from types import SimpleNamespace

    from app.api.v1.assignments import record_human_input_resolution

    user = SimpleNamespace(id="u1", full_name="Reviewer", email="r@example.com")
    return record_human_input_resolution(run, user, resolution="dismissed", response=None)


def test_discarding_a_pause_records_an_outcome_for_every_gated_call():
    """FR-020 wants an outcome for every call. A discarded run never resumes, so
    the engine never stamps one and no tool-call row exists — this trail is the
    only place the calls can be accounted for."""
    from app.tools.approval import OUTCOME_DISMISSED

    run = _DismissRun(
        proposals=[
            {"tool_call_id": "toolu_1", "tool_name": "generate_file"},
            {"tool_call_id": "toolu_2", "tool_name": "web_fetch"},
        ]
    )
    entry = _dismiss(run)

    assert [o["tool_call_id"] for o in entry["gate_outcomes"]] == ["toolu_1", "toolu_2"]
    assert {o["outcome"] for o in entry["gate_outcomes"]} == {OUTCOME_DISMISSED}
    assert [o["tool_name"] for o in entry["gate_outcomes"]] == ["generate_file", "web_fetch"]


def test_the_dismissed_outcome_is_neither_an_auto_run_nor_a_human_verdict():
    """It must not be readable as consent or as a decision. A discarded call was
    never run and never judged, which is a third thing."""
    from app.tools import approval

    assert approval.OUTCOME_DISMISSED not in (
        approval.OUTCOME_HITL_OFF,
        approval.OUTCOME_WHITELISTED,
        approval.OUTCOME_STANDING_EXCEPTION,
        approval.OUTCOME_APPROVALS_DISABLED,
        approval.OUTCOME_USER_ALLOWS_ALL,
    )
    assert not approval.OUTCOME_DISMISSED.startswith("auto_run")
    assert not approval.OUTCOME_DISMISSED.startswith("human_")


def test_a_free_text_pause_is_unchanged_by_this():
    """The classic `request_human_input` dismissal has no proposals and must not
    grow an empty key — every pre-existing consumer of this trail reads it."""
    assert "gate_outcomes" not in _dismiss(_DismissRun(proposals=None))
    assert "gate_outcomes" not in _dismiss(_DismissRun(proposals=[]))
