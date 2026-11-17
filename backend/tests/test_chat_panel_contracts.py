"""What the web app's chat panel names, held to what the backend serves.

The panel (``@filigran/chatbot``) and the web app's host of it read the
backend through a few shapes written down on the frontend side: the routes
(``components/chat-panel/endpoints.ts``), the answer markers and the stream
frames (``pages/chat/*.contract.json``), the upload limit
(``lib/constants.ts``). A route renamed, a frame type or end rule changed, a
limit moved on one side only breaks the chat with no error anywhere: these
read the frontend files and check them against the backend's own names.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = BACKEND_ROOT.parent / "frontend"
API_PREFIX = "/api/v1"


def _frontend(rel: str) -> str:
    return (FRONTEND_ROOT / rel).read_text(encoding="utf-8")


def _contract(name: str) -> dict:
    return json.loads(_frontend(f"src/pages/chat/{name}"))


# ── Routes ───────────────────────────────────────────────────────────────


def _chat_panel_endpoints() -> dict[str, str | None]:
    """``CHAT_PANEL_ENDPOINTS`` as written: ``key: "path"``, ``key: CONSTANT`` or ``key: null``."""
    source = _frontend("src/components/chat-panel/endpoints.ts")
    body = re.search(r"export const CHAT_PANEL_ENDPOINTS: ApiEndpoints = \{(.*?)\n\};", source, re.DOTALL)
    assert body, "endpoints.ts declares CHAT_PANEL_ENDPOINTS"
    constants = dict(re.findall(r'^const (\w+) = "([^"]*)";', source, re.MULTILINE))
    entries: dict[str, str | None] = {}
    for key, value in re.findall(r"^\s*(\w+):\s*([^,]+),", body.group(1), re.MULTILINE):
        value = value.strip()
        entries[key] = None if value == "null" else json.loads(value) if value.startswith('"') else constants[value]
    return entries


def test_every_route_the_chat_panel_names_is_served():
    """Each path is a route, or the base of the routes the panel builds on it
    (``{sessions}/{id}``, ``{events}/{id}/events``, ``{download}/{id}/download``)."""
    from app.main import application

    served = {getattr(route, "path", "") for route in application.routes}
    endpoints = _chat_panel_endpoints()
    named = {key: path for key, path in endpoints.items() if path}
    assert len(named) >= 15, endpoints
    assert {"messages", "events", "download", "tts"} <= named.keys()
    for key, path in named.items():
        full = API_PREFIX + path.split("?", 1)[0]
        assert any(p == full or p.startswith(full + "/") for p in served), f"{key}: {full} is not served"


# ── Answer markers ───────────────────────────────────────────────────────


def _marker_regexes() -> dict:
    """The backend's regex of each marker kind the contract samples beyond the
    deck, the report and the investigation (``test_presentations.py``,
    ``test_reports.py``, ``test_investigation_events.py``)."""
    from app.channels.base import _FILE_MARKER_RE, _VIDEO_MARKER_RE, _WORKITEM_MARKER_RE
    from app.services.chat_charts import CHART_MARKER_RE

    return {
        "FILE": _FILE_MARKER_RE,
        "VIDEO": _VIDEO_MARKER_RE,
        "WORK_ITEM": _WORKITEM_MARKER_RE,
        "CHART": CHART_MARKER_RE,
    }


def test_every_marker_kind_is_sampled_and_checked():
    kinds = {m["kind"] for m in _contract("answer-markers.contract.json")["markers"]}
    assert kinds == set(_marker_regexes()) | {"PRESENTATION", "REPORT", "INVESTIGATION"}


@pytest.mark.parametrize("kind", ["FILE", "VIDEO", "WORK_ITEM", "CHART"])
def test_the_marker_samples_match_the_backend_regexes(kind):
    pattern = _marker_regexes()[kind]
    samples = [m for m in _contract("answer-markers.contract.json")["markers"] if m["kind"] == kind]
    assert samples
    for sample in samples:
        match = pattern.fullmatch(sample["marker"])
        assert match, sample["marker"]
        assert match.group(1).lower() == sample["id"]
        assert sample["slideRef"] is None


def test_a_chart_marker_names_the_id_the_chart_tool_mints():
    from app.services.chat_charts import CHART_MARKER_RE, ChatChart

    chart = ChatChart(id="0123456789ab", block={})
    assert CHART_MARKER_RE.fullmatch(chart.marker)
    assert {"marker": chart.marker, "kind": "CHART", "id": chart.id, "slideRef": None} in _contract(
        "answer-markers.contract.json"
    )["markers"]


# ── Upload limit ─────────────────────────────────────────────────────────


def test_the_web_apps_upload_limit_is_the_stored_file_limit():
    """The panel's composer refuses what the upload route would (a total over
    the attached files; the route checks each file)."""
    from app.services.file_storage import MAX_UPLOAD_SIZE_BYTES

    match = re.search(r"^export const MAX_CHAT_UPLOAD_SIZE_MB = (\d+);", _frontend("src/lib/constants.ts"), re.MULTILINE)
    assert match
    assert int(match.group(1)) * 1024 * 1024 == MAX_UPLOAD_SIZE_BYTES


# ── Stream frames ────────────────────────────────────────────────────────


def _frame_samples() -> list[dict]:
    return _contract("stream-frames.contract.json")["samples"]


def _samples_of(frame_type: str) -> list[dict]:
    return [s["frame"] for s in _frame_samples() if s["frame"]["type"] == frame_type]


def _stream_only_types() -> set[str]:
    """The frames a send's own stream carries and no other (they name its own send, or its session)."""
    from app.core.sessions import SESSION_EXPIRED_CODE

    chat_source = (BACKEND_ROOT / "app" / "api" / "v1" / "chat.py").read_text(encoding="utf-8")
    written = {t for t in ("turn_started", "folded_into_active_turn") if f'"type": "{t}"' in chat_source}
    return written | {SESSION_EXPIRED_CODE}


