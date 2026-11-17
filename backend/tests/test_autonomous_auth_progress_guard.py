"""WS2: auth-storm progress guard + deterministic delegation close-out.

A long autonomous run outlives its build-time platform JWT (10 min TTL). When
the transparent re-mint (WS2 part 1) ALSO fails, every OpenAEV call 401s and the
run can neither progress nor settle itself. These tests pin the deterministic
signals that let the adapter stop such a run PROMPTLY and keep the cockpit from
freezing:

- :func:`cycle_made_progress` must NOT credit an auth-failed / error-shaped
  result as progress (an author call that 401'd changed no state), so an
  auth-only cycle registers as no-progress;
- :func:`cycle_auth_failed` trips only when a cycle is dominated by auth
  failures (>= the trip minimum), never on a couple of unrelated errors;
- :func:`_close_dangling_delegation` posts a synthetic AGENT_DELEGATION
  ``phase:"result"`` ONLY when the cycle left a delegation ``start`` unpaired,
  so a consult that ended (or threw) does not hang the live "waiting for X".
"""

from __future__ import annotations

import json

import pytest

from app.services import autonomous_pentest as ap
from app.tools.base import ToolCall


class _ToolResult:
    """A tool-result message (role='tool') carrying a provider return string."""

    def __init__(self, name: str, content: str) -> None:
        self.role = "tool"
        self.name = name
        self.content = content
        self.tool_calls = None


class _AssistantMsg:
    """An assistant message carrying tool CALLS (where delegation phase lives)."""

    def __init__(self, tool_calls: list[ToolCall]) -> None:
        self.role = "assistant"
        self.content = ""
        self.tool_calls = tool_calls


_AUTH_401 = "OpenAEV API error: 401 Unauthorized (the token may be expired)."


# ── progress must exclude auth / error-shaped results ─────────────────────────


def test_author_step_success_counts_as_progress():
    msg = _ToolResult("author_openaev_attack_path_step", "Authored attack-path step s1.")
    assert ap._message_made_progress(msg) is True
    assert ap.cycle_made_progress([msg]) is True


def test_author_step_auth_error_is_not_progress():
    # The exact run-killer: an author call that 401'd authored nothing, so it
    # must NOT reset the convergence guard's stall streak.
    msg = _ToolResult("author_openaev_attack_path_step", _AUTH_401)
    assert ap._message_made_progress(msg) is False
    assert ap.cycle_made_progress([msg]) is False


def test_status_update_error_prefix_is_not_progress():
    msg = _ToolResult("update_openaev_run_status", "Error: could not reach OpenAEV.")
    assert ap._message_made_progress(msg) is False


def test_arsenal_call_is_never_attack_path_progress():
    # WS4-C steps-first: arsenal is NOT a progress tool at all - neither a mutation
    # nor a read advances the attack path, so an arsenal-only cycle must feed the
    # no-progress guard (the "manufacturing arsenal, not orchestrating a path"
    # stall). Only authoring/executing a step counts.
    created = _ToolResult(
        "manage_openaev_threat_arsenal", "Created threat arsenal action abc."
    )
    read = _ToolResult(
        "manage_openaev_threat_arsenal", "Found 3 threat arsenal actions matching."
    )
    errored = _ToolResult("manage_openaev_threat_arsenal", _AUTH_401)
    assert ap._message_made_progress(created) is False
    assert ap._message_made_progress(read) is False
    assert ap._message_made_progress(errored) is False


def test_non_progress_tool_never_counts():
    assert ap._message_made_progress(_ToolResult("search_openaev_findings", "ok")) is False


# ── cycle_auth_failed: dominated-by-auth detection ───────────────────────────


def test_cycle_auth_failed_trips_at_the_minimum():
    msgs = [
        _ToolResult("author_openaev_attack_path_step", _AUTH_401),
        _ToolResult("record_openaev_event", _AUTH_401),
        _ToolResult("update_openaev_run_status", _AUTH_401),
    ]
    assert len(msgs) >= ap._AUTH_FAILURE_TRIP_MIN
    assert ap.cycle_auth_failed(msgs) is True


def test_cycle_auth_failed_ignores_a_couple_of_failures():
    msgs = [
        _ToolResult("author_openaev_attack_path_step", _AUTH_401),
        _ToolResult("author_openaev_attack_path_step", "Authored attack-path step s1."),
    ]
    assert ap.cycle_auth_failed(msgs) is False


