"""The linked-device gateway backend of the ``whatsapp`` integration."""

from __future__ import annotations

import base64

import pytest
from fastapi import HTTPException

from app.api.v1.integration_catalog import build_dummy_token, build_tools_for_provider
from app.integrations.providers import whatsapp, whatsapp_gateway
from app.integrations.providers.whatsapp_gateway import (
    GATEWAY_ONLY_TOOLS,
    WHAPI_BASE_URL,
    WhapiClient,
    chat_id,
    gateway_token,
    pace_send,
    parse_gateway_token,
    poll_payload,
    poll_tally,
    typing_seconds,
)
from app.services.integration_read_only import integration_tool_access
from app.services.integration_tools import _SafeIntegrationExecutor

_TOKEN = gateway_token("whapi", "", "channel-token")
_GROUP = "120363025555555555@g.us"


@pytest.fixture(autouse=True)
def _no_send_jitter(monkeypatch):
    """The pacing jitter is a real sleep: the tests send at once."""
    monkeypatch.setattr(whatsapp_gateway, "SEND_JITTER_SECONDS", (0.0, 0.0))


class _FakeResp:
    def __init__(self, payload: dict | None = None):
        self._payload = payload or {}
        self.status_code = 200
        self.is_success = True

    def json(self) -> dict:
        return self._payload


def _capture(monkeypatch, payload: dict | None = None) -> list[dict]:
    calls: list[dict] = []

    async def _fake(client, method, url, provider, action, **kwargs):
        calls.append({"method": method, "url": url, "action": action, **kwargs})
        return _FakeResp(payload if payload is not None else {"sent": True, "message": {"id": "MSG1"}}), None

    monkeypatch.setattr(whatsapp_gateway, "safe_request", _fake)
    return calls


def _handlers(token: str = _TOKEN):
    executor = _SafeIntegrationExecutor()
    tools = whatsapp.build_tools(token, executor)
    return executor._handlers, tools


def test_token_round_trip_defaults_to_whapi():
    assert parse_gateway_token(_TOKEN) == ("whapi", WHAPI_BASE_URL, "channel-token")
    assert parse_gateway_token(gateway_token("whapi", "https://gw.example.com/", "t")) == (
        "whapi",
        "https://gw.example.com",
        "t",
    )
    assert parse_gateway_token(gateway_token("unknown", "", "t")) is None
    assert parse_gateway_token(gateway_token("whapi", "", "")) is None
    assert parse_gateway_token("1234||EAAG") is None


def test_gateway_token_selects_the_gateway_tools():
    handlers, tools = _handlers()
    names = {t.name for t in tools}
    assert names >= GATEWAY_ONLY_TOOLS
    assert "send_whatsapp_template" not in names  # Meta-only
    assert set(handlers) == names


def test_meta_integration_never_gets_the_gateway_tools():
    _, tools = _handlers("1234567890||EAAGtoken")
    assert not GATEWAY_ONLY_TOOLS & {t.name for t in tools}


def test_catalog_lists_the_poll_and_pairing_tools():
    names = {t.name for t in build_tools_for_provider("whatsapp") or []}
    assert names >= GATEWAY_ONLY_TOOLS
    assert "send_whatsapp_template" in names
    assert whatsapp.build_tools(build_dummy_token("whatsapp"), _SafeIntegrationExecutor())


def test_poll_creation_is_a_write_and_results_a_read():
    assert integration_tool_access("send_whatsapp_poll") == "write"
    assert integration_tool_access("get_whatsapp_poll_results") == "read"
    assert integration_tool_access("get_whatsapp_pairing_status") == "read"


def test_chat_id_handles_numbers_and_groups():
    assert chat_id("+33612345678") == "33612345678"
    assert chat_id("33612345678@s.whatsapp.net") == "33612345678@s.whatsapp.net"
    assert chat_id(_GROUP) == _GROUP
    assert chat_id("Y2FwaV9ncm91cDox") is None
    assert chat_id("") is None


