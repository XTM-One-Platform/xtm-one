"""What a routing outcome logs (``ROUTING.md`` section 14).

A bot in a busy channel sees every message and most are not for it. Each of
those used to log one or more WARNING lines - the per-agent
"not @mentioned" / "not in receive_channels" diagnostic, "GA not enabled for
this channel" with the sender's email, the whole ``channel_config`` of a bound
agent - which drowned the warnings an operator needs.

Pinned here: a group message that was not for the bot is never a WARNING and
its per-agent reasons stay at DEBUG; a configuration that cannot work (a
conversation bound to an agent that does not receive on its channel, an
@mention nobody can answer) is still a WARNING naming the deciding setting;
and no router record carries the sender's email.

For the content: no channel record at INFO or above carries the
message text, the sender's display name or phone number, adapter by adapter
through each webhook and its processing task; no WhatsApp id (the number, a
message id that encodes it, a group) nor a key the router builds from one is
logged raw at any level; an attachment is logged without its name or URL;
with an AST tripwire over ``app/channels`` for the paths no test drives (the
Discord gateway handler).

A message with no text or no thread id (a sticker, a picture, a JSON
``null``) is skipped or read as the empty string a missing field gives, so no
length or cut in a log line raises on it.
"""

from __future__ import annotations

import ast
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.requests import Request

from app.channels.base import ChannelType, InboundMessage, OutboundMessage, log_preview
from tests.test_background_edition_gate import COMMUNITY, _pin, _Session

EMAIL = "analyst@example.com"
ROUTER_LOGGER = "app.channels.router"


def _slack(*, dm: bool = False, mentioned: bool = False, text: str = "Did anyone look at the VPN alerts?") -> InboundMessage:
    metadata: dict = {"channel_type": "im" if dm else "channel"}
    if mentioned:
        metadata["bot_was_mentioned"] = True
    return InboundMessage(
        channel=ChannelType.SLACK,
        text=text,
        user_email=EMAIL,
        channel_user_id="U12345",
        channel_thread_id="D0123" if dm else "C0123:1700000000.000100",
        metadata=metadata,
    )


def _bot(**overrides) -> SimpleNamespace:
    values = {"id": uuid.uuid4(), "only_mention": False, "allow_anonymous": False, "allow_private": True}
    values.update(overrides)
    return SimpleNamespace(**values)


def _agent(name: str, **slack_rules) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        enabled=True,
        user_id=None,
        is_company_managed=True,
        channel_config={
            "slack": {
                "channel_bot_id": str(uuid.uuid4()),
                "receive_enabled": True,
                "publish_enabled": True,
                **slack_rules,
            }
        },
        available_channel_types=None,
    )


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), email=EMAIL, channel_agent_config=None)


MENTION_ONLY = _agent("Mention only", only_mention=True)
ELSEWHERE = _agent("Other channel", receive_channels=[{"id": "C0999"}])
NOT_RECEIVING = _agent("Receive off", receive_enabled=False)


def _route(monkeypatch, *, bot, user, rows=(), bound=None):
    """``handle_message`` with its collaborators stubbed, both agent resolvers real.

    Community Edition answers right after routing, so every routing rule
    runs and nothing past it needs stubbing. ``rows``: what the session answers,
    in order - the resolvers' agent lists, a bound agent's row, a conversation.
    """
    from app.channels import router

    _pin(monkeypatch, **COMMUNITY)
    session = _Session(rows)
    monkeypatch.setattr(router, "async_session", lambda: session)
    monkeypatch.setattr(router, "_get_channel_bot", AsyncMock(return_value=bot))
    monkeypatch.setattr(router, "_bot_allows_receive", lambda _bot, _inbound: True)
    monkeypatch.setattr(router, "_is_thread_active", AsyncMock(return_value=False))
    monkeypatch.setattr(router, "_is_thread_muted", AsyncMock(return_value=False))
    monkeypatch.setattr(router, "resolve_user_by_email", AsyncMock(return_value=user))
    monkeypatch.setattr(router, "resolve_user_by_channel_identity", AsyncMock(return_value=user))
    monkeypatch.setattr(router, "link_channel_identity", AsyncMock())
    monkeypatch.setattr(router, "get_user_group_ids", AsyncMock(return_value=[]))
    conv = SimpleNamespace(id=uuid.uuid4(), agent_id=bound.id) if bound else None
    monkeypatch.setattr(router, "_find_existing_external_conversation", AsyncMock(return_value=conv))
    monkeypatch.setattr(router, "_find_bound_conversation", AsyncMock(return_value=None))
    monkeypatch.setattr(router, "_release_bound_thread", AsyncMock(return_value=0))
    monkeypatch.setattr(router, "get_conversation_history", AsyncMock(return_value=[]))
    monkeypatch.setattr(router, "_ga_allows_channel", AsyncMock(return_value=False))
    return router


def _records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == ROUTER_LOGGER]


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in _records(caplog) if r.levelno >= logging.WARNING]


def _silent(out) -> bool:
    return out.suppress is True and out.text == ""


# ── Not for the bot: never a WARNING, the reasons stay at DEBUG ────────────


@pytest.mark.asyncio
async def test_a_message_every_agent_declines_is_debug_with_each_agents_reason(monkeypatch, caplog):
    """The production sample: one agent answers only an @mention, one listens to
    other channels, the General Assistant is not enabled for the channel."""
    caplog.set_level(logging.DEBUG, logger=ROUTER_LOGGER)
    router = _route(
        monkeypatch, bot=_bot(), user=_user(), rows=[[MENTION_ONLY], [MENTION_ONLY, ELSEWHERE]]
    )

    assert _silent(await router.handle_message(_slack()))

    assert _warnings(caplog) == []
    diagnostic = [
        r.getMessage()
        for r in _records(caplog)
        if r.levelno == logging.DEBUG and r.getMessage().startswith("No agent took the slack message:")
    ]
    assert len(diagnostic) == 1
    assert "'Mention only'" in diagnostic[0] and "only_mention=true but bot was not @mentioned" in diagnostic[0]
    assert "'Other channel'" in diagnostic[0] and "C0123 not in receive_channels whitelist" in diagnostic[0]
    assert any(
        r.levelno == logging.DEBUG and r.getMessage().startswith("The General Assistant is not enabled for slack")
        for r in _records(caplog)
    )


