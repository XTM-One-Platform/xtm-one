"""WhatsApp self-chat as an instruction channel of the owner (#9547).

The paired number's own "message yourself" chat is stored (``chat_type =
"self"``) and routed ONLY to the agent the integration's
``self_chat_agent_id`` names; every other chat keeps its behaviour, and the
agent's own replies echoed in the self-chat never start a turn.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import whatsapp_inbound
from app.services.agent_accounts.providers import whatsapp as wa
from app.services.agent_accounts.rules import AccountSettings, EventState, evaluate
from app.services.whatsapp_gateway_inbound import parse_whapi

OWN = "33611111111"
OTHER = "33622222222"
GROUP = "120363000000000000@g.us"


def _msg(**fields):
    base = {"id": "M1", "type": "text", "text": {"body": "what is two plus two"}, "timestamp": 1_800_000_000}
    return {**base, **fields}


def test_self_chat_message_is_stored_as_self():
    d = parse_whapi({"messages": [_msg(from_me=True, chat_id=f"{OWN}@s.whatsapp.net", **{"from": OWN})]})
    assert len(d.rows) == 1
    row = d.rows[0]
    assert row["chat_type"] == whatsapp_inbound.SELF_CHAT
    assert row["conversation_id"] == OWN and row["text"] == "what is two plus two"


@pytest.mark.parametrize(
    "msg",
    [
        # what the number sends to a contact
        _msg(from_me=True, chat_id=f"{OTHER}@s.whatsapp.net", **{"from": OWN}),
        # what it sends in a group
        _msg(from_me=True, chat_id=GROUP, **{"from": OWN}),
        # an own message without an explicit sender is never taken for the self-chat
        _msg(from_me=True, chat_id=f"{OWN}@s.whatsapp.net"),
    ],
)
def test_other_own_messages_are_still_not_stored(msg):
    assert parse_whapi({"messages": [msg]}).rows == []


def test_received_message_keeps_its_type():
    d = parse_whapi({"messages": [_msg(from_me=False, chat_id=f"{OTHER}@s.whatsapp.net", **{"from": OTHER})]})
    assert d.rows[0]["chat_type"] == "private"


def test_self_chat_event_is_marked_and_named_by_the_integration_owner():
    row = {"wamid": "M1", "conversation_id": OWN, "chat_type": "self", "sender": OWN, "text": "hi"}
    event = wa.event_from_row(row, owner_email="owner@example.com")
    assert event.self_chat and event.conversation_type == "direct"
    assert event.sender_email == "owner@example.com"
    assert not wa.event_from_row({**row, "chat_type": "private"}).self_chat


@pytest.mark.parametrize(
    ("extra", "expected"),
    [({}, None), ({"self_chat_agent_id": ""}, None), ({"self_chat_agent_id": "not-a-uuid"}, None)],
)
def test_self_chat_routing_is_off_by_default(extra, expected):
    assert wa.self_chat_agent(extra) is expected


def test_self_chat_agent_reads_the_setting():
    agent = uuid.uuid4()
    assert wa.self_chat_agent({"self_chat_agent_id": str(agent)}) == agent


class _Session:
    def __init__(self, accounts, integration, owner):
        self._accounts, self._integration, self._owner = accounts, integration, owner

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _query):
        return SimpleNamespace(all=lambda: self._accounts)

    async def get(self, model, _key):
        return self._integration if model.__name__ == "Integration" else self._owner


def _route(monkeypatch, *, extra, owner_active=True):
    routed, other = uuid.uuid4(), uuid.uuid4()
    acc_routed, acc_other = uuid.uuid4(), uuid.uuid4()
    integration = SimpleNamespace(extra_config_enc=b"x", user_id=uuid.uuid4())
    owner = SimpleNamespace(email="owner@example.com", is_active=owner_active)
    extra = {k: (str(routed) if v == "ROUTED" else v) for k, v in extra.items()}
    monkeypatch.setattr(
        "app.core.database.async_session",
        lambda: _Session([(acc_routed, routed), (acc_other, other)], integration, owner),
    )
    monkeypatch.setattr("app.core.security.decrypt_json", lambda _blob: extra)
    sent: list[tuple[str, object]] = []
    monkeypatch.setattr("app.services.agent_accounts.pipeline.dispatch", lambda a, e: sent.append((a, e)))
    return str(acc_routed), str(acc_other), sent


SELF_ROW = {"wamid": "S1", "conversation_id": OWN, "chat_type": "self", "sender": OWN, "text": "hi"}
OTHER_ROW = {"wamid": "O1", "conversation_id": OTHER, "chat_type": "private", "sender": OTHER, "text": "hello"}


async def test_self_chat_goes_only_to_the_configured_agent(monkeypatch):
    routed, other, sent = _route(monkeypatch, extra={"self_chat_agent_id": "ROUTED"})
    await wa.dispatch_stored(uuid.uuid4(), [SELF_ROW, OTHER_ROW])
    self_targets = [a for a, e in sent if e.self_chat]
    assert self_targets == [routed]
    # every other chat still reaches every account, as before
    assert sorted(a for a, e in sent if not e.self_chat) == sorted([routed, other])


async def test_self_chat_is_not_routed_when_off(monkeypatch):
    _routed, _other, sent = _route(monkeypatch, extra={})
    await wa.dispatch_stored(uuid.uuid4(), [SELF_ROW])
    assert sent == []


async def test_self_chat_is_not_routed_without_an_active_owner(monkeypatch):
    _routed, _other, sent = _route(monkeypatch, extra={"self_chat_agent_id": "ROUTED"}, owner_active=False)
    await wa.dispatch_stored(uuid.uuid4(), [SELF_ROW])
    assert sent == []


_VOICE = {
    "id": "V1",
    "type": "voice",
    "voice": {"id": "oga-abc123", "seconds": 42, "mime_type": "audio/ogg; codecs=opus"},
    "timestamp": 1_800_000_000,
    "from_me": True,
    "chat_id": f"{OWN}@s.whatsapp.net",
    "from": OWN,
}


def _gateway(**fields):
    from app.services.whatsapp_gateway_inbound import GatewayIntegration

    target = whatsapp_inbound.InboundIntegration(
        integration_id=uuid.uuid4(), app_secret="", verify_token="", retention_days=30
    )
    base = {
        "target": target,
        "vendor": "whapi",
        "webhook_secret": "s",
        "channel_token": "tok",
        "owner_id": uuid.uuid4(),
    }
    return GatewayIntegration(**{**base, **fields})


def test_voice_note_is_parsed_with_its_duration():
    d = parse_whapi({"messages": [_VOICE]})
    assert d.voice["V1"].media_id == "oga-abc123" and d.voice["V1"].seconds == 42
    # until transcribed, the row says so: never an empty message
    assert d.rows[0]["text"] == "[voice note, 0:42: transcription failed: not transcribed]"


async def test_voice_note_is_transcribed_into_the_row(monkeypatch):
    from app.services import whatsapp_gateway_inbound as gi

    async def fake_one(_gateway, note):
        assert note.media_id == "oga-abc123"
        return "[voice note, 0:42, transcribed] combien font deux plus deux", None

    monkeypatch.setattr(gi, "_transcribe_one", fake_one)
    d = parse_whapi({"messages": [_VOICE]})
    assert await gi.transcribe_voice(_gateway(), d) == 1
    line, audio = d.rows[0]["text"].split("\n")
    assert line == "[voice note, 0:42, transcribed] combien font deux plus deux"
    assert "media id oga-abc123" in audio


async def test_voice_note_failure_is_stated(monkeypatch):
    from app.services import whatsapp_gateway_inbound as gi

    async def boom(_gateway, _note):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(gi, "_transcribe_one", boom)
    d = parse_whapi({"messages": [_VOICE]})
    assert await gi.transcribe_voice(_gateway(), d) == 0
    assert d.rows[0]["text"].startswith("[voice note, 0:42: transcription failed:")
    assert "gateway down" in d.rows[0]["text"]


async def test_voice_note_download_failure_is_stated(monkeypatch):
    from app.integrations.providers import whatsapp_gateway as wg
    from app.services import whatsapp_gateway_inbound as gi

    async def no_media(self, media_id, max_bytes):
        return b"", "404 not found"

    monkeypatch.setattr(wg.WhapiClient, "download_media", no_media)
    d = parse_whapi({"messages": [_VOICE]})
    await gi.transcribe_voice(_gateway(), d)
    assert d.rows[0]["text"].split("\n")[0] == (
        "[voice note, 0:42: transcription failed: download failed (404 not found)]"
    )


async def test_voice_note_without_a_token_is_stated():
    from app.services import whatsapp_gateway_inbound as gi

    d = parse_whapi({"messages": [_VOICE]})
    await gi.transcribe_voice(_gateway(channel_token=""), d)
    assert "transcription failed: the gateway cannot be reached" in d.rows[0]["text"]


@pytest.mark.parametrize(("seconds", "words"), [(0, "0:00"), (42, "0:42"), (125, "2:05")])
def test_duration_words(seconds, words):
    from app.services.whatsapp_gateway_inbound import duration_words

    assert duration_words(seconds) == words


def _settings():
    return AccountSettings.of(
        SimpleNamespace(
            enabled=True,
            agent_id=uuid.uuid4(),
            rules=[{"id": "r1", "trigger": "message", "action": "respond", "conversation_types": ["direct"]}],
            instructions="",
            response_mode="auto",
            ai_label_enabled=False,
            grace_period_seconds=0,
            owner_takeover_minutes=0,
            max_replies_per_hour=0,
            max_turns_per_hour=0,
            active_hours=None,
        )
    )


def test_echo_of_the_agents_own_reply_is_never_a_turn():
    event = wa.event_from_row(SELF_ROW, owner_email="owner@example.com")
    decision = evaluate(_settings(), event, EventState(agent_post=True))
    assert not decision.respond and decision.reason == "agent_author"