def test_poll_payload_single_and_multiple_choice():
    assert poll_payload(_GROUP, " Date? ", ["Mon", " Tue ", ""]) == {
        "to": _GROUP,
        "title": "Date?",
        "options": ["Mon", "Tue"],
        "count": 1,
    }
    assert poll_payload("+33612345678", "Q", ["a", "b"], multiple=True)["count"] == 0


@pytest.mark.parametrize(
    ("to", "title", "options", "error"),
    [
        ("bad", "Q", ["a", "b"], "'to'"),
        (_GROUP, " ", ["a", "b"], "title"),
        (_GROUP, "Q", ["a"], "2 to 12"),
        (_GROUP, "Q", [str(i) for i in range(13)], "2 to 12"),
        (_GROUP, "Q", ["a", "a"], "distinct"),
    ],
)
def test_poll_payload_refusals(to, title, options, error):
    result = poll_payload(to, title, options)
    assert isinstance(result, str) and error in result


@pytest.mark.asyncio
async def test_send_poll_posts_to_the_poll_endpoint(monkeypatch):
    calls = _capture(monkeypatch)
    handlers, _ = _handlers()
    out = await handlers["send_whatsapp_poll"](to=_GROUP, title="Board date", options=["Mon", "Tue"])
    assert calls[0]["method"] == "post"
    assert calls[0]["url"] == f"{WHAPI_BASE_URL}/messages/poll"
    assert calls[0]["json"] == {"to": _GROUP, "title": "Board date", "options": ["Mon", "Tue"], "count": 1}
    assert calls[0]["headers"]["Authorization"] == "Bearer channel-token"
    assert "MSG1" in out


_POLL_MESSAGE = {
    "id": "MSG1",
    "type": "poll",
    "poll": {
        "title": "Board date",
        "options": ["Mon", "Tue"],
        "total": 3,
        "results": [
            {"id": "o1", "name": "Mon", "count": 2, "voters": [{"id": "33611111111"}, "33622222222"]},
            {"id": "o2", "name": "Tue", "count": 1, "voters": [{"id": "33633333333"}]},
        ],
    },
}


def test_poll_tally_names_the_voters():
    tally = poll_tally(_POLL_MESSAGE)
    assert tally == {
        "title": "Board date",
        "total": 3,
        "options": [
            {"id": "o1", "name": "Mon", "count": 2, "voters": ["33611111111", "33622222222"]},
            {"id": "o2", "name": "Tue", "count": 1, "voters": ["33633333333"]},
        ],
    }
    assert poll_tally({"type": "text"}) is None
    no_votes = poll_tally({"poll": {"title": "Q", "options": ["a", "b"]}})
    assert no_votes["total"] == 0 and [o["name"] for o in no_votes["options"]] == ["a", "b"]


@pytest.mark.asyncio
async def test_poll_results_tool_reads_the_message(monkeypatch):
    calls = _capture(monkeypatch, _POLL_MESSAGE)
    handlers, _ = _handlers()
    out = await handlers["get_whatsapp_poll_results"](message_id="MSG1")
    assert calls[0]["url"] == f"{WHAPI_BASE_URL}/messages/MSG1"
    assert "Mon (option o1): 2 vote(s), by 33611111111, 33622222222" in out


@pytest.mark.parametrize(
    ("text", "state"),
    [
        ("AUTH", "connected"),
        ("QR", "qr"),
        ("BANNED", "banned"),
        ("SYNC_ERROR", "sync_error"),
        ("LAUNCH", "starting"),
        ("whatever", "unknown"),
    ],
)
def test_pairing_status_mapping(text, state):
    assert WhapiClient(WHAPI_BASE_URL, "t").pairing_state({"status": {"code": 4, "text": text}}) == state


