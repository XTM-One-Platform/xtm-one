"""The user's likely next message, proposed by the routing model once a turn is done.

``services/chat_reply_suggestions.py``: a side call after the ``done`` frame,
on a digest of the turn (never a tool's raw output), that may stay silent; the
suggestion travels as a live-only ``reply_suggestion`` frame and only fills the
composer. ``dev-docs/reply-suggestions-discovery.md`` holds the design.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import chat_reply_suggestions as rs
from app.services import chat_turn_stream

BACKEND_ROOT = Path(__file__).resolve().parents[1]


# ── parse_suggestion ─────────────────────────────────────────────────────


def _answer(suggestion: object) -> str:
    return json.dumps({"suggestion": suggestion})


def test_a_one_line_suggestion_is_kept_cleaned():
    assert rs.parse_suggestion(_answer('  "Enrich   the three indicators" ')) == "Enrich the three indicators"


def test_a_fenced_object_is_read():
    raw = "```json\n" + _answer("Show me the related reports") + "\n```"
    assert rs.parse_suggestion(raw) == "Show me the related reports"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "Enrich the indicators",  # no object
        json.dumps(["Enrich the indicators"]),
        _answer(""),  # the model's silence
        _answer("   "),
        _answer(42),  # never type-coerced
        _answer({"text": "Enrich"}),
        _answer("Enrich the indicators.\nThen build the report."),  # two lines
        _answer("word " * (rs.MAX_SUGGESTION_WORDS + 1)),
        _answer("x" * (rs.MAX_SUGGESTION_CHARS + 1)),
    ],
)
def test_anything_but_one_short_line_is_no_suggestion(raw):
    assert rs.parse_suggestion(raw) is None


def test_repeating_the_users_last_message_is_no_suggestion():
    assert rs.parse_suggestion(_answer("List my  alerts"), user_text="list my alerts") is None


# ── the digest ───────────────────────────────────────────────────────────


def test_tools_come_from_the_trace_with_their_outcome():
    meta = {
        "tool_names": ["search", "enrich"],
        "tool_call_trace": [
            {"name": "search", "success": True},
            {"name": "enrich", "success": False},
            {"name": "enrich"},
        ],
    }
    assert rs.turn_tools(meta) == [("search", True), ("enrich", False), ("enrich", True)]


def test_tools_fall_back_to_their_names():
    assert rs.turn_tools({"tool_names": ["search"]}) == [("search", True)]
    assert rs.turn_tools({}) == []
    assert rs.turn_tools(None) == []


def test_the_request_holds_the_turn_as_data_and_no_tool_output():
    digest = rs.TurnDigest(
        user_text="Enrich this IP",
        assistant_text="The IP is a known C2 server.",
        tools=[("enrich", True), ("enrich", False), ("search", True)],
        agent_name="CTI Analyst",
        agent_description="Investigates threats",
    )
    system, user = rs.build_messages(digest)
    assert system.role == "system" and "JSON" in system.content
    assert "<agent>\nName: CTI Analyst\nDescription: Investigates threats\n</agent>" in user.content
    assert "<user_message>\nEnrich this IP\n</user_message>" in user.content
    assert "- enrich (2 calls, 1 failed)" in user.content
    assert "- search (1 call)" in user.content
    assert "<agent_answer>\nThe IP is a known C2 server.\n</agent_answer>" in user.content


def test_a_long_answer_keeps_its_head_and_its_tail():
    answer = "H" * 4000 + "TAIL: shall I build the report?"
    _system, user = rs.build_messages(rs.TurnDigest(user_text="go", assistant_text=answer))
    assert "TAIL: shall I build the report?" in user.content
    assert "[...]" in user.content
    assert len(user.content) < len(answer)


# ── generation ───────────────────────────────────────────────────────────


class _LLM:
    def __init__(self, content: str | None = None, *, fail: bool = False) -> None:
        self.content = content
        self.fail = fail
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("provider down")
        return SimpleNamespace(content=self.content, stop_reason="end_turn", usage={}, model="routing-mini")


_DIGEST = rs.TurnDigest(user_text="List my alerts", assistant_text="You have 3 critical alerts.")


async def test_the_routing_model_is_asked_without_thinking():
    llm = _LLM(_answer("Triage the critical ones"))

    assert await rs.generate_reply_suggestion(llm, "routing-mini", _DIGEST) == "Triage the critical ones"

    (call,) = llm.calls
    assert call["model"] == "routing-mini"
    assert call["disable_thinking"] is True


async def test_a_failed_call_is_silence_and_never_raises():
    assert await rs.generate_reply_suggestion(_LLM(fail=True), "m", _DIGEST) is None


async def test_no_answer_asks_nothing():
    llm = _LLM(_answer("anything"))
    assert await rs.generate_reply_suggestion(llm, "m", rs.TurnDigest(user_text="hi", assistant_text="  ")) is None
    assert llm.calls == []


# ── who gets one ─────────────────────────────────────────────────────────


class _DB:
    def __init__(self, user) -> None:
        self.user = user

    async def get(self, _model, _id):
        return self.user


def _settings(monkeypatch, *, enabled: bool = True, model: str | None = "routing-mini", provider_id=None):
    async def _ps(_db):
        return SimpleNamespace(reply_suggestions_enabled=enabled)

    async def _routing(_db):
        return model, provider_id

    resolved: list[dict] = []

    async def _llm(_db, user_id, **kwargs):
        resolved.append({"user_id": user_id, **kwargs})
        return object(), kwargs["model_override"]

    monkeypatch.setattr("app.api.v1.platform_settings_helpers.get_platform_settings", _ps)
    monkeypatch.setattr("app.services.flow_llm.routing_model", _routing)
    monkeypatch.setattr("app.llm.router.get_llm_for_user", _llm)
    return resolved


async def test_the_routing_model_of_the_user_serves_the_suggestion(monkeypatch):
    provider_id = uuid.uuid4()
    resolved = _settings(monkeypatch, provider_id=provider_id)
    user_id = uuid.uuid4()

    llm_model = await rs.resolve_suggestion_llm(_DB(SimpleNamespace(reply_suggestions_enabled=True)), user_id)

    assert llm_model is not None and llm_model[1] == "routing-mini"
    assert resolved == [{"user_id": user_id, "model_override": "routing-mini", "provider_id_override": provider_id}]


@pytest.mark.parametrize(
    ("enabled", "model", "user_on"),
    [
        (False, "routing-mini", True),  # the administrator's switch
        (True, None, True),  # no routing model: nothing is generated
        (True, "routing-mini", False),  # the user's preference
    ],
)
async def test_no_suggestion_is_due(monkeypatch, enabled, model, user_on):
    resolved = _settings(monkeypatch, enabled=enabled, model=model)
    db = _DB(SimpleNamespace(reply_suggestions_enabled=user_on))

    assert await rs.resolve_suggestion_llm(db, uuid.uuid4()) is None
    assert resolved == []


async def test_a_routing_model_the_user_may_not_use_is_silence(monkeypatch):
    from app.llm.provider_access import ProviderNotUsableError

    _settings(monkeypatch)

    async def _refused(*_a, **_k):
        raise ProviderNotUsableError("reserved")

    monkeypatch.setattr("app.llm.router.get_llm_for_user", _refused)
    db = _DB(SimpleNamespace(reply_suggestions_enabled=True))
    assert await rs.resolve_suggestion_llm(db, uuid.uuid4()) is None


# ── after done ───────────────────────────────────────────────────────────


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _turn(**overrides) -> dict:
    turn = {
        "conversation_id": uuid.uuid4(),
        "message_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "agent_id": None,
        "user_text": "List my alerts",
        "assistant_text": "You have 3 critical alerts.",
        "turn_meta": {"tool_call_trace": [{"name": "list_alerts", "success": True}]},
    }
    turn.update(overrides)
    return turn


def _after_done(monkeypatch, *, suggestion: str | None, latest: bool = True, resolved: bool = True) -> list:
    digests: list = []
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session())

    async def _resolve(_db, _user_id):
        return (object(), "routing-mini") if resolved else None

    async def _agent(_db, _agent_id):
        return "CTI Analyst", None

    async def _generate(_llm, _model, digest):
        digests.append(digest)
        return suggestion

    async def _latest(_db, _cid, _mid):
        return latest

    monkeypatch.setattr(rs, "resolve_suggestion_llm", _resolve)
    monkeypatch.setattr(rs, "_agent_digest", _agent)
    monkeypatch.setattr(rs, "generate_reply_suggestion", _generate)
    monkeypatch.setattr(rs, "is_latest_message", _latest)
    return digests


@pytest.fixture
def sent():
    frames: list[dict] = []

    async def _send(frame: dict) -> bool:
        frames.append(frame)
        return True

    _send.frames = frames  # type: ignore[attr-defined]
    return _send


@pytest.fixture
def published(monkeypatch) -> list[tuple[str, dict, str]]:
    frames: list[tuple[str, dict, str]] = []
    monkeypatch.setattr(
        chat_turn_stream,
        "publish_live",
        lambda conversation_id, frame, *, origin: frames.append((conversation_id, frame, origin)),
    )
    return frames


async def test_the_suggestion_reaches_this_stream_and_the_owners_others(monkeypatch, sent, published):
    digests = _after_done(monkeypatch, suggestion="Triage the critical ones")
    turn = _turn()

    await rs.send_suggestion_after_done(send=sent, origin="stream-1", **turn)

    frame = {
        "type": "reply_suggestion",
        "conversation_id": str(turn["conversation_id"]),
        "message_id": str(turn["message_id"]),
        "text": "Triage the critical ones",
    }
    assert sent.frames == [frame]
    assert published == [(str(turn["conversation_id"]), frame, "stream-1")]
    (digest,) = digests
    assert digest.tools == [("list_alerts", True)]
    assert digest.agent_name == "CTI Analyst"


@pytest.mark.parametrize(
    "case",
    [
        {"suggestion": None},  # the model stayed silent
        {"suggestion": "Triage", "latest": False},  # the user sent something meanwhile
        {"suggestion": "Triage", "resolved": False},  # switched off, or no routing model
    ],
)
async def test_nothing_is_sent(monkeypatch, sent, published, case):
    _after_done(monkeypatch, **case)

    await rs.send_suggestion_after_done(send=sent, origin="stream-1", **_turn())

    assert sent.frames == []
    assert published == []


async def test_a_turn_without_an_answer_asks_nothing(monkeypatch, sent, published):
    digests = _after_done(monkeypatch, suggestion="Triage")

    await rs.send_suggestion_after_done(send=sent, origin="stream-1", **_turn(assistant_text=" "))

    assert digests == []
    assert sent.frames == []


# ── wiring ───────────────────────────────────────────────────────────────


def test_the_frame_is_live_only():
    assert rs.REPLY_SUGGESTION_FRAME in chat_turn_stream.LIVE_ONLY_FRAME_TYPES
    assert rs.REPLY_SUGGESTION_FRAME not in chat_turn_stream.RELAYED_FRAME_TYPES


def test_both_chat_streams_propose_one_after_done_for_a_client_that_offers_it():
    """The web chat runs it after ``done`` like the title and holds the stream
    for it; the embedded chat's stream yields it after its final ``done``.
    Both only for a send that set ``supports_reply_suggestions``: a script or a
    one-shot generation call reading to the end of the stream never waits on
    one, nor pays for it."""
    chat_source = (BACKEND_ROOT / "app" / "api" / "v1" / "chat.py").read_text(encoding="utf-8")
    assert "send_suggestion_after_done(" in chat_source
    assert "_send_title_task, _send_suggestion_task" in chat_source
    assert 'bool(data.get("supports_reply_suggestions"))' in chat_source
    assert "if _turn_supports_reply_suggestions and not _turn_stopped" in chat_source
    platform_source = (BACKEND_ROOT / "app" / "api" / "v1" / "platform.py").read_text(encoding="utf-8")
    assert "suggest_after_turn(**_pf_suggestion_turn)" in platform_source
    assert "if body.supports_reply_suggestions and _pf_answered" in platform_source


def test_a_send_offers_no_suggestion_unless_it_says_so():
    from app.api.v1.chat_web_stream import WebChatSendRequest
    from app.api.v1.platform import PlatformChatRequest

    web = WebChatSendRequest(content="hi", conversation_id="c1")
    assert web.supports_reply_suggestions is False
    assert "supports_reply_suggestions" in WebChatSendRequest(
        content="hi", conversation_id="c1", supports_reply_suggestions=True
    ).frame()
    assert PlatformChatRequest(content="hi").supports_reply_suggestions is False