@pytest.mark.asyncio
async def test_a_bound_agent_that_declines_a_message_is_debug(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=ROUTER_LOGGER)
    router = _route(monkeypatch, bot=_bot(), user=_user(), rows=[MENTION_ONLY], bound=MENTION_ONLY)

    assert _silent(await router.handle_message(_slack()))

    assert _warnings(caplog) == []
    declined = [r.getMessage() for r in _records(caplog) if r.getMessage().startswith("Bound agent 'Mention only'")]
    assert len(declined) == 1 and "only_mention=true" in declined[0]
    assert [r.levelno for r in _records(caplog) if r.getMessage() in declined] == [logging.DEBUG]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bot", "rows"),
    [(_bot(allow_anonymous=False), []), (_bot(allow_anonymous=True), [[MENTION_ONLY, ELSEWHERE]])],
    ids=["anonymous_not_enabled", "no_agent_for_anonymous"],
)
async def test_a_group_message_from_a_sender_without_an_account_is_not_a_warning(monkeypatch, caplog, bot, rows):
    caplog.set_level(logging.DEBUG, logger=ROUTER_LOGGER)
    router = _route(monkeypatch, bot=bot, user=None, rows=rows)

    assert _silent(await router.handle_message(_slack()))

    assert _warnings(caplog) == []


@pytest.mark.asyncio
async def test_a_group_message_on_a_channel_no_agent_receives_is_debug(monkeypatch, caplog):
    """A publish-only bot sees every message of the channels it posts to."""
    caplog.set_level(logging.DEBUG, logger=ROUTER_LOGGER)
    router = _route(monkeypatch, bot=_bot(), user=_user(), rows=[[], []])

    assert _silent(await router.handle_message(_slack()))

    assert _warnings(caplog) == []


@pytest.mark.asyncio
async def test_a_direct_message_the_router_answers_itself_is_one_info_line(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=ROUTER_LOGGER)
    bot = _bot(allow_anonymous=False)
    router = _route(monkeypatch, bot=bot, user=None)

    out = await router.handle_message(_slack(dm=True))

    assert out.text.startswith("I couldn't identify your account")
    assert _warnings(caplog) == []
    info = [r.getMessage() for r in _records(caplog) if r.levelno == logging.INFO]
    assert info == [
        "Anonymous slack message not processed: anonymous access is not enabled on the "
        f"channel bot (channel_bot={bot.id}, channel_thread_id=D0123)"
    ]


# ── A configuration that cannot work: still a WARNING, concise ─────────────


@pytest.mark.asyncio
async def test_a_conversation_bound_to_an_agent_that_does_not_receive_warns_with_the_setting(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=ROUTER_LOGGER)
    router = _route(monkeypatch, bot=_bot(), user=_user(), rows=[NOT_RECEIVING], bound=NOT_RECEIVING)

    assert _silent(await router.handle_message(_slack()))

    (warning,) = _warnings(caplog)
    assert "'Receive off'" in warning and "receive_enabled=false" in warning
    assert "publish_enabled" not in warning and "channel_bot_id" not in warning, "the config is not dumped"


@pytest.mark.asyncio
async def test_an_at_mention_nobody_can_answer_warns(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=ROUTER_LOGGER)
    router = _route(monkeypatch, bot=_bot(), user=_user(), rows=[[], []])

    assert _silent(await router.handle_message(_slack(mentioned=True)))

    (warning,) = _warnings(caplog)
    assert warning.startswith("A slack @mention went unanswered: no agent receives slack messages")


@pytest.mark.asyncio
async def test_an_at_mention_an_agent_declined_is_not_a_warning(monkeypatch, caplog):
    """The agent listens to other channels: the configuration says so."""
    caplog.set_level(logging.DEBUG, logger=ROUTER_LOGGER)
    router = _route(monkeypatch, bot=_bot(), user=_user(), rows=[[ELSEWHERE], [ELSEWHERE]])

    assert _silent(await router.handle_message(_slack(mentioned=True)))

    assert _warnings(caplog) == []


# ── No router record carries the sender's email ───────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inbound", "bot", "user", "rows", "bound"),
    [
        (_slack(), _bot(), _user(), [[MENTION_ONLY], [MENTION_ONLY, ELSEWHERE]], None),
        (_slack(mentioned=True), _bot(), _user(), [[], []], None),
        (_slack(), _bot(), _user(), [NOT_RECEIVING], NOT_RECEIVING),
        (_slack(), _bot(allow_anonymous=False), None, [], None),
        (_slack(dm=True), _bot(allow_anonymous=False), None, [], None),
        (_slack(dm=True), _bot(allow_anonymous=True), None, [[NOT_RECEIVING]], None),
        (
            _slack(text="/reset"),
            _bot(),
            _user(),
            [SimpleNamespace(id=uuid.uuid4(), is_archived=False)],
            None,
        ),
    ],
    ids=[
        "declined",
        "unanswered_mention",
        "bound_not_receiving",
        "anonymous_group",
        "anonymous_dm",
        "anonymous_dm_no_agent",
        "reset",
    ],
)
async def test_no_router_record_carries_the_senders_email(monkeypatch, caplog, inbound, bot, user, rows, bound):
    caplog.set_level(logging.DEBUG, logger=ROUTER_LOGGER)
    router = _route(monkeypatch, bot=bot, user=user, rows=rows, bound=bound)

    await router.handle_message(inbound)

    assert _records(caplog), "the scenario logs something"
    assert [r.getMessage() for r in _records(caplog) if EMAIL in r.getMessage()] == []