@pytest.mark.asyncio
async def test_pairing_status_tool(monkeypatch):
    _capture(monkeypatch, {"status": {"text": "QR"}})
    handlers, _ = _handlers()
    out = await handlers["get_whatsapp_pairing_status"]()
    assert "pairing status: qr" in out


@pytest.mark.asyncio
async def test_pairing_qr_delivers_an_image(monkeypatch):
    png = b"\x89PNG\r\n\x1a\nfake"
    _capture(monkeypatch, {"status": "OK", "base64": "data:image/png;base64," + base64.b64encode(png).decode()})
    delivered: dict = {}

    async def _deliver(**kwargs):
        delivered.update(kwargs)
        return "delivered"

    monkeypatch.setattr(whatsapp_gateway, "deliver_file", _deliver)
    handlers, _ = _handlers()
    assert await handlers["get_whatsapp_pairing_qr"]() == "delivered"
    assert delivered["data"] == png
    assert delivered["content_type"] == "image/png"


@pytest.mark.asyncio
async def test_group_tools_use_the_group_jid(monkeypatch):
    calls = _capture(monkeypatch, {"name": "Board", "participants": [{"id": "33611111111", "rank": "admin"}]})
    handlers, _ = _handlers()
    out = await handlers["get_whatsapp_group"](group_id=_GROUP)
    assert calls[0]["url"] == f"{WHAPI_BASE_URL}/groups/{_GROUP}"
    assert "33611111111 (admin)" in out
    refused = await handlers["get_whatsapp_group"](group_id="not-a-group")
    assert refused.startswith("Error")


@pytest.mark.asyncio
async def test_send_text_to_a_group(monkeypatch):
    calls = _capture(monkeypatch)
    handlers, _ = _handlers()
    await handlers["send_whatsapp_message"](to=_GROUP, text="hello")
    assert calls[0]["url"] == f"{WHAPI_BASE_URL}/messages/text"
    assert calls[0]["json"] == {"to": _GROUP, "body": "hello", "typing_time": 1}


# --- outbound pacing and presence ---


class _FakeLimiter:
    """In-memory fixed windows standing in for ``enforce_rate_limit`` / ``refund_rate_limit``."""

    def __init__(self):
        self.counts: dict[str, int] = {}

    async def enforce(self, request, name, *, limit, window_seconds, bucket, **kwargs):
        key = f"{name}:{bucket}"
        self.counts[key] = self.counts.get(key, 0) + 1
        if self.counts[key] > limit:
            raise HTTPException(status_code=429, headers={"Retry-After": "42"})

    async def refund(self, request, name, *, bucket):
        key = f"{name}:{bucket}"
        self.counts[key] = max(0, self.counts.get(key, 0) - 1)


@pytest.fixture
def limiter(monkeypatch):
    fake = _FakeLimiter()
    monkeypatch.setattr(whatsapp_gateway, "enforce_rate_limit", fake.enforce)
    monkeypatch.setattr(whatsapp_gateway, "refund_rate_limit", fake.refund)
    return fake


@pytest.mark.parametrize(
    ("length", "seconds"),
    [(0, 1), (5, 1), (60, 5), (96, 8), (4096, whatsapp_gateway.TYPING_MAX_SECONDS)],
)
def test_typing_time_follows_the_length(length, seconds):
    assert typing_seconds("x" * length) == seconds


@pytest.mark.asyncio
async def test_chat_budget_refuses_and_refunds_the_integration_budget(monkeypatch, limiter):
    monkeypatch.setattr(whatsapp_gateway, "SENDS_PER_CHAT_PER_MINUTE", 2)
    calls = _capture(monkeypatch)
    executor = _SafeIntegrationExecutor()
    whatsapp.build_tools(_TOKEN, executor, integration_id="int-1")
    send = executor._handlers["send_whatsapp_message"]
    assert "sent" in await send(to=_GROUP, text="one")
    assert "sent" in await send(to=_GROUP, text="two")
    refused = await send(to=_GROUP, text="three")
    assert refused.startswith("Error: send limit reached for chat")
    assert "Retry in 42 seconds" in refused and "nothing was sent" in refused
    assert len(calls) == 2
    assert limiter.counts["whatsapp-gateway-send:int-1"] == 2  # the refused send was given back
    assert "sent" in await send(to="33611111111", text="other chat")


