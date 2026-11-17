"""Inbound webhook of the WhatsApp linked-device gateway backend."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.channels import whatsapp as whatsapp_channel
from app.integrations.providers import whatsapp, whatsapp_gateway
from app.integrations.providers.whatsapp_gateway import gateway_token
from app.services import whatsapp_gateway_inbound, whatsapp_inbound
from app.services.agent_accounts.providers import whatsapp as wa_accounts
from app.services.integration_tools import _SafeIntegrationExecutor
from app.services.whatsapp_gateway_inbound import (
    GatewayIntegration,
    PollUpdate,
    apply_poll_update,
    parse_whapi,
    tally_of,
)

_GROUP = "120363025555555555@g.us"
_POLL = {
    "id": "POLL1",
    "from_me": True,
    "type": "poll",
    "chat_id": _GROUP,
    "timestamp": 1747730565,
    "poll": {
        "title": "Board date",
        "options": ["Mon", "Tue"],
        "results": [
            {"name": "Mon", "voters": [], "count": 0, "id": "o1"},
            {"name": "Tue", "voters": [], "count": 0, "id": "o2"},
        ],
    },
}


def _vote(voter: str, votes: list[str], msg_id: str = "V1") -> dict:
    return {
        "id": msg_id,
        "from_me": False,
        "type": "action",
        "chat_id": _GROUP,
        "timestamp": 1747730574,
        "action": {"target": "POLL1", "type": "vote", "votes": votes},
        "from": voter,
        "from_name": "Pedro",
    }


# ── Parsing ─────────────────────────────────────────────────────────────────


def test_text_message_becomes_a_private_row():
    d = parse_whapi(
        {
            "messages": [
                {
                    "id": "M1",
                    "from_me": False,
                    "type": "text",
                    "chat_id": "33611111111@s.whatsapp.net",
                    "timestamp": 1713201613,
                    "text": {"body": "hello"},
                    "from": "33611111111",
                    "from_name": "Gerald",
                }
            ],
            "event": {"type": "messages", "event": "post"},
        }
    )
    assert d.polls == []
    (row,) = d.rows
    assert row["wamid"] == "M1"
    assert row["conversation_id"] == "33611111111"
    assert row["chat_type"] == "private"
    assert row["sender"] == "33611111111" and row["sender_name"] == "Gerald"
    assert row["text"] == "hello" and row["message_type"] == "text"


def test_own_messages_are_not_stored_but_own_polls_are_tallied():
    d = parse_whapi({"messages": [_POLL, {"id": "M2", "from_me": True, "type": "text", "chat_id": _GROUP}]})
    assert d.rows == []
    (poll,) = d.polls
    assert poll.poll_id == "POLL1" and poll.conversation_id == _GROUP and poll.title == "Board date"
    assert [(o["id"], o["name"]) for o in poll.options] == [("o1", "Mon"), ("o2", "Tue")]


def test_received_poll_is_stored_in_words():
    d = parse_whapi({"messages": [dict(_POLL, from_me=False, **{"from": "33622222222"})]})
    (row,) = d.rows
    assert row["chat_type"] == "group" and row["message_type"] == "poll"
    assert row["text"] == "[poll] Board date: Mon / Tue"


def test_vote_webhook_names_option_and_voter():
    d = parse_whapi({"messages": [_vote("33633333333", ["o2"])]})
    assert d.rows == []  # a vote is a tally change, not a message
    (vote,) = d.polls
    assert (vote.poll_id, vote.voter, vote.votes, vote.options) == ("POLL1", "33633333333", ["o2"], None)


def test_poll_patch_carries_the_whole_tally():
    after = dict(_POLL)
    after["poll"] = dict(
        _POLL["poll"],
        results=[
            {"name": "Mon", "id": "o1", "count": 1, "voters": [{"id": "33611111111@s.whatsapp.net"}]},
            {"name": "Tue", "id": "o2", "count": 0, "voters": []},
        ],
    )
    d = parse_whapi(
        {
            "messages_updates": [{"id": "POLL1", "trigger": _vote("33611111111", ["o1"]), "after_update": after}],
            "event": {"type": "messages", "event": "patch"},
        }
    )
    (poll,) = d.polls
    assert poll.options[0]["voters"] == ["33611111111"]
    assert tally_of(poll.title, poll.options)["total"] == 1


def test_group_events_in_words():
    d = parse_whapi(
        {
            "messages": [
                {
                    "id": "A1",
                    "type": "action",
                    "chat_id": _GROUP,
                    "from": "33600000000",
                    "from_name": "Admin",
                    "action": {"type": "add", "participants": ["33644444444@s.whatsapp.net"]},
                }
            ],
            "groups_participants": [
                {"group_id": _GROUP, "participants": ["33655555555"], "action": "remove", "timestamp": 1700000000}
            ],
        }
    )
    texts = [r["text"] for r in d.rows]
    assert texts == ["[group event] Admin added 33644444444", "[group event] someone removed 33655555555"]
    assert d.rows[1]["message_type"] == "group_event" and d.rows[1]["chat_type"] == "group"


def test_malformed_payloads_are_ignored():
    assert parse_whapi(None).rows == []
    assert parse_whapi({"messages": [None, {"id": ""}, {"id": "x"}], "messages_updates": [1]}).rows == []


# ── Tally ───────────────────────────────────────────────────────────────────


def test_a_vote_replaces_the_voters_previous_choice():
    stored = apply_poll_update(
        None, PollUpdate(poll_id="POLL1", options=[{"id": "o1", "name": "Mon"}, {"id": "o2", "name": "Tue"}])
    )
    stored = apply_poll_update(stored, PollUpdate(poll_id="POLL1", voter="A", votes=["o1"]))
    stored = apply_poll_update(stored, PollUpdate(poll_id="POLL1", voter="B", votes=["o1"]))
    stored = apply_poll_update(stored, PollUpdate(poll_id="POLL1", voter="A", votes=["o2"]))
    tally = tally_of("Board date", stored)
    assert tally["total"] == 2
    assert [(o["id"], o["voters"]) for o in tally["options"]] == [("o1", ["B"]), ("o2", ["A"])]
    # withdrawn vote
    stored = apply_poll_update(stored, PollUpdate(poll_id="POLL1", voter="B", votes=[]))
    assert tally_of("t", stored)["total"] == 1


def test_a_vote_before_the_poll_is_kept_by_option_id():
    stored = apply_poll_update(None, PollUpdate(poll_id="P", voter="A", votes=["o9"]))
    assert stored == [{"id": "o9", "name": "", "voters": ["A"]}]


# ── Routing per backend ─────────────────────────────────────────────────────


def _gateway(secret: str = "s3cret") -> GatewayIntegration:  # noqa: S107 - test fixture
    return GatewayIntegration(
        target=whatsapp_inbound.InboundIntegration(
            integration_id=uuid.uuid4(), app_secret="", verify_token="", retention_days=30
        ),
        vendor="whapi",
        webhook_secret=secret,
    )


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(whatsapp_channel.router, prefix="/api/channels")
    return TestClient(app)


def _route(monkeypatch, gateway):
    stored: list = []

    async def _lookup(_integration_id):
        return gateway

    async def _record(gw, payload):
        stored.append((gw, payload))
        return 1, 0

    monkeypatch.setattr(whatsapp_gateway_inbound, "gateway_integration", _lookup)
    monkeypatch.setattr(whatsapp_gateway_inbound, "record_delivery", _record)
    return stored


def test_webhook_stores_with_the_right_secret(client, monkeypatch):
    gw = _gateway()
    stored = _route(monkeypatch, gw)
    url = f"/api/channels/whatsapp/gateway/{gw.target.integration_id}"
    assert client.post(url + "?token=s3cret", json={"messages": []}).json()["messages"] == 1
    assert client.post(url, json={}, headers={"X-Webhook-Secret": "s3cret"}).status_code == 200
    assert len(stored) == 2 and stored[0][0] is gw


@pytest.mark.parametrize("secret,token", [("s3cret", "wrong"), ("s3cret", ""), ("", ""), ("", "anything")])
def test_webhook_refuses_without_the_secret(client, monkeypatch, secret, token):
    gw = _gateway(secret)
    stored = _route(monkeypatch, gw)
    resp = client.post(f"/api/channels/whatsapp/gateway/{gw.target.integration_id}?token={token}", json={})
    assert resp.status_code == 401 and stored == []


def test_webhook_refuses_a_meta_or_unknown_integration(client, monkeypatch):
    stored = _route(monkeypatch, None)
    resp = client.post(f"/api/channels/whatsapp/gateway/{uuid.uuid4()}?token=x", json={})
    assert resp.status_code == 404 and stored == []


def test_webhook_url_opened_in_a_browser_says_post_only(client):
    resp = client.get(f"/api/channels/whatsapp/gateway/{uuid.uuid4()}")
    assert resp.status_code == 405 and resp.headers["allow"] == "POST"
    assert "POST only" in resp.json()["detail"]


# ── Integration lookup ──────────────────────────────────────────────


class _Session:
    def __init__(self, row):
        self.row, self.statements = row, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        self.statements.append(statement)
        row = self.row
        return type("R", (), {"scalar_one_or_none": lambda _self: row})()


def _row(*, status="connected", provider="whatsapp", extra=None):
    from app.core.security import encrypt_value

    extra = {"backend": "linked_device_gateway", "webhook_secret": "s3cret"} if extra is None else extra
    return SimpleNamespace(
        id=uuid.uuid4(),
        provider=provider,
        status=status,
        extra_config_enc=encrypt_value(json.dumps(extra)),
        # The channel token and the owner: a voice note is downloaded and transcribed with them.
        api_key_enc=encrypt_value("channel-token"),
        user_id=uuid.uuid4(),
    )


def _lookup_with(monkeypatch, row):
    session = _Session(row)
    monkeypatch.setattr(whatsapp_gateway_inbound, "async_session", lambda: session)
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["connected", "not_connected", "error", "disconnected"])
async def test_lookup_does_not_depend_on_the_stored_status(monkeypatch, status):
    # The tool build of an api_key integration ignores the stored status; the
    # webhook refused a gateway whose tools worked.
    row = _row(status=status)
    session = _lookup_with(monkeypatch, row)
    gw = await whatsapp_gateway_inbound.gateway_integration(str(row.id))
    assert gw is not None and gw.webhook_secret == "s3cret" and gw.target.integration_id == row.id
    assert "status" not in str(session.statements[0].whereclause)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row,reason",
    [
        (None, "no integration with this id"),
        (_row(provider="slack"), "provider is 'slack'"),
        (_row(extra={"backend": "meta_cloud_api"}), "backend is 'meta_cloud_api'"),
        (_row(extra={}), "backend is 'unset'"),
    ],
)
async def test_a_miss_is_logged_with_its_reason(monkeypatch, caplog, row, reason):
    _lookup_with(monkeypatch, row)
    integration_id = str(row.id) if row else str(uuid.uuid4())
    with caplog.at_level("WARNING", logger=whatsapp_gateway_inbound.__name__):
        assert await whatsapp_gateway_inbound.gateway_integration(integration_id) is None
    assert any(reason in r.getMessage() and integration_id in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_malformed_id_is_logged(caplog):
    with caplog.at_level("WARNING", logger=whatsapp_gateway_inbound.__name__):
        assert await whatsapp_gateway_inbound.gateway_integration("not-a-uuid") is None
    assert any("not an integration id" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_record_delivery_writes_rows_and_polls(monkeypatch):
    calls: dict = {}

    async def _rows(target, rows):
        calls["rows"] = rows
        return len(rows)

    async def _polls(target, updates):
        calls["polls"] = updates
        return len(updates)

    monkeypatch.setattr(whatsapp_inbound, "record_rows", _rows)
    monkeypatch.setattr(whatsapp_gateway_inbound, "record_polls", _polls)
    payload = {"messages": [dict(_POLL, from_me=False, **{"from": "33622222222"}), _vote("33633333333", ["o1"])]}
    assert await whatsapp_gateway_inbound.record_delivery(_gateway(), payload) == (1, 2)
    assert calls["rows"][0]["message_type"] == "poll"


@pytest.mark.asyncio
async def test_unknown_vendor_stores_nothing():
    gw = GatewayIntegration(target=_gateway().target, vendor="other", webhook_secret="s")
    assert await whatsapp_gateway_inbound.record_delivery(gw, {"messages": []}) == (0, 0)


def test_group_events_are_not_answered_by_agent_accounts():
    assert "group_event" in wa_accounts._NOT_MESSAGES


# ── Tools on the gateway backend ────────────────────────────────────────────


def _gateway_handlers(integration_id: str = ""):
    executor = _SafeIntegrationExecutor()
    tools = whatsapp.build_tools(gateway_token("whapi", "", "channel-token"), executor, integration_id=integration_id)
    return executor._handlers, {t.name for t in tools}


def test_gateway_backend_has_the_inbound_read_tools():
    handlers, names = _gateway_handlers()
    assert {"list_whatsapp_conversations", "read_whatsapp_messages"} <= names
    assert {"list_whatsapp_conversations", "read_whatsapp_messages"} <= set(handlers)


@pytest.mark.asyncio
async def test_poll_results_read_the_store_first(monkeypatch):
    async def _stored(integration_id, poll_id):
        return tally_of("Board date", [{"id": "o1", "name": "Mon", "voters": ["A", "B"]}])

    async def _never(*a, **k):  # the gateway must not be called
        raise AssertionError("gateway called")

    monkeypatch.setattr(whatsapp_gateway_inbound, "stored_tally", _stored)
    monkeypatch.setattr(whatsapp_gateway, "safe_request", _never)
    handlers, _ = _gateway_handlers(str(uuid.uuid4()))
    out = await handlers["get_whatsapp_poll_results"](message_id="POLL1")
    assert "Mon (option o1): 2 vote(s), by A, B" in out


@pytest.mark.asyncio
async def test_gateway_conversations_read_the_shared_store(monkeypatch):
    seen = {}

    async def _text(integration_id, limit, empty):
        seen["id"] = integration_id
        return empty

    monkeypatch.setattr(whatsapp_inbound, "conversations_text", _text)
    iid = str(uuid.uuid4())
    handlers, _ = _gateway_handlers(iid)
    out = await handlers["list_whatsapp_conversations"]()
    assert seen["id"] == iid and "webhook_secret" in out