# ── No channel record at INFO or above carries what a person wrote ──

TEXT = "My badge PIN is 4471, can you reset it before the audit?"
NAME = "Jane Placeholder"
PHONE = "33612345678"
# A WhatsApp message id is base64 over a record holding the other party's
# number in clear (Meta's own examples decode to it): logging one logs the number.
WAMID = "wamid." + base64.b64encode(b"\x1c\x18\x0b" + PHONE.encode() + b"\x15\x02\x00\x12\x18\x143A4A6599AEE0\x00").decode()
REPLY_WAMID = "wamid." + base64.b64encode(b"\x1c\x18\x0b" + PHONE.encode() + b"\x15\x02\x00\x11\x18\x125F425A74F130\x00").decode()
GROUP_ID = "Y2FwaV9ncm91cDozMzE5ODc2NTQzMjoxMjAzNjM0MDQ2OTQyOTM4NjM="
TELEGRAM_TOKEN = "123456:AAH-telegram-bot-token"


def _rendered(record: logging.LogRecord) -> str:
    """A record as a handler writes it, traceback included."""
    return logging.Formatter("%(name)s %(levelname)s: %(message)s").format(record)


def _leaks(caplog, *extra: str, loggers: str = "app.channels") -> list[str]:
    """Records of *loggers* at INFO or above carrying the text, a name, an email, a phone or *extra*, in any case."""
    personal = [v.lower() for v in (TEXT, "badge PIN", NAME, "Jane", "Placeholder", PHONE, EMAIL, *extra)]
    rendered = [_rendered(r) for r in caplog.records if r.name.startswith(loggers) and r.levelno >= logging.INFO]
    return [text for text in rendered if any(value in text.lower() for value in personal)]


def _request(path: str, payload: dict, headers: dict[str, str] | None = None) -> Request:
    body = json.dumps(payload).encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


class _FakeChannel:
    """A platform client that sends nothing: every call is an awaitable no-op."""

    supports_typing = False

    def __init__(self, *_args, **_kwargs):
        pass

    def __getattr__(self, _name):
        return AsyncMock(return_value=None)


async def _no_network(_client, request, **_kwargs):
    # pytest.fail raises a BaseException, which the adapters' ``except Exception`` cannot swallow.
    pytest.fail(f"unstubbed outbound request: {request.method} {request.url.host}")


def _adapter(monkeypatch, module) -> list:
    """Stub the adapter's platform client and router; return its spawned tasks.

    Any real outbound HTTP request, from the webhook or from a task it spawned, fails the test.
    """
    spawned: list = []
    monkeypatch.setattr(httpx.AsyncClient, "send", _no_network)
    monkeypatch.setattr(module, "spawn_channel_task", lambda coro, **_kw: spawned.append(coro))
    monkeypatch.setattr(
        module, "handle_message", AsyncMock(return_value=OutboundMessage(text="", suppress=True))
    )
    return spawned


async def _run(spawned: list) -> None:
    assert len(spawned) == 1, "the webhook hands the message to its processing task"
    await spawned.pop()


@pytest.mark.asyncio
async def test_the_slack_adapter_never_logs_the_text_or_the_senders_name(monkeypatch, caplog):
    from app.channels import slack

    caplog.set_level(logging.DEBUG, logger="app.channels")
    spawned = _adapter(monkeypatch, slack)
    monkeypatch.setattr(slack, "SlackChannel", _FakeChannel)
    monkeypatch.setattr(slack, "_get_slack_bot_creds", AsyncMock(return_value=("xoxb-token", "signing")))
    monkeypatch.setattr(slack, "verify_slack_signature", lambda *_a: True)
    monkeypatch.setattr(slack, "_is_duplicate_event", AsyncMock(return_value=False))
    monkeypatch.setattr(slack, "_resolve_slack_user", AsyncMock(return_value=(EMAIL, NAME)))
    payload = {
        "type": "event_callback",
        "authorizations": [{"user_id": "UBOT"}],
        "event": {
            "type": "app_mention",
            "text": f"<@UBOT> {TEXT}",
            "user": "U12345",
            "channel": "C0123",
            "channel_type": "channel",
            "ts": "1700000000.000100",
        },
    }

    await slack.slack_events(_request("/api/channels/slack/events", payload))
    await _run(spawned)

    assert _leaks(caplog) == []
    assert slack.handle_message.await_args.args[0].text == TEXT, "the message still reaches the router"


@pytest.mark.asyncio
async def test_the_teams_adapter_never_logs_the_text_or_the_senders_name(monkeypatch, caplog):
    from app.channels import teams

    caplog.set_level(logging.DEBUG, logger="app.channels")
    spawned = _adapter(monkeypatch, teams)
    monkeypatch.setattr(teams, "TeamsChannel", _FakeChannel)
    monkeypatch.setattr(teams, "_get_teams_app_id", AsyncMock(return_value=None))
    monkeypatch.setattr(teams, "_get_teams_creds", AsyncMock(return_value=("bf-token", "app-id")))
    monkeypatch.setattr(teams, "_get_teams_graph_creds", AsyncMock(return_value=None))
    monkeypatch.setattr(teams, "_fetch_graph_inbound_message", AsyncMock(return_value=None))
    monkeypatch.setattr(teams, "_store_service_url", AsyncMock())
    monkeypatch.setattr(teams, "_store_conversation_reference", AsyncMock())
    payload = {
        "type": "message",
        "text": f"<at>Copilot</at> {TEXT}",
        "serviceUrl": "https://smba.trafficmanager.net/emea/",
        "from": {"id": "29:1abc", "aadObjectId": "aad-123", "name": NAME, "email": EMAIL},
        "recipient": {"id": "28:bot"},
        "conversation": {"id": "19:chat@thread.v2", "conversationType": "groupChat"},
        "entities": [{"type": "mention", "mentioned": {"id": "28:bot"}, "text": "<at>Copilot</at>"}],
        "channelData": {"tenant": {"id": "tenant-1"}},
    }

    await teams.teams_messages(_request("/api/channels/teams/messages", payload))
    await _run(spawned)

    assert _leaks(caplog) == []
    assert teams.handle_message.await_args.args[0].text == TEXT