@pytest.mark.asyncio
async def test_integration_budget_covers_every_chat_and_the_poll(monkeypatch, limiter):
    monkeypatch.setattr(whatsapp_gateway, "SENDS_PER_MINUTE", 1)
    calls = _capture(monkeypatch)
    executor = _SafeIntegrationExecutor()
    whatsapp.build_tools(_TOKEN, executor, integration_id="int-2")
    assert "sent" in await executor._handlers["send_whatsapp_message"](to=_GROUP, text="hi")
    refused = await executor._handlers["send_whatsapp_poll"](to="33611111111", title="Q", options=["a", "b"])
    assert refused.startswith("Error: send limit reached for this WhatsApp number")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_pacing_waits_a_jitter_before_the_send(monkeypatch, limiter):
    monkeypatch.setattr(whatsapp_gateway, "SEND_JITTER_SECONDS", (0.5, 1.5))
    slept: list[float] = []

    async def _pause(seconds):
        slept.append(seconds)

    monkeypatch.setattr(whatsapp_gateway, "_pause", _pause)
    assert await pace_send("int-3", _GROUP) is None
    assert len(slept) == 1 and 0.5 <= slept[0] <= 1.5


@pytest.mark.asyncio
async def test_pacing_fails_open_without_redis():
    # The unit suite's Redis refuses every connection (conftest): the send still goes.
    assert await pace_send("int-4", _GROUP) is None


# --- edit, delete and the sent messages ---


def _route(monkeypatch, answers: dict[tuple[str, str], dict]) -> list[dict]:
    """Answer each (method, path) with its payload; every call is recorded."""
    calls: list[dict] = []

    async def _fake(client, method, url, provider, action, **kwargs):
        calls.append({"method": method, "url": url, "action": action, **kwargs})
        return _FakeResp(answers.get((method, url.removeprefix(WHAPI_BASE_URL)), {"success": True})), None

    monkeypatch.setattr(whatsapp_gateway, "safe_request", _fake)
    return calls


def test_edit_and_delete_are_gateway_only_writes():
    assert {"edit_whatsapp_message", "delete_whatsapp_message"} <= GATEWAY_ONLY_TOOLS
    assert integration_tool_access("edit_whatsapp_message") == "write"
    assert integration_tool_access("delete_whatsapp_message") == "write"
    names = {t.name for t in build_tools_for_provider("whatsapp") or []}
    assert {"edit_whatsapp_message", "delete_whatsapp_message"} <= names


@pytest.mark.asyncio
async def test_send_answers_the_message_id(monkeypatch):
    _capture(monkeypatch)
    handlers, _ = _handlers()
    assert "(message ID: MSG1)" in await handlers["send_whatsapp_message"](to=_GROUP, text="hello")


@pytest.mark.parametrize(
    ("message", "age", "refused"),
    [
        ({"from_me": True, "type": "text"}, 60, None),
        ({"from_me": True, "type": "text"}, 16 * 60, "within 15 minutes"),
        ({"from_me": False, "type": "text"}, 60, "not sent by this number"),
        ({"from_me": True, "type": "image"}, 60, "only a text message"),
        ({}, 10_000, None),
    ],
)
def test_edit_refusal(message, age, refused):
    now = 1_800_000_000.0
    out = whatsapp_gateway.edit_refusal({**message, "timestamp": now - age} if message else {}, "M1", now)
    assert (out is None) if refused is None else (refused in out and out.startswith("Error"))