def test_cycle_auth_failed_ignores_non_auth_errors():
    # A validation error (400) or a rate limit (429) is not an auth storm; only
    # true auth/permission failures should trip the immediate dead-letter.
    msgs = [
        _ToolResult("author_openaev_attack_path_step", "Error: 400 Bad Request."),
        _ToolResult("author_openaev_attack_path_step", "Error: 429 rate limit."),
        _ToolResult("author_openaev_attack_path_step", "Error: 400 Bad Request."),
    ]
    assert ap.cycle_auth_failed(msgs) is False


def test_cycle_auth_failed_requires_auth_domination_not_just_a_count():
    # A handful of RBAC-masked 403s among WORKING OpenAEV reads is not a token
    # storm: the healthy read proves the token still authenticates, so the
    # immediate non-retryable dead-letter must NOT fire (the ordinary
    # forward-progress guard handles a genuinely stuck run instead).
    msgs = [
        _ToolResult("search_openaev_findings", "Found 4 findings for the run."),
        _ToolResult("author_openaev_attack_path_step", _AUTH_401),
        _ToolResult("record_openaev_event", _AUTH_401),
        _ToolResult("update_openaev_run_status", _AUTH_401),
    ]
    assert len(msgs) - 1 >= ap._AUTH_FAILURE_TRIP_MIN
    assert ap.cycle_auth_failed(msgs) is False


def test_cycle_auth_failed_ignores_a_successful_non_openaev_tool():
    # Only a healthy OPENAEV result disproves an OpenAEV auth storm - a
    # successful non-OpenAEV tool (memory search, code interpreter) says nothing
    # about OpenAEV auth, so the storm still trips when every OpenAEV call 401s.
    msgs = [
        _ToolResult("search_memory", "2 memories recalled."),
        _ToolResult("author_openaev_attack_path_step", _AUTH_401),
        _ToolResult("record_openaev_event", _AUTH_401),
        _ToolResult("update_openaev_run_status", _AUTH_401),
    ]
    assert ap.cycle_auth_failed(msgs) is True


# ── deterministic delegation phase:"result" close-out ────────────────────────


def _delegation_call(phase: str, agent: str = "Payload Creator") -> _AssistantMsg:
    return _AssistantMsg(
        [
            ToolCall(
                id="c1",
                name="record_openaev_event",
                arguments={
                    "run_id": "r1",
                    "type": "AGENT_DELEGATION",
                    "data": json.dumps({"phase": phase, "agent_name": agent}),
                },
            )
        ]
    )


def test_last_delegation_phase_reads_from_call_args():
    assert ap._last_delegation_phase([_delegation_call("start")]) == (
        "start",
        "Payload Creator",
    )
    assert ap._last_delegation_phase([]) is None
    # A non-delegation event is ignored.
    other = _AssistantMsg(
        [
            ToolCall(
                id="c2",
                name="record_openaev_event",
                arguments={"run_id": "r1", "type": "NARRATION"},
            )
        ]
    )
    assert ap._last_delegation_phase([other]) is None


@pytest.mark.asyncio
async def test_close_dangling_delegation_posts_result_when_unpaired(monkeypatch):
    posted: list[dict] = []

    async def _capture(payload: dict, body: dict) -> None:
        posted.append(body)

    monkeypatch.setattr(ap, "_post_autonomous_event", _capture)

    payload = {"openaev_run_id": "r1", "callback_base_url": "https://x", "user_id": "u1"}
    await ap._close_dangling_delegation(payload, [_delegation_call("start")])

    assert len(posted) == 1
    body = posted[0]
    assert body["type"] == "AGENT_DELEGATION"
    assert json.loads(body["data"]) == {
        "phase": "result",
        "agent_name": "Payload Creator",
        "status": "closed",
    }


@pytest.mark.asyncio
async def test_close_dangling_delegation_noop_when_already_paired(monkeypatch):
    posted: list[dict] = []

    async def _capture(payload: dict, body: dict) -> None:
        posted.append(body)

    monkeypatch.setattr(ap, "_post_autonomous_event", _capture)

    payload = {"openaev_run_id": "r1", "callback_base_url": "https://x", "user_id": "u1"}
    # start THEN result -> nothing dangling.
    await ap._close_dangling_delegation(
        payload, [_delegation_call("start"), _delegation_call("result")]
    )
    # no delegation at all -> nothing to close.
    await ap._close_dangling_delegation(payload, [])

    assert posted == []