@pytest.mark.asyncio
async def test_the_telegram_adapter_never_logs_the_text_or_the_senders_name(monkeypatch, caplog):
    from app.channels import telegram

    caplog.set_level(logging.DEBUG, logger="app.channels")
    spawned = _adapter(monkeypatch, telegram)
    monkeypatch.setattr(telegram, "TelegramChannel", _FakeChannel)
    monkeypatch.setattr(
        telegram,
        "_get_telegram_creds",
        AsyncMock(return_value={"token": "tg-token", "webhook_secret_token": "", "bot_username": "copilot_bot"}),
    )
    text = f"@copilot_bot {TEXT}"
    payload = {
        "message": {
            "message_id": 7,
            "text": text,
            "chat": {"id": -1001, "type": "group"},
            "from": {"id": 42, "first_name": "Jane", "last_name": "Placeholder", "username": "jane_placeholder"},
            "entities": [{"type": "mention", "offset": 0, "length": len("@copilot_bot")}],
        }
    }

    await telegram.telegram_webhook(_request("/api/channels/telegram/webhook", payload))
    await _run(spawned)

    assert _leaks(caplog) == []
    assert not [r for r in caplog.records if r.levelno >= logging.INFO and "jane_placeholder" in r.getMessage()]
    assert telegram.handle_message.await_args.args[0].text == TEXT


class _WhatsAppChannel(_FakeChannel):
    async def send_message(self, _outbound):
        return REPLY_WAMID


@pytest.mark.asyncio
@pytest.mark.parametrize("group_id", [None, GROUP_ID], ids=["direct_message", "group"])
async def test_the_whatsapp_adapter_never_logs_the_text_the_name_or_the_number(monkeypatch, caplog, group_id):
    """Nor a message id, which carries the number, nor a group id: WhatsApp ids go through ``log_id``."""
    from app.channels import whatsapp

    caplog.set_level(logging.DEBUG, logger="app.channels")
    spawned = _adapter(monkeypatch, whatsapp)
    monkeypatch.setattr(whatsapp, "handle_message", AsyncMock(return_value=OutboundMessage(text="Done.")))
    monkeypatch.setattr(whatsapp, "WhatsAppChannel", _WhatsAppChannel)
    monkeypatch.setattr(whatsapp, "_get_whatsapp_creds", AsyncMock(return_value=("token", "pn-1", "verify", "")))
    message = {"type": "text", "from": PHONE, "id": WAMID, "text": {"body": TEXT}}
    if group_id:
        message["group_id"] = group_id
    payload = {
        "entry": [
            {"changes": [{"value": {"contacts": [{"wa_id": PHONE, "profile": {"name": NAME}}], "messages": [message]}}]}
        ]
    }

    await whatsapp.whatsapp_webhook(_request("/api/channels/whatsapp/webhook", payload))
    await _run(spawned)

    assert _leaks(caplog, WAMID, REPLY_WAMID, GROUP_ID) == []
    assert any(r.getMessage().startswith("WhatsApp processing: reply sent") for r in caplog.records)
    assert whatsapp.handle_message.await_args.args[0].channel_user_id == PHONE


@pytest.mark.asyncio
async def test_the_google_chat_adapter_never_logs_the_text_or_the_senders_name(monkeypatch, caplog):
    from app.channels import google_chat

    caplog.set_level(logging.DEBUG, logger="app.channels")
    spawned = _adapter(monkeypatch, google_chat)
    monkeypatch.setattr(google_chat, "GoogleChatChannel", _FakeChannel)
    monkeypatch.setattr(
        google_chat, "_get_googlechat_creds", AsyncMock(return_value={"credentials_json": "{}"})
    )
    payload = {
        "type": "MESSAGE",
        "space": {"name": "spaces/AAA", "type": "ROOM"},
        "message": {
            "name": "spaces/AAA/messages/1",
            "text": TEXT,
            "sender": {"name": "users/123", "displayName": NAME, "email": EMAIL, "type": "HUMAN"},
        },
    }

    await google_chat.googlechat_webhook(_request("/api/channels/googlechat/webhook", payload))
    await _run(spawned)

    assert _leaks(caplog) == []
    assert google_chat.handle_message.await_args.args[0].text == TEXT


def _telegram_update(**message) -> dict:
    return {"message": {"message_id": 8, "chat": {"id": -1001, "type": "group"}, "from": {"id": 42}, **message}}


def _whatsapp_update(message: dict) -> dict:
    return {"entry": [{"changes": [{"value": {"messages": [{"from": PHONE, "id": WAMID, **message}]}}]}]}


def _slack_update(text) -> dict:
    event = {"type": "message", "text": text, "user": "U12345", "channel": "C0123", "channel_type": "channel"}
    return {"type": "event_callback", "authorizations": [{"user_id": "UBOT"}], "event": {**event, "ts": "1700000000.000100"}}


def _teams_update(text, conversation_id="19:chat@thread.v2") -> dict:
    """A Teams message activity; ``None`` for *text* or *conversation_id* is sent as a JSON ``null``."""
    return {
        "type": "message",
        "text": text,
        "serviceUrl": "https://smba.trafficmanager.net/emea/",
        "from": {"id": "29:1abc", "aadObjectId": "aad-123"},
        "recipient": {"id": "28:bot"},
        "conversation": {"id": conversation_id, "conversationType": "personal"},
    }


_GOOGLE_CHAT_NO_TEXT = {
    "type": "MESSAGE",
    "space": {"name": "spaces/AAA", "type": "ROOM"},
    "message": {"name": "spaces/AAA/messages/2", "text": None, "argumentText": None, "sender": {"name": "users/123"}},
}


