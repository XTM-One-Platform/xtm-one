"""Backward compatibility of the HITL resume path.

Per-tool approval reworked how a paused run resumes. The risk this file guards
is narrow and real: a run paused by ``request_human_input`` **before** this
feature shipped has ``pending_approvals = NULL`` and ``approval_decisions =
NULL``, and it must still resume by injecting the user's free text. Getting that
wrong strands every in-flight paused run at upgrade time.

The resume logic lives inside ``execute_assignment_run``, a ~2,000-line
coroutine behind live DB, LLM and orchestration dependencies. This suite uses
the source-level pin pattern already established here for exactly that situation
(see ``test_tool_deny_list_runtime_enforcement.py``), plus behavioural tests of
the extracted helper.
"""

from __future__ import annotations

import inspect

import pytest

import app.services.assignment_engine as engine
from app.api.v1.schemas import assignment as schemas
from app.api.v1.schemas.assignment import RespondToHumanInputRequest


def _resume_source() -> str:
    return inspect.getsource(engine.execute_assignment_run)


def test_resume_still_triggers_on_a_free_text_response():
    """The entry condition must accept a run with only ``human_input_response``."""
    source = _resume_source()

    assert 'original_status == "awaiting_human"' in source
    assert "run.human_input_response or run.approval_decisions" in source, (
        "the resume condition no longer admits a free-text-only pause — runs "
        "paused before per-tool approval shipped would never resume"
    )


def test_free_text_injection_is_the_fallback_branch():
    """Structured decisions are the special case; free text is the default.

    Written this way round deliberately: if the branch is ever inverted so that
    free text becomes conditional, pre-existing paused runs break silently.
    """
    source = _resume_source()

    decisions_at = source.find("if run.approval_decisions:")
    # Anchor on the injection itself, not on the tool's registration earlier in
    # this same function, which also mentions request_human_input.
    free_text_at = source.find("content=run_answer_text(")

    assert decisions_at != -1, "structured-decision branch missing"
    assert free_text_at != -1, "free-text injection missing"
    assert decisions_at < free_text_at, (
        "free-text injection should sit in the else branch, after the "
        "structured-decision check"
    )


def test_free_text_path_still_matches_by_tool_name():
    """The legacy matcher stays for the legacy shape.

    Matching by tool name is wrong for gated calls — a turn can call one tool
    several times — which is why the new path keys on ``tool_call_id``. But the
    free-text pause only ever has a single ``request_human_input`` call, so the
    old matcher remains correct there and must not be "fixed" away.
    """
    source = _resume_source()

    assert 'tc.get("name") == "request_human_input"' in source


def test_pause_leaves_pending_approvals_none_for_a_free_text_pause():
    """``proposals`` is absent on a classic pause, so the flag stays NULL."""
    source = _resume_source()

    assert 'run.pending_approvals = getattr(hitl, "proposals", None)' in source


def test_respond_schema_still_accepts_a_bare_response():
    """The original request body must remain valid, unchanged."""
    body = RespondToHumanInputRequest(response="looks good")

    assert body.response == "looks good"
    assert body.decisions is None


def test_respond_schema_accepts_decisions_without_a_response():
    body = RespondToHumanInputRequest(
        decisions=[{"tool_call_id": "c1", "decision": "approve"}]
    )

    assert body.response is None
    assert body.decisions[0].tool_call_id == "c1"


@pytest.mark.parametrize(
    "payload",
    [{}, {"other": 1}, {"response": None}, {"response": 3}, {"decisions": None}, [1]],
)
def test_an_answer_without_a_response_is_refused_as_it_always_was(payload):
    """A body that is not a free-text answer and carries no decisions gets the
    422 the original ``response: str`` model gave, body for body."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pydantic import BaseModel

    def _app(model: type[BaseModel]) -> TestClient:
        app = FastAPI()

        @app.post("/respond")
        def respond(body: model) -> dict:  # type: ignore[valid-type]
            return {}

        return TestClient(app)

    class RespondToHumanInputRequest(BaseModel):  # the earlier model
        response: str

    before = _app(RespondToHumanInputRequest).post("/respond", json=payload)
    now = _app(schemas.RespondToHumanInputRequest).post("/respond", json=payload)

    assert before.status_code == 422
    assert (now.status_code, now.json()) == (before.status_code, before.json())


def test_human_input_requested_error_keeps_its_original_signature():
    """Existing raisers construct it positionally with three arguments."""
    from app.tools.base import HumanInputRequestedError

    error = HumanInputRequestedError("question?", "tc-1", [])

    assert error.question == "question?"
    assert error.tool_call_id == "tc-1"
    assert error.conversation == []
    assert error.proposals is None, (
        "proposals must default to None so a classic pause is distinguishable "
        "from a gated one"
    )