def test_the_sampled_frames_are_the_backends_and_every_relayed_one_is_sampled():
    from app.services.chat_turn_stream import LIVE_ONLY_FRAME_TYPES, RELAYED_FRAME_TYPES

    relayed = RELAYED_FRAME_TYPES | LIVE_ONLY_FRAME_TYPES
    stream_only = _stream_only_types()
    assert stream_only == {"turn_started", "folded_into_active_turn", "session_expired"}
    sampled = {s["frame"]["type"] for s in _frame_samples()}
    assert sampled == relayed | stream_only
    for sample in _frame_samples():
        assert sample["relayed"] == (sample["frame"]["type"] in relayed), sample["name"]


def test_each_sampled_end_follows_the_backends_end_rule():
    """``provisional``: the turn's stored end follows (``is_provisional_end``);
    ``stored``: the end names the stored reply; ``null``: the frame ends no turn
    the panel reads a stored reply on."""
    from app.services.chat_turn_stream import is_provisional_end, is_terminal_frame

    ends = {"provisional": 0, "stored": 0}
    for sample in _frame_samples():
        frame, end = sample["frame"], sample["end"]
        assert is_provisional_end(frame) == (end == "provisional"), sample["name"]
        if end == "stored":
            assert is_terminal_frame(frame) and frame.get("message_id"), sample["name"]
        if end is None and is_terminal_frame(frame):
            assert frame.get("reason"), sample["name"]
        if end:
            ends[end] += 1
    assert all(ends.values()), ends
    assert {s["frame"]["type"] for s in _frame_samples() if s["end"] == "provisional"} == {"error", "cancelled"}
    assert any(f.get("intermediate") is True for f in _samples_of("done"))


def test_every_sampled_turn_frame_names_its_turn():
    """Every frame but those that belong to no turn carries ``turn_id``."""
    from app.core.sessions import SESSION_EXPIRED_CODE
    from app.services.chat_turn_stream import NON_TURN_FRAME_TYPES

    no_turn = NON_TURN_FRAME_TYPES | {SESSION_EXPIRED_CODE}
    for sample in _frame_samples():
        frame = sample["frame"]
        assert bool(frame.get("turn_id")) == (frame["type"] not in no_turn), sample["name"]