async def _deliver(monkeypatch, adapter: str, payload: dict):
    """Post *payload* to *adapter*'s webhook; return the module and the tasks it spawned."""
    from app.channels import google_chat, slack, teams, telegram, whatsapp

    module = {"google_chat": google_chat, "slack": slack, "teams": teams, "telegram": telegram, "whatsapp": whatsapp}[
        adapter
    ]
    spawned = _adapter(monkeypatch, module)
    if adapter == "telegram":
        creds = {"token": "tg-token", "webhook_secret_token": "", "bot_username": "copilot_bot"}
        monkeypatch.setattr(telegram, "_get_telegram_creds", AsyncMock(return_value=creds))
        await telegram.telegram_webhook(_request("/api/channels/telegram/webhook", payload))
    elif adapter == "whatsapp":
        monkeypatch.setattr(whatsapp, "_get_whatsapp_creds", AsyncMock(return_value=("token", "pn-1", "verify", "")))
        await whatsapp.whatsapp_webhook(_request("/api/channels/whatsapp/webhook", payload))
    elif adapter == "google_chat":
        monkeypatch.setattr(google_chat, "_get_googlechat_creds", AsyncMock(return_value={"credentials_json": "{}"}))
        await google_chat.googlechat_webhook(_request("/api/channels/googlechat/webhook", payload))
    elif adapter == "slack":
        monkeypatch.setattr(slack, "_get_slack_bot_creds", AsyncMock(return_value=("xoxb-token", "signing")))
        monkeypatch.setattr(slack, "verify_slack_signature", lambda *_a: True)
        monkeypatch.setattr(slack, "_is_duplicate_event", AsyncMock(return_value=False))
        await slack.slack_events(_request("/api/channels/slack/events", payload))
    else:
        monkeypatch.setattr(teams, "TeamsChannel", _FakeChannel)
        monkeypatch.setattr(teams, "_get_teams_app_id", AsyncMock(return_value=None))
        monkeypatch.setattr(teams, "_get_teams_creds", AsyncMock(return_value=("bf-token", "app-id")))
        monkeypatch.setattr(teams, "_get_teams_graph_creds", AsyncMock(return_value=None))
        monkeypatch.setattr(teams, "_fetch_graph_inbound_message", AsyncMock(return_value=None))
        monkeypatch.setattr(teams, "_store_service_url", AsyncMock())
        monkeypatch.setattr(teams, "_store_conversation_reference", AsyncMock())
        monkeypatch.setattr(teams, "_resolve_teams_email", AsyncMock(return_value=None))
        await teams.teams_messages(_request("/api/channels/teams/messages", payload))
    return module, spawned


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter", "payload"),
    [
        ("telegram", _telegram_update(sticker={"file_id": "CAACAgIAAxkBAAE", "emoji": "+"})),
        ("telegram", _telegram_update(text=None)),
        ("whatsapp", _whatsapp_update({"type": "sticker", "sticker": {"id": "5566"}})),
        ("whatsapp", _whatsapp_update({"type": "text", "text": None})),
        ("whatsapp", _whatsapp_update({"type": "text", "text": {"body": None}})),
        ("google_chat", _GOOGLE_CHAT_NO_TEXT),
        ("slack", _slack_update(None)),
        ("teams", _teams_update(None)),
    ],
    ids=[
        "telegram_sticker",
        "telegram_null_text",
        "whatsapp_sticker",
        "whatsapp_null_text",
        "whatsapp_null_body",
        "google_chat_null_text",
        "slack_null_text",
        "teams_null_text",
    ],
)
async def test_a_message_without_text_is_skipped_by_its_adapter_without_raising(monkeypatch, caplog, adapter, payload):
    """A sticker or a JSON ``null`` text never reaches a ``len(text)`` in a log line."""
    caplog.set_level(logging.DEBUG, logger="app.channels")

    module, spawned = await _deliver(monkeypatch, adapter, payload)

    assert spawned == [], "a message with no text is not handed to the router"
    module.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_teams_message_without_a_conversation_id_is_logged_without_raising(monkeypatch, caplog):
    """The webhook and its processing task cut the conversation id in their log lines."""
    caplog.set_level(logging.DEBUG, logger="app.channels")

    module, spawned = await _deliver(monkeypatch, "teams", _teams_update(TEXT, conversation_id=None))
    await _run(spawned)

    assert module.handle_message.await_args.args[0].channel_thread_id == "", "read as the missing field reads"
    assert any(r.getMessage().startswith("Teams processing: suppressing reply") for r in caplog.records)
    assert not [r for r in caplog.records if "EXCEPTION" in r.getMessage()]
    assert _leaks(caplog) == []


@pytest.mark.asyncio
async def test_a_cached_teams_backfill_for_a_chat_without_a_thread_id_is_logged_without_raising(monkeypatch, caplog):
    """``_log_thread_id(inbound)[:60]`` in ``_load_channel_backfill``: the helper always returns a str."""
    from app.channels import router, teams

    caplog.set_level(logging.INFO, logger=ROUTER_LOGGER)
    monkeypatch.setattr(teams, "load_teams_chat_backfill", AsyncMock(return_value="Earlier: the VPN alert"))
    inbound = InboundMessage(channel=ChannelType.TEAMS, text=TEXT, metadata={"conversation_type": "groupChat"})

    assert await router._load_channel_backfill(inbound) == "Earlier: the VPN alert"
    assert any(r.getMessage().startswith("Loaded cached Teams chat backfill for (none)") for r in _records(caplog))


