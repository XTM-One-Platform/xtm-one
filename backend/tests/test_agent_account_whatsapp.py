"""WhatsApp agent-account provider."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.agent_accounts.providers import ProviderError, ProviderSession, get_provider, providers
from app.services.agent_accounts.providers import whatsapp as wa

INTEGRATION = str(uuid.uuid4())
PHONE_ID = "1234567890"


def _session(**config: str) -> ProviderSession:
    return ProviderSession(
        token="token",
        account_external_id=PHONE_ID,
        config={"phone_number_id": PHONE_ID, "integration_id": INTEGRATION, **config},
    )


class _Calls:
    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str, dict | None, dict | None]] = []

    async def __call__(self, session, method, url, *, payload=None, params=None):
        self.calls.append((method, url, payload, params))
        return self.answer


def test_registered_with_api_key_credential() -> None:
    provider = get_provider("whatsapp")
    assert provider is not None and "whatsapp" in providers()
    assert provider.integration_provider == "whatsapp"
    assert provider.credential == "api_key"
    assert provider.session_config_keys == ("phone_number_id", "backend", "gateway_vendor", "base_url")
    # The Cloud API needs its phone number id; a linked-device gateway does not.
    assert provider.required_config_keys({"backend": "meta_cloud_api"}) == ("phone_number_id",)
    assert provider.required_config_keys({}) == ("phone_number_id",)
    assert provider.required_config_keys({"backend": "linked_device_gateway"}) == ()
    caps = provider.capabilities
    assert caps.direct and caps.group and not caps.channel and not caps.push and not caps.poll
    # The Microsoft providers keep their OAuth session.
    assert get_provider("microsoft_teams").credential == "oauth"


def test_session_copy_keeps_config() -> None:
    session = _session()
    copy = session.as_account("other")
    assert copy.config == session.config and copy.config is not session.config
    assert copy.account_external_id == "other"


async def test_identify_reads_the_verified_name(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = wa.WhatsAppProvider()
    fake = _Calls({"verified_name": "Filigran Support", "display_phone_number": "+33 1 23"})
    monkeypatch.setattr(provider, "_call", fake)
    identity = await provider.identify(_session())
    assert identity.external_id == PHONE_ID
    assert identity.display_name == "Filigran Support"
    assert fake.calls[0][1].endswith(f"/{PHONE_ID}")


class _Gateway:
    """A gateway client double: the health it answers, the texts it sends."""

    def __init__(self, health: dict[str, Any], err: str | None = None) -> None:
        self._health, self._err = health, err
        self.sent: list[tuple[str, str]] = []

    def pairing_state(self, health: dict[str, Any]) -> str:
        return "connected" if (health.get("status") or {}).get("text") == "AUTH" else "qr"

    async def health(self):
        return self._health, self._err

    async def send_text(self, chat: str, text: str):
        self.sent.append((chat, text))
        return {"sent": True, "message": {"id": "gw-out"}}, None


def _gateway_session() -> ProviderSession:
    return ProviderSession(
        token="channel-token",
        config={"backend": "linked_device_gateway", "gateway_vendor": "whapi", "integration_id": INTEGRATION},
    )


async def test_identify_on_the_gateway_reads_the_paired_number(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = wa.WhatsAppProvider()
    gateway = _Gateway({"status": {"text": "AUTH"}, "user": {"id": "33612345678", "name": "Samuel"}})
    monkeypatch.setattr(provider, "_gateway", lambda session: gateway)
    # No Graph API call on the gateway backend.
    monkeypatch.setattr(provider, "_call", _Calls({}))
    identity = await provider.identify(_gateway_session())
    assert identity.external_id == "33612345678" and identity.display_name == "Samuel"
    assert provider._call.calls == []


async def test_identify_on_an_unpaired_gateway_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = wa.WhatsAppProvider()
    monkeypatch.setattr(provider, "_gateway", lambda session: _Gateway({"status": {"text": "QR"}}))
    with pytest.raises(ProviderError) as raised:
        await provider.identify(_gateway_session())
    assert raised.value.kind == "reauthorize"
    assert "not paired (state qr, linked-device gateway backend)" in str(raised.value)
    monkeypatch.setattr(provider, "_gateway", lambda session: _Gateway({}, err="WhatsApp gateway answered 401"))
    with pytest.raises(ProviderError) as raised:
        await provider.identify(_gateway_session())
    assert "answered 401" in str(raised.value)


async def test_send_reply_on_the_gateway_goes_through_it(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = wa.WhatsAppProvider()
    gateway = _Gateway({})
    monkeypatch.setattr(provider, "_gateway", lambda session: gateway)
    message_id = await provider.send_reply(_gateway_session(), {"to": "120363404393005709@g.us"}, "hi")
    assert message_id == "gw-out" and gateway.sent == [("120363404393005709@g.us", "hi")]
    with pytest.raises(ProviderError):
        await provider.send_reply(_gateway_session(), {"to": "not a chat"}, "hi")


def test_gateway_client_from_the_session() -> None:
    client = wa.WhatsAppProvider()._gateway(_gateway_session())
    assert client.base_url == "https://gate.whapi.cloud"
    assert client.headers["Authorization"] == "Bearer channel-token"
    with pytest.raises(ProviderError):
        wa.WhatsAppProvider()._gateway(ProviderSession(token="t", config={"gateway_vendor": "other"}))


async def test_identify_refuses_a_missing_phone_number_id() -> None:
    with pytest.raises(ProviderError) as raised:
        await wa.WhatsAppProvider().identify(_session(phone_number_id=""))
    # The technical detail names the backend it checked.
    assert "checked for the Cloud API backend" in str(raised.value)


def test_the_backend_checked_is_named() -> None:
    provider = wa.WhatsAppProvider()
    # The stored backend value read is named: an empty one shows a lost selector (#9903).
    assert provider.config_label({}) == "the Cloud API backend; stored backend: empty"
    assert provider.config_label({"backend": ""}) == "the Cloud API backend; stored backend: empty"
    assert (
        provider.config_label({"backend": "meta_cloud_api"})
        == "the Cloud API backend; stored backend: 'meta_cloud_api'"
    )
    assert provider.config_label({"backend": "linked_device_gateway"}) == "the linked-device gateway backend"
    # Both backends hear through the integration's webhook, no subscription.
    assert provider.capabilities.inbound_webhook


async def test_verify_session_checks_the_pairing_on_the_gateway_only(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = wa.WhatsAppProvider()
    monkeypatch.setattr(provider, "_gateway", lambda session: _Gateway({"status": {"text": "QR"}}))
    with pytest.raises(ProviderError) as raised:
        await provider.verify_session(_gateway_session())
    assert raised.value.kind == "reauthorize" and "linked-device gateway backend" in str(raised.value)
    paired = _Gateway({"status": {"text": "AUTH"}, "user": {"id": "33612345678"}})
    monkeypatch.setattr(provider, "_gateway", lambda session: paired)
    await provider.verify_session(_gateway_session())
    # The Cloud API backend makes no call at every sync.
    monkeypatch.setattr(provider, "_gateway", lambda session: pytest.fail("no gateway on the Cloud API"))
    await provider.verify_session(_session())


async def _sync_with(monkeypatch: pytest.MonkeyPatch, session: ProviderSession | Exception) -> Any:
    """Run one ``_sync_account`` of a WhatsApp account answering chats, the session given or refused."""
    from app.services.agent_accounts import listening
    from app.services.agent_accounts.rules import ListeningPlan

    async def _open(db, account, provider):
        if isinstance(session, Exception):
            raise session
        return session

    async def _no_subscriptions(db, account, provider, session, wanted):
        assert wanted == []
        return set(), []

    async def _settle(db, account, result):
        return result

    monkeypatch.setattr(listening, "open_session", _open)
    monkeypatch.setattr(listening, "_sync_subscriptions", _no_subscriptions)
    monkeypatch.setattr(listening, "_settle", _settle)
    monkeypatch.setattr(listening, "listening_plan", lambda settings: ListeningPlan(chats=True))
    account = SimpleNamespace(id=uuid.uuid4(), provider="whatsapp", account_external_id="33612345678")
    return await listening._sync_account(None, account, poll=True)


async def test_a_paired_gateway_account_listens_in_real_time(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = get_provider("whatsapp")
    paired = _Gateway({"status": {"text": "AUTH"}, "user": {"id": "33612345678"}})
    monkeypatch.setattr(provider, "_gateway", lambda session: paired)
    result = await _sync_with(monkeypatch, _gateway_session())
    # Heard through the gateway webhook's store: no phone_number_id, no subscription, no poll.
    assert (result.status, result.transport, result.problems) == ("active", "push", [])


async def test_an_unpaired_gateway_account_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = get_provider("whatsapp")
    monkeypatch.setattr(provider, "_gateway", lambda session: _Gateway({"status": {"text": "QR"}}))
    result = await _sync_with(monkeypatch, _gateway_session())
    assert (result.status, result.transport) == ("error", "none")
    assert result.problems[0].kind == "reauthorize" and "not paired" in result.problems[0].detail


async def test_a_cloud_api_account_without_phone_number_id_keeps_its_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_accounts.pipeline import AccountUnavailableError

    refusal = AccountUnavailableError(
        "The integration has no phone_number_id (checked for the Cloud API backend): edit it and set it."
    )
    result = await _sync_with(monkeypatch, refusal)
    assert (result.status, result.transport) == ("error", "none")
    assert result.problems[0].kind == "integration"
    assert "has no phone_number_id" in result.problems[0].detail


async def test_a_cloud_api_account_with_its_number_listens(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await _sync_with(monkeypatch, _session())
    assert (result.status, result.transport, result.problems) == ("active", "push", [])


def test_event_from_a_stored_row() -> None:
    received = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    event = wa.event_from_row(
        {
            "wamid": "wamid.1",
            "conversation_id": "33612345678",
            "chat_type": "private",
            "sender": "33612345678",
            "sender_name": "Ada",
            "message_type": "text",
            "text": "hello",
            "received_at": received,
        }
    )
    assert event.provider == "whatsapp"
    assert event.conversation_type == "direct"
    assert event.message_id == "wamid.1"
    assert event.sender_name == "Ada"
    assert event.route == {"to": "33612345678", "reply_to": "wamid.1", "conversation_type": "direct"}
    group = wa.event_from_row({"wamid": "w2", "conversation_id": "g1", "chat_type": "group", "sender": "336"})
    assert group.conversation_type == "group" and group.route["to"] == "g1"


async def test_send_reply_answers_the_message(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = wa.WhatsAppProvider()
    fake = _Calls({"messages": [{"id": "wamid.out"}]})
    monkeypatch.setattr(provider, "_call", fake)
    message_id = await provider.send_reply(
        _session(), {"to": "+33612345678", "reply_to": "wamid.1"}, "x" * (wa.MAX_REPLY_CHARS + 10)
    )
    assert message_id == "wamid.out"
    method, url, payload, _ = fake.calls[0]
    assert method == "POST" and url.endswith(f"/{PHONE_ID}/messages")
    assert payload["to"] == "33612345678" and payload["recipient_type"] == "individual"
    assert payload["context"] == {"message_id": "wamid.1"}
    assert len(payload["text"]["body"]) == wa.MAX_REPLY_CHARS


async def test_send_reply_to_a_group(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = wa.WhatsAppProvider()
    fake = _Calls({"messages": [{"id": "w"}]})
    monkeypatch.setattr(provider, "_call", fake)
    await provider.send_reply(_session(), {"to": "group-abc"}, "hi")
    payload = fake.calls[0][2]
    assert payload["recipient_type"] == "group" and "context" not in payload


async def test_fetch_context_from_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    rows = [
        SimpleNamespace(
            wamid=f"w{i}", sender="336", sender_name="Ada", text=f"m{i}", received_at=now - timedelta(minutes=5 - i)
        )
        for i in range(4)
    ]

    async def read_messages(integration_id, conversation_id, limit):
        assert integration_id == INTEGRATION and conversation_id == "336"
        return rows

    monkeypatch.setattr("app.services.whatsapp_inbound.read_messages", read_messages)
    event = wa.event_from_row(
        {"wamid": "w3", "conversation_id": "336", "sender": "336", "text": "m3", "received_at": rows[3].received_at}
    )
    context = await wa.WhatsAppProvider().fetch_context(_session(), event, 2)
    assert [m.message_id for m in context] == ["w1", "w2"]


async def test_dispatch_stored_skips_reactions_and_hands_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    account_ids = [uuid.uuid4()]
    dispatched: list[tuple[str, Any]] = []

    class _Result:
        def scalars(self):
            return self

        def all(self):
            # (account id, agent id) rows: the self-chat routing reads the agent.
            return [(account_id, uuid.uuid4()) for account_id in account_ids]

    class _Db:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, _stmt):
            return _Result()

    monkeypatch.setattr("app.core.database.async_session", lambda: _Db())

    def fake_dispatch(account_id: str, event: Any) -> None:
        dispatched.append((account_id, event))

    monkeypatch.setattr("app.services.agent_accounts.pipeline.dispatch", fake_dispatch)
    rows = [
        {"wamid": "w1", "conversation_id": "336", "sender": "336", "message_type": "text", "text": "hi"},
        {"wamid": "w2", "conversation_id": "336", "sender": "336", "message_type": "reaction", "text": "[reaction]"},
    ]
    assert await wa.dispatch_stored(INTEGRATION, rows) == 1
    assert dispatched[0][0] == str(account_ids[0]) and dispatched[0][1].message_id == "w1"
    assert await wa.dispatch_stored(INTEGRATION, rows[1:]) == 0


async def test_record_change_dispatches_new_rows_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import whatsapp_inbound

    inserted_counts = iter([1, 0])

    class _Exec:
        def __init__(self, rowcount: int) -> None:
            self.rowcount = rowcount

    class _Db:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, stmt):
            name = type(stmt).__name__.lower()
            return _Exec(next(inserted_counts) if "insert" in name else 0)

        async def commit(self):
            return None

    handed: list[list[dict]] = []

    async def dispatch_stored(integration_id, rows):
        handed.append(list(rows))
        return len(rows)

    monkeypatch.setattr(whatsapp_inbound, "async_session", lambda: _Db())
    monkeypatch.setattr(wa, "dispatch_stored", dispatch_stored)
    target = whatsapp_inbound.InboundIntegration(uuid.uuid4(), "s", "v", 30)
    value = {
        "contacts": [],
        "messages": [
            {"id": "w-new", "from": "336", "type": "text", "text": {"body": "a"}, "timestamp": "1762084800"},
            {"id": "w-dup", "from": "336", "type": "text", "text": {"body": "b"}, "timestamp": "1762084800"},
        ],
    }
    assert await whatsapp_inbound.record_change(target, value) == 1
    assert [r["wamid"] for r in handed[0]] == ["w-new"]