def _unnamed_samples_of(frame_type: str) -> list[dict]:
    """The samples of *frame_type* without the ``turn_id`` the stream names them with."""
    return [{k: v for k, v in f.items() if k != "turn_id"} for f in _samples_of(frame_type)]


def test_the_frames_the_backend_builds_are_sampled_as_built():
    from app.services.chat_turn_stream import (
        AGENTIC_ACTION_STATUS,
        TurnSnapshot,
        event_frame,
        interrupted_frame,
        lost_turn_frame,
        resume_frames,
    )

    errors = _unnamed_samples_of("error")
    assert interrupted_frame() in errors
    assert lost_turn_frame() in errors
    assert event_frame({"status": AGENTIC_ACTION_STATUS, "actions": 1, "period": "2026-10"}) in _unnamed_samples_of(
        "agentic_action"
    )
    replay_ack = resume_frames(TurnSnapshot(token="t", started_at=100.0), now=112.5)[0]
    assert replay_ack in _unnamed_samples_of("thinking")

    from app.services.cut_turn_resume import MAX_TOTAL_AUTO_RESUMES, CutTurn, resuming_frame

    cut = CutTurn(
        conversation_id="c",
        user_id="u",
        user_message_id=None,
        turn_id=None,
        cause="stop",
        resumes=0,
        cut_at="2026-10-22T10:00:00+00:00",
    )
    assert resuming_frame(cut) in _unnamed_samples_of("turn_resuming")
    assert MAX_TOTAL_AUTO_RESUMES == 100

    from app.services.parent_wake import wake_note, wake_start_frame

    wake = wake_start_frame(wake_note(task_id="0b6d1c1e-3a52-4f0e-9a51-2f7c1c0de001", agent_name="Threat Researcher"))
    assert wake in _unnamed_samples_of("thinking")


def test_the_sampled_fields_are_the_backends():
    from app.api.v1.chat import MessageResponse
    from app.core.sessions import REASON_IDLE, REASON_LIFETIME
    from app.services.builtin_tools.page_actions import PAGE_ACTION_REQUIRED_EVENT
    from app.services.mention_dispatch import ANSWERED_BY_KEY, MENTION_FAILED_KEY, MentionTarget
    from app.tools.approval import build_action_proposal
    from app.tools.tool_events import TOOL_PROGRESS_STATUS

    proposal_keys = set(build_action_proposal(tool_call_id="c", tool_name="t", arguments={}))
    for frame in _samples_of("approval_required"):
        assert all(set(p) == proposal_keys for p in frame["proposals"])

    attribution_keys = set(MentionTarget(agent_id="a", name="n", icon="i").answered_by() or {})
    stored_answers = [f for f in _samples_of("done") if f.get("intermediate") is not True]
    assert any(MENTION_FAILED_KEY in f for f in stored_answers)
    for frame in stored_answers:
        assert set(frame[ANSWERED_BY_KEY]) <= attribution_keys

    for frame in _samples_of("user_message"):
        assert set(frame["message"]) <= set(MessageResponse.model_fields)

    for frame in _samples_of("session_expired"):
        assert frame["reason"] in (REASON_IDLE, REASON_LIFETIME)

    statuses = {f["status"] for f in _samples_of("status")}
    loop_source = (BACKEND_ROOT / "app" / "tools" / "agent_loop.py").read_text(encoding="utf-8")
    for status in statuses - {TOOL_PROGRESS_STATUS}:
        assert f'"status": "{status}"' in loop_source, status
    assert TOOL_PROGRESS_STATUS in statuses
    assert {f.get("event") for f in _samples_of("status") if f["status"] == TOOL_PROGRESS_STATUS} == {
        PAGE_ACTION_REQUIRED_EVENT
    }