@pytest.mark.asyncio
async def test_a_whatsapp_thread_is_logged_by_the_router_without_the_number(monkeypatch, caplog):
    """A WhatsApp direct message is threaded by the sender's phone number."""
    caplog.set_level(logging.DEBUG, logger=ROUTER_LOGGER)
    router = _route(monkeypatch, bot=_bot(allow_anonymous=False), user=None)
    inbound = InboundMessage(
        channel=ChannelType.WHATSAPP,
        text=TEXT,
        channel_user_id=PHONE,
        channel_thread_id=PHONE,
        metadata={"chat_type": "private", "contact_name": NAME},
    )

    await router.handle_message(inbound)

    assert _records(caplog)
    assert _leaks(caplog) == []
    reference = router._log_thread_id(inbound)
    assert reference.startswith("wa-") and PHONE not in reference
    assert reference == router._log_thread_id(inbound), "one chat, one reference"
    assert any(reference in r.getMessage() for r in _records(caplog))


class _Redis:
    """A Redis that holds nothing and grants no lock."""

    async def set(self, *_args, **_kwargs):
        return False

    async def exists(self, *_args):
        return 0

    async def delete(self, *_args):
        return 0

    async def get(self, *_args):
        return None


async def _contended_lock(router) -> None:
    async with router._conversation_create_lock(ChannelType.WHATSAPP, PHONE, None):
        pass


async def _duplicate_identity(router) -> None:
    users = [SimpleNamespace(id=uuid.uuid4()), SimpleNamespace(id=uuid.uuid4())]
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: users))
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    assert await router.resolve_user_by_channel_identity(db, "whatsapp", PHONE) is users[0]


async def _group_thread_state(router) -> None:
    await router._mark_thread_active(ChannelType.WHATSAPP, GROUP_ID)
    await router._is_thread_active(ChannelType.WHATSAPP, GROUP_ID)
    await router._mute_thread(ChannelType.WHATSAPP, GROUP_ID)
    await router._is_thread_muted(ChannelType.WHATSAPP, GROUP_ID)
    await router._unmute_thread(ChannelType.WHATSAPP, GROUP_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "redis"),
    [
        (_contended_lock, _Redis()),
        (_duplicate_identity, _Redis()),
        (_group_thread_state, _Redis()),
        (_group_thread_state, ConnectionError("Redis is unreachable")),
    ],
    ids=["contended_conversation_lock", "duplicate_channel_identity", "group_thread_state", "group_thread_redis_down"],
)
async def test_a_key_the_router_derives_from_a_whatsapp_id_is_logged_without_it(monkeypatch, caplog, scenario, redis):
    """A lock, an identity or a thread key built from a number or a group id, at any level."""
    from app.channels import router

    caplog.set_level(logging.DEBUG, logger=ROUTER_LOGGER)
    monkeypatch.setattr(router, "asyncio", SimpleNamespace(sleep=AsyncMock()))
    if isinstance(redis, Exception):
        monkeypatch.setattr(router, "redis_client", AsyncMock(side_effect=redis))
    else:
        monkeypatch.setattr(router, "redis_client", AsyncMock(return_value=redis))

    await scenario(router)

    rendered = [_rendered(r) for r in _records(caplog)]
    assert rendered, "the scenario logs something"
    assert [line for line in rendered if PHONE in line or GROUP_ID in line] == []
    assert any("wa-" in line for line in rendered), "the line still says which chat"


def test_a_licence_refusal_names_the_sender_by_a_loggable_id(caplog):
    """A Slack member id is logged as it is; a WhatsApp sender is their number."""
    from app.channels import router

    caplog.set_level(logging.DEBUG, logger=ROUTER_LOGGER)
    router._licence_refusal_outbound(_slack())
    router._licence_refusal_outbound(
        InboundMessage(channel=ChannelType.WHATSAPP, text=TEXT, channel_user_id=PHONE, channel_thread_id=PHONE)
    )

    slack, whatsapp = (r.getMessage() for r in _records(caplog))
    assert slack == "AI is not licensed: refusing inbound slack message from U12345"
    assert whatsapp.startswith("AI is not licensed: refusing inbound whatsapp message from wa-")
    assert PHONE not in whatsapp


def _http(status: int, content: bytes = b"%PDF-1.7") -> type:
    """An ``httpx.AsyncClient`` answering every GET, plain or streamed, with *status*."""

    class _Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, url, params=None, headers=None):
            return httpx.Response(status, content=content, request=httpx.Request("GET", url, params=params))

        @contextlib.asynccontextmanager
        async def stream(self, method, url, headers=None):
            # A body a real client streams: one built from ``content=`` is already read.
            yield httpx.Response(status, stream=httpx.ByteStream(content), request=httpx.Request(method, url))

    return _Client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "limit"),
    [(200, None), (200, 1), (404, None)],
    ids=["resolved", "too_large", "download_failed"],
)
async def test_a_channel_file_is_logged_without_its_name_or_its_url(monkeypatch, caplog, status, limit):
    """The sender named the file, which the router and the storage it uploads through
    both log, and a Telegram file URL carries the bot token."""
    from app.channels import router
    from app.channels.base import InboundFileAttachment
    from app.services import file_storage

    caplog.set_level(logging.DEBUG, logger="app")
    monkeypatch.setattr(httpx, "AsyncClient", _http(status))
    monkeypatch.setattr(file_storage.file_storage, "_client", SimpleNamespace(put_object=lambda *_args: None))
    if limit is not None:
        monkeypatch.setattr(file_storage, "MAX_UPLOAD_SIZE_BYTES", limit)
    attachment = InboundFileAttachment(
        url=f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/documents/file_7.pdf",
        filename=f"payslip.{NAME}",
        content_type="application/pdf",
    )
    inbound = InboundMessage(channel=ChannelType.TELEGRAM, text=TEXT, file_attachments=[attachment])
    db = SimpleNamespace(add=lambda _row: None, flush=AsyncMock())

    refs = await router._resolve_channel_files(db, inbound, None)

    assert len(refs) == (1 if status == 200 and limit is None else 0)
    assert _records(caplog), "the attachment is logged"
    assert _leaks(caplog, TELEGRAM_TOKEN, "payslip", loggers="app.") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "suffix"),
    [
        ("report.PDF", ".pdf"),
        ("minutes.docx", ".docx"),
        ("backup.tar.gz", ".gz"),
        ("notes", ""),
        (f"payslip.{NAME}", ""),
        ("cv.JaneDoe", ""),
        ("scan.p\u00e9df", ""),
        ("report.\u212a", ""),
    ],
    ids=[
        "extension",
        "office_extension",
        "last_extension",
        "no_extension",
        "sender_text",
        "alphanumeric_sender_text",
        "non_ascii",
        "lowercases_to_ascii",
    ],
)
async def test_an_object_key_keeps_a_known_extension_only(monkeypatch, filename, suffix):
    """Storage logs the object key it writes, so its suffix is one the platform knows, never the sender's text."""
    from app.services import file_storage

    monkeypatch.setattr(file_storage.file_storage, "_client", SimpleNamespace(put_object=lambda *_args: None))

    key = await file_storage.file_storage.upload(data=b"%PDF", filename=filename, prefix="channel/telegram")

    assert re.fullmatch(r"channel/telegram/[0-9a-f]{32}" + re.escape(suffix), key)