@pytest.mark.asyncio
async def test_edit_sends_the_edit_within_the_window(monkeypatch):
    monkeypatch.setattr(whatsapp_gateway.time, "time", lambda: 1_800_000_100.0)
    message = {"id": "AB12-CD34", "from_me": True, "type": "text", "timestamp": 1_800_000_000}
    calls = _route(monkeypatch, {("get", "/messages/AB12-CD34"): message})
    handlers, _ = _handlers()
    out = await handlers["edit_whatsapp_message"](to="+33612345678", message_id="AB12-CD34", text="fixed")
    assert "edited" in out
    assert calls[-1]["method"] == "post" and calls[-1]["url"] == f"{WHAPI_BASE_URL}/messages/text"
    assert calls[-1]["json"] == {"to": "33612345678", "body": "fixed", "edit": "AB12-CD34"}


@pytest.mark.asyncio
async def test_edit_past_the_window_is_refused_and_nothing_sent(monkeypatch):
    monkeypatch.setattr(whatsapp_gateway.time, "time", lambda: 1_800_002_000.0)
    message = {"id": "AB12-CD34", "from_me": True, "type": "text", "timestamp": 1_800_000_000}
    calls = _route(monkeypatch, {("get", "/messages/AB12-CD34"): message})
    handlers, _ = _handlers()
    out = await handlers["edit_whatsapp_message"](to=_GROUP, message_id="AB12-CD34", text="late")
    assert out.is_error and "within 15 minutes" in out.content
    assert [c["method"] for c in calls] == ["get"]


@pytest.mark.asyncio
async def test_edit_refuses_bad_arguments_before_any_call(monkeypatch):
    calls = _route(monkeypatch, {})
    handlers, _ = _handlers()
    edit = handlers["edit_whatsapp_message"]
    assert (await edit(to="bad", message_id="M", text="x")).is_error
    assert (await edit(to=_GROUP, message_id=" ", text="x")).is_error
    assert (await edit(to=_GROUP, message_id="M", text=" ")).is_error
    assert (await edit(to=_GROUP, message_id="M", text="x" * 5000)).is_error
    assert calls == []


@pytest.mark.asyncio
async def test_delete_for_everyone(monkeypatch):
    calls = _route(monkeypatch, {})
    handlers, _ = _handlers()
    out = await handlers["delete_whatsapp_message"](to=_GROUP, message_id="AB12-CD34")
    assert "deleted for everyone" in out
    assert calls[0]["method"] == "delete" and calls[0]["url"] == f"{WHAPI_BASE_URL}/messages/AB12-CD34"
    refused = await handlers["delete_whatsapp_message"](to=_GROUP, message_id="AB12-CD34", for_everyone=False)
    assert refused.is_error and len(calls) == 1


@pytest.mark.asyncio
async def test_read_messages_lists_the_sent_ones_flagged_outgoing(monkeypatch):
    from app.services import whatsapp_inbound

    async def _received(integration_id, conversation_id, limit):
        return "1 message(s) received"

    monkeypatch.setattr(whatsapp_inbound, "messages_text", _received)
    sent = {
        "messages": [
            {"id": "S2", "type": "text", "text": {"body": "second"}, "timestamp": 1_800_000_100, "from_me": True},
            {"id": "S1", "type": "text", "text": {"body": "first"}, "timestamp": 1_800_000_000, "from_me": True},
        ]
    }
    calls = _route(monkeypatch, {("get", "/messages/list/33612345678@s.whatsapp.net"): sent})
    executor = _SafeIntegrationExecutor()
    whatsapp.build_tools(_TOKEN, executor, integration_id="int-5")
    out = await executor._handlers["read_whatsapp_messages"](conversation_id="33612345678", limit=10)
    assert out.startswith("1 message(s) received")
    assert calls[0]["params"] == {"count": 10, "from_me": "true", "sort": "desc"}
    assert out.index("message id S1: first") < out.index("message id S2: second")
    assert "sent by this number (outgoing)" in out