@pytest.mark.asyncio
async def test_a_telegram_file_that_cannot_be_resolved_is_logged_without_the_bot_token(monkeypatch, caplog):
    from app.channels import telegram

    caplog.set_level(logging.DEBUG, logger="app.channels")
    spawned = _adapter(monkeypatch, telegram)
    monkeypatch.setattr(telegram, "TelegramChannel", _FakeChannel)
    monkeypatch.setattr(
        telegram,
        "_get_telegram_creds",
        AsyncMock(return_value={"token": TELEGRAM_TOKEN, "webhook_secret_token": "", "bot_username": "copilot_bot"}),
    )
    monkeypatch.setattr(httpx, "AsyncClient", _http(400))
    payload = {
        "message": {
            "message_id": 8,
            "text": TEXT,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42},
            "document": {"file_id": "BQACAgQ", "file_name": "payslip.pdf", "mime_type": "application/pdf"},
        }
    }

    await telegram.telegram_webhook(_request("/api/channels/telegram/webhook", payload))
    await _run(spawned)

    assert any(r.getMessage().startswith("Failed to resolve Telegram file") for r in caplog.records)
    assert _leaks(caplog, TELEGRAM_TOKEN) == []


def test_a_debug_preview_is_bounded():
    preview = log_preview("x" * 500)
    assert preview.endswith("...") and len(preview) < 100
    assert log_preview(None) == "(empty)"


def test_a_whatsapp_id_is_logged_as_a_keyed_digest_and_any_other_id_as_it_is(monkeypatch):
    from app.channels import router
    from app.channels.base import log_id
    from app.core.config import settings

    monkeypatch.setattr(settings, "secret_key", "k" * 32)
    reference = log_id(ChannelType.WHATSAPP, PHONE)
    assert reference.startswith("wa-") and len(reference) == 19 and PHONE not in reference
    assert log_id(ChannelType.WHATSAPP, PHONE) == reference, "one number, one reference"
    assert log_id(ChannelType.SLACK, "U12345") == "U12345"
    assert log_id(ChannelType.WHATSAPP, None) == log_id(ChannelType.TEAMS, "") == "(none)"
    no_thread = InboundMessage(channel=ChannelType.TEAMS, text=TEXT)
    assert router._log_thread_id(no_thread) == router._log_user_id(no_thread) == "(none)", "a str a line can slice"
    monkeypatch.setattr(settings, "secret_key", "q" * 32)
    assert log_id(ChannelType.WHATSAPP, PHONE) != reference, "keyed: hashing candidate numbers does not find it"


@pytest.mark.parametrize(
    "secret",
    ["", "change-me-in-production-please", "change-me-to-a-random-string-in-production"],
    ids=["empty", "compose_default", "env_sample_default"],
)
def test_a_guessable_secret_key_never_keys_the_whatsapp_digest(monkeypatch, secret):
    """Anyone can read a published ``SECRET_KEY``: a digest under it would give the number back."""
    from app.channels import base as channel_base
    from app.core.config import settings

    monkeypatch.setattr(settings, "secret_key", secret)
    reference = channel_base.log_id(ChannelType.WHATSAPP, PHONE)
    assert channel_base.log_id(ChannelType.WHATSAPP, PHONE) == reference, "stable within the process"
    monkeypatch.setattr(channel_base, "secret_key_is_guessable", lambda _secret: False)
    under_the_secret = channel_base.log_id(ChannelType.WHATSAPP, PHONE)
    plain = hmac.new(secret.encode(), PHONE.encode(), hashlib.sha256).hexdigest()[:16]
    assert reference[3:] not in (under_the_secret[3:], plain)


def test_every_secret_key_placeholder_the_repository_publishes_is_guessable():
    """``.env.sample``, the Compose file and the CI workflows publish a ``SECRET_KEY`` for anyone to read."""
    from app.core.security import secret_key_is_guessable

    root = Path(__file__).resolve().parents[2]
    files = [root / ".env.sample", root / "docker-compose.yml", *sorted((root / ".github" / "workflows").glob("*.yml"))]
    placeholder = re.compile(r'\bSECRET_KEY(?:=|: )"?(?:\$\{SECRET_KEY:-)?([^"}\s]+)')
    published = [value for path in files for value in placeholder.findall(path.read_text(encoding="utf-8"))]
    assert len(published) >= 3, "the placeholders are found"
    assert [value for value in published if not secret_key_is_guessable(value)] == []


# The AST half: what no test drives (Discord's gateway handler, error paths)
# is held to the same rule. An argument of a logger call at INFO or above may
# not read message text, a personal field or an attachment's name or URL,
# except through ``len()``, ``bool()`` or the URL's host; in the router and
# the WhatsApp adapter, an id or a key built from one goes through ``log_id()``.

_APP = Path(__file__).resolve().parents[1] / "app"
_CHANNELS = _APP / "channels"
# What ``log_preview()`` may show at DEBUG: message text, and nothing else.
_MESSAGE_TEXT_NAMES = {"text", "original_text", "raw_text", "mention_text", "cmd_text", "entity_text"}
_MESSAGE_TEXT_ATTRIBUTES = {"text"}
_PERSONAL_NAMES = _MESSAGE_TEXT_NAMES | {
    "from_name", "user_name", "sender_name", "contact_name", "first_name", "last_name",
    "username", "slack_display_name", "display_name", "from_number", "filename", "s3_key",
    "email", "user_email", "sender_email",
}
_MESSAGE_OBJECTS = {"inbound", "message", "msg", "event", "activity", "from_user", "sender", "att"}
_PERSONAL_ATTRIBUTES = _MESSAGE_TEXT_ATTRIBUTES | {"user_email", "display_name", "author", "filename", "url"}
# A WhatsApp user id and direct-message thread are the sender's number, a
# message id carries it, and every key the router builds from them does too.
_WHATSAPP_ID_FILES = {"router.py", "whatsapp.py"}
_WHATSAPP_ID_NAMES = {
    "channel_user_id", "channel_thread_id", "msg_id", "group_id",
    "thread_key", "_thread_key", "_mute_thread_key", "key", "active_key", "muted_key", "identity_key",
}
_WHATSAPP_ID_ATTRIBUTES = {"channel_user_id", "channel_thread_id", "channel_message_id"}
_LOGGABLE = {"len", "bool", "log_id", "_log_thread_id", "_log_user_id", "_log_url_host"}


def _log_arguments(path: Path, *, debug: bool = False):
    """``(line, argument)`` of every logger call in *path* at INFO or above, or at DEBUG."""
    for call in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "logger"
        ):
            continue
        args, at_debug = call.args[1:], call.func.attr == "debug"
        if call.func.attr == "log":
            level = call.args[0] if call.args else None
            args, at_debug = call.args[2:], isinstance(level, ast.Attribute) and level.attr == "DEBUG"
        if at_debug != debug:
            continue
        for value in [*args, *(k.value for k in call.keywords)]:
            yield call.lineno, value


def _reads(
    node: ast.AST,
    names: set[str],
    attributes: set[str],
    receivers: set[str] | None,
    *,
    preview: bool = False,
) -> list[str]:
    """What *node* reads of *names*, or of *attributes* on *receivers* (``None``: on anything).

    ``preview``: message text read through ``log_preview()`` does not count.
    """
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in _LOGGABLE:
            return []
        if preview and node.func.id == "log_preview":
            names, attributes = names - _MESSAGE_TEXT_NAMES, attributes - _MESSAGE_TEXT_ATTRIBUTES
    found = []
    if isinstance(node, ast.Name) and node.id in names:
        found.append(node.id)
    if isinstance(node, ast.Attribute) and node.attr in attributes:
        receiver = node.value.id if isinstance(node.value, ast.Name) else None
        if receivers is None or receiver in receivers:
            found.append(f"{receiver or '...'}.{node.attr}")
    for child in ast.iter_child_nodes(node):
        found += _reads(child, names, attributes, receivers, preview=preview)
    return found


def _logged_personal_data(path: Path, *, debug: bool = False) -> list[str]:
    """At DEBUG, message text is allowed through ``log_preview()``; a WhatsApp id never is raw."""
    names, attributes = set(_PERSONAL_NAMES), set(_PERSONAL_ATTRIBUTES)
    if path.name in _WHATSAPP_ID_FILES:
        names |= _WHATSAPP_ID_NAMES
        attributes |= _WHATSAPP_ID_ATTRIBUTES
    return [
        f"{path.name}:{line} {read}"
        for line, value in _log_arguments(path, debug=debug)
        for read in _reads(value, names, attributes, _MESSAGE_OBJECTS, preview=debug)
    ]


def test_the_tripwire_allows_log_preview_message_text_only(tmp_path):
    """``log_preview()`` bounds message text; it does not make a name, an email or a number loggable."""
    source = tmp_path / "whatsapp.py"
    source.write_text(
        "logger.debug('%s', log_preview(text))\n"
        "logger.debug('%s', log_preview(inbound.text))\n"
        "logger.debug('%s', log_preview(channel_user_id))\n"
        "logger.debug('%s', log_preview(contact_name))\n"
        "logger.info('%s', log_preview(text))\n",
        encoding="utf-8",
    )

    assert _logged_personal_data(source, debug=True) == ["whatsapp.py:3 channel_user_id", "whatsapp.py:4 contact_name"]
    assert _logged_personal_data(source) == ["whatsapp.py:5 text"]


@pytest.mark.parametrize("debug", [False, True], ids=["info_and_above", "debug"])
def test_no_channel_log_call_reads_message_text_or_personal_data(debug):
    offenders = [o for path in sorted(_CHANNELS.rglob("*.py")) for o in _logged_personal_data(path, debug=debug)]
    assert offenders == [], (
        "Channel log calls that write message text or personal data:\n  "
        + "\n  ".join(offenders)
        + "\nLog the length or has_* instead, a bounded log_preview() at DEBUG only, "
        "and a WhatsApp id through log_id() (ROUTING.md section 14)."
    )


def test_no_chat_runtime_log_call_reads_an_email():
    """Every channel turn runs one of them: a log line names a user by id."""
    offenders = [
        f"{path.name}:{line} {read}"
        for path in (_APP / "services" / "agent_chat.py", _APP / "services" / "general_chat.py")
        for line, value in _log_arguments(path)
        for read in _reads(value, {"email", "user_email"}, {"email", "user_email"}, None)
    ]
    assert offenders == [], "Chat runtime log calls that write an email:\n  " + "\n  ".join(offenders)
