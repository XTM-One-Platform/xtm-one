"""An agent account acts as the account it operates, on real PostgreSQL.

Samuel signs in to XTM One as himself and operates ``executive-assistant@``
through a second Microsoft integration: the Executive Assistant's turns reach
Microsoft as that account (never through Samuel's own integration, which the
agent's template binding resolves to), talk to the person who wrote rather
than to Samuel, and leave Samuel's user memory out. One platform account is
operated by one agent account, the oldest; a subscription a stopped sync left
at the provider is cleaned up before the account subscribes again.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.core.security import encrypt_value
from app.models.agent import Agent
from app.models.agent_account import AgentAccount, AgentAccountSubscription
from app.models.integration import Integration
from app.models.user import User
from app.services import integration_tools
from app.services.agent_accounts import listening, state
from app.services.agent_accounts import providers as provider_registry
from app.services.agent_accounts.events import AccountEvent, AccountIdentity
from app.services.agent_accounts.providers import (
    AccountProvider,
    CreatedSubscription,
    DesiredSubscription,
    ExistingSubscription,
    InboundNotification,
    ProviderCapabilities,
    ProviderError,
    ProviderSession,
)
from app.services.agent_accounts.rules import normalize_rules
from app.services.integration_tools import DIRECT_BINDING_ORIGIN, pin_bindings
from app.services.prompt_guard import build_security_policy
from tests.scratch_database import ALL_TABLES, scratch_database
from tests.test_memory_quarantine_chat_postgres import _LLM, _StopTurnError, _stub_turn

pytestmark = pytest.mark.integration

_PERSONA = "You are the executive assistant of the leadership team."


def _scratch(name: str):
    return scratch_database(name, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True)


async def _samuel_and_two_sign_ins(db) -> tuple[User, Integration, Integration]:
    """Samuel, his own Microsoft integration (the older), and one signed in as the service account."""
    samuel = User(
        email=f"samuel-{uuid.uuid4().hex[:6]}@example.com", hashed_password="x", full_name="Samuel Owner",
        timezone="Europe/Paris",
    )
    db.add(samuel)
    await db.flush()
    own = Integration(
        user_id=samuel.id, provider="microsoft", auth_type="oauth", name="Samuel's Microsoft", status="connected",
        access_token_enc=encrypt_value("samuel"), created_at=datetime.now(UTC) - timedelta(days=1),
    )
    service = Integration(
        user_id=samuel.id, provider="microsoft", auth_type="oauth", name="[Executive Assistant] Microsoft",
        status="connected", access_token_enc=encrypt_value("ea"),
    )
    db.add_all([own, service])
    await db.flush()
    return samuel, own, service


async def test_a_pin_replaces_every_binding_of_its_provider_with_a_usable_integration_only():
    async with _scratch("acct_pin_bindings") as scratch, scratch.sessions() as db:
        samuel, own, service = await _samuel_and_two_sign_ins(db)
        other = User(email=f"o-{uuid.uuid4().hex[:6]}@example.com", hashed_password="x", full_name="Other")
        db.add(other)
        await db.flush()
        foreign = Integration(
            user_id=other.id, provider="microsoft", auth_type="oauth", name="Other's", status="connected",
            access_token_enc=encrypt_value("other"),
        )
        db.add(foreign)
        await db.commit()

        bindings = {"microsoft": str(own.id), "microsoft#2": str(own.id), "github": "gh"}
        origins = dict.fromkeys(bindings, "template")
        bound = await pin_bindings(
            db, bindings, origins, {"microsoft": str(service.id)}, acting_user_id=str(samuel.id)
        )
        assert bound == ["microsoft"]
        assert bindings == {"github": "gh", "microsoft": str(service.id)} and origins["microsoft"] == "pinned"

        # An integration the user may not use never stands in: the provider goes.
        bindings = {"microsoft": str(own.id), "github": "gh"}
        assert await pin_bindings(
            db, bindings, {}, {"microsoft": str(foreign.id)}, acting_user_id=str(samuel.id)
        ) == []
        assert bindings == {"github": "gh"}

        # The pin chooses the account a capability acts as, never adds one...
        bindings = {"github": "gh"}
        await pin_bindings(db, bindings, {}, {"microsoft": str(service.id)}, acting_user_id=str(samuel.id))
        assert bindings == {"github": "gh"}
        # ...unless the agent declares it, as a template binding resolved to nothing.
        await pin_bindings(
            db, bindings, {}, {"microsoft": str(service.id)},
            acting_user_id=str(samuel.id), used_providers=["microsoft"],
        )
        assert bindings == {"github": "gh", "microsoft": str(service.id)}


async def test_a_pin_keeps_the_agent_s_own_direct_bindings_of_its_provider_beside_it():
    """The agent's owner bound the executive's calendar on purpose: the account keeps reaching it."""
    async with _scratch("acct_pin_keeps_direct") as scratch, scratch.sessions() as db:
        samuel, own, service = await _samuel_and_two_sign_ins(db)
        other = User(email=f"o-{uuid.uuid4().hex[:6]}@example.com", hashed_password="x", full_name="Other")
        db.add(other)
        await db.flush()
        foreign = Integration(
            user_id=other.id, provider="microsoft", auth_type="oauth", name="Other's", status="connected",
            access_token_enc=encrypt_value("other"),
        )
        db.add(foreign)
        await db.commit()

        # Direct bindings: the service account AND Samuel's own sign-in. The pin takes
        # the base slot, Samuel's sign-in stays as a second instance (suffixed tools).
        bindings = {"microsoft": str(service.id), "microsoft#2": str(own.id), "github": "gh"}
        origins = dict.fromkeys(bindings, DIRECT_BINDING_ORIGIN)
        bound = await pin_bindings(
            db, bindings, origins, {"microsoft": str(service.id)}, acting_user_id=str(samuel.id)
        )
        assert bound == ["microsoft"]
        assert bindings == {"github": "gh", "microsoft": str(service.id), "microsoft#2": str(own.id)}
        assert origins == {"github": DIRECT_BINDING_ORIGIN, "microsoft": "pinned", "microsoft#2": DIRECT_BINDING_ORIGIN}

        # Whatever the binding order, the pinned integration is the base slot and is never doubled.
        bindings = {"microsoft": str(own.id), "microsoft#2": str(service.id)}
        origins = dict.fromkeys(bindings, DIRECT_BINDING_ORIGIN)
        await pin_bindings(db, bindings, origins, {"microsoft": str(service.id)}, acting_user_id=str(samuel.id))
        assert bindings == {"microsoft": str(service.id), "microsoft#2": str(own.id)}

        # A resolved (template) or session binding still gives way: it would be the owner's own mailbox.
        bindings = {"microsoft": str(own.id)}
        origins = {"microsoft": "template_binding"}
        await pin_bindings(db, bindings, origins, {"microsoft": str(service.id)}, acting_user_id=str(samuel.id))
        assert bindings == {"microsoft": str(service.id)} and origins == {"microsoft": "pinned"}

        # A pin the owner may not use withholds the provider, the kept direct bindings included.
        bindings = {"microsoft": str(own.id), "github": "gh"}
        origins = dict.fromkeys(bindings, DIRECT_BINDING_ORIGIN)
        assert await pin_bindings(
            db, bindings, origins, {"microsoft": str(foreign.id)}, acting_user_id=str(samuel.id)
        ) == []
        assert bindings == {"github": "gh"} and origins == {"github": DIRECT_BINDING_ORIGIN}


_OWNER_DEFAULT_KB = "00000000-0000-4000-8000-00000000000b"


class Turn(NamedTuple):
    prompt: str
    bindings: dict[str, str]
    knowledge_base_ids: list[str]
    own: Integration
    service: Integration


async def _turn(monkeypatch, **kwargs) -> Turn:
    """One Executive Assistant turn run by Samuel: its prompt, bindings and knowledge bases, his two sign-ins.

    Samuel has a default knowledge base of his own, for his own chats.
    """
    from app.services import builtin_tools
    from app.services.agent_chat import run_agent_chat

    async with _scratch("acct_identity_turn") as scratch, scratch.sessions() as db:
        samuel, own, service = await _samuel_and_two_sign_ins(db)
        samuel.default_knowledge_base_ids = [_OWNER_DEFAULT_KB]
        direct = kwargs.pop("direct", False)
        agent = Agent(
            name="Executive Assistant", slug=f"ea-{uuid.uuid4().hex[:8]}", persona=_PERSONA, enabled=True,
            user_id=samuel.id, is_builtin=False, is_company_managed=False, builtin_tool_ids=[],
            handover_agent_ids=[],
            # Either the provider resolved per user (the service account must replace the owner's
            # sign-in), or both sign-ins bound directly (the owner wants both reachable).
            integration_bindings={"microsoft": [str(service.id), str(own.id)]} if direct else {},
            integration_template_bindings=[] if direct else ["microsoft"],
            mcp_server_ids=[], custom_tool_ids=[], knowledge_base_ids=[], output_format="markdown",
            memory_short_term_enabled=False, memory_medium_term_enabled=False, memory_persistent_enabled=False,
            memory_user_enabled=True,
        )
        db.add(agent)
        await db.commit()
        measured = _stub_turn(monkeypatch)
        pinned = kwargs.pop("pinned", False)
        context_extra = kwargs.pop("context_extra", {})
        with pytest.raises(_StopTurnError):
            await run_agent_chat(
                db, str(samuel.id), agent, [], "Alice: can you move my 3pm?",
                context={
                    "platform": "teams", "channel_type": "direct_message", "is_direct_message": True,
                    "contact_name": "Alice", "sender_email": "alice@example.com", **context_extra,
                },
                meter_quota=False, prebuilt_llm=_LLM,
                pinned_integrations={"microsoft": str(service.id)} if pinned else None,
                **kwargs,
            )
    (prompt,) = [text for text in measured if isinstance(text, str) and text.startswith(_PERSONA)]
    built = integration_tools.build_integration_tools.await_args
    kb_call = builtin_tools.build_kb_search_tool.call_args
    return Turn(
        prompt,
        dict(built.args[1]) if built else {},
        list(kb_call.args[2]) if kb_call else [],
        own,
        service,
    )


async def test_without_the_account_the_turn_is_samuels(monkeypatch):
    turn = await _turn(monkeypatch)
    # What an agent-account turn used to be: the owner's mailbox, the owner as the verified interlocutor.
    assert turn.bindings == {"microsoft": str(turn.own.id)}
    assert "Assisting Samuel Owner" in turn.prompt and "Prefers answers in French." in turn.prompt
    assert turn.prompt.endswith(build_security_policy(include_identity=True))
    assert turn.knowledge_base_ids == [_OWNER_DEFAULT_KB]


async def test_an_agent_account_turn_acts_as_the_account_and_talks_to_its_sender(monkeypatch):
    turn = await _turn(monkeypatch, pinned=True, identity_from_platform=True)
    assert turn.bindings == {"microsoft": str(turn.service.id)}
    assert "Samuel Owner" not in turn.prompt
    assert "User Identity (platform-reported, unverified)" in turn.prompt and "alice@example.com" in turn.prompt
    # Samuel's user memory and default knowledge base are his: they never reach the people the account answers.
    assert "Prefers answers in French." not in turn.prompt
    assert turn.knowledge_base_ids == []
    assert turn.prompt.endswith(build_security_policy(include_identity=False))


async def test_a_triage_turn_answers_in_its_schema_with_no_silence_marker_to_contradict_it(monkeypatch):
    """A mailbox turn: the schema carries whether to answer, so no [NO_RESPONSE] block is added."""
    from app.services.agent_accounts import triage

    plain = await _turn(monkeypatch, pinned=True, identity_from_platform=True, context_extra={"response_policy": "decide"})
    assert "[NO_RESPONSE]" in plain.prompt
    turn = await _turn(
        monkeypatch,
        pinned=True,
        identity_from_platform=True,
        context_extra={"response_policy": "schema", "is_thread_continuation": True},
        session_output_schema=triage.output_schema(must_reply=False),
    )
    assert "[NO_RESPONSE]" not in turn.prompt
    assert "── You MUST respond ──" not in turn.prompt and "── Thread Continuation Mode ──" not in turn.prompt
    assert "needs_owner" in turn.prompt and "no_reply" in turn.prompt


async def test_an_agent_account_turn_keeps_the_other_sign_in_the_agent_binds_directly(monkeypatch):
    """An agent bound to the service account AND its owner's calendar reaches both from a Teams run,
    the account's own sign-in on the base slot (the pin), the other as a second, suffixed instance."""
    turn = await _turn(monkeypatch, pinned=True, identity_from_platform=True, direct=True)
    assert turn.bindings == {"microsoft": str(turn.service.id), "microsoft#2": str(turn.own.id)}
    # Without the account, a chat run already exposes both, in binding order.
    chat = await _turn(monkeypatch, direct=True)
    assert chat.bindings == {"microsoft": str(chat.service.id), "microsoft#2": str(chat.own.id)}


async def test_a_provider_a_rule_leaves_out_is_dropped_even_when_pinned(monkeypatch):
    turn = await _turn(
        monkeypatch, pinned=True, identity_from_platform=True, allowed_integration_providers={"opencti"}
    )
    assert turn.bindings == {}
    kept = await _turn(
        monkeypatch, pinned=True, identity_from_platform=True, allowed_integration_providers={"microsoft"}
    )
    assert kept.bindings == {"microsoft": str(kept.service.id)}


# ---------------------------------------------------------------------------
# Listening: one agent account per platform account, no stranded subscription
# ---------------------------------------------------------------------------


class FakeProvider(AccountProvider):
    key = "fake"
    label = "Fake Chat"
    integration_provider = "microsoft"
    platform = "teams"
    capabilities = ProviderCapabilities(direct=True, group=True, channel=True, push=True)

    def __init__(self) -> None:
        self.held: list[ExistingSubscription] = []
        self.created: list[str] = []
        self.deleted: list[str] = []
        #: What the provider answers a creation of each resource with, instead of creating it.
        self.refusals: dict[str, ProviderError] = {}
        #: The message the next notification names.
        self.notified: AccountEvent | None = None
        #: The replies a followed thread brings, and the reads asked of it.
        self.thread: list[AccountEvent] = []
        self.thread_reads: list[tuple[dict, datetime]] = []

    async def event_from_notification(self, session, notification):
        return self.notified

    async def thread_messages_since(self, session, route, since):
        self.thread_reads.append((route, since))
        return [event for event in self.thread if event.created_at > since]

    async def identify(self, session):
        return AccountIdentity("ea", "Executive Assistant", "ea@example.com")

    async def desired_subscriptions(self, session, plan, *, reactions=False):
        return [
            DesiredSubscription("/users/ea/chats/getAllMessages", covers="chats", permission="Chat.Read"),
            DesiredSubscription("/teams/t/channels/c", name="Ops > General", permission="ChannelMessage.Read.All"),
        ]

    def subscription_slot(self, resource):
        return resource.split("?", 1)[0].strip("/")

    async def create_subscription(
        self, session, resource, *, notification_url, lifecycle_url, client_state, change_type="created"
    ):
        if resource in self.refusals:
            raise self.refusals[resource]
        self.created.append(resource)
        return CreatedSubscription(f"new-{len(self.created)}", datetime.now(UTC) + timedelta(days=2))

    async def delete_subscription(self, session, external_id):
        self.deleted.append(external_id)
        return True

    async def list_subscriptions(self, session):
        return list(self.held)

    async def fetch_context(self, session, event, limit):  # pragma: no cover
        return []

    async def account_messages_since(self, session, event):  # pragma: no cover
        return []

    async def send_reply(self, session, route, text):  # pragma: no cover
        return "sent"


@pytest.fixture
def provider(monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr(provider_registry, "_PROVIDERS", {"fake": fake})

    async def _session(db, account, provider_):
        return ProviderSession(token="t", account_external_id=account.account_external_id)

    async def _nothing(*args, **kwargs):
        return None

    async def _granted(*args, **kwargs):
        return True

    monkeypatch.setattr(listening, "open_session", _session)
    monkeypatch.setattr(listening, "push_reachable", lambda: True)
    monkeypatch.setattr(state, "take_lease", _granted)
    monkeypatch.setattr(state, "release_lease", _nothing)
    monkeypatch.setattr(state, "mark_synced", _nothing)
    return fake


async def _operated(
    db, owner: User, integration: Integration, name: str, *, age: timedelta, external_id: str | None = "ea"
) -> AgentAccount:
    agent = Agent(name=name, slug=f"a-{uuid.uuid4().hex[:8]}", user_id=owner.id)
    db.add(agent)
    await db.flush()
    account = AgentAccount(
        user_id=owner.id, name=name, provider="fake", integration_id=integration.id, agent_id=agent.id, mode="ai_user",
        rules=normalize_rules([{"id": "direct", "conversation_types": ["direct"], "trigger": "any"}]),
        account_external_id=external_id, created_at=datetime.now(UTC) - age,
    )
    db.add(account)
    await db.flush()
    return account


async def _subscription(db, account: AgentAccount, external_id: str, resource: str) -> None:
    db.add(
        AgentAccountSubscription(
            account_id=account.id, external_id=external_id, resource=resource,
            client_state_enc=encrypt_value("s"), expires_at=datetime.now(UTC) + timedelta(days=2),
        )
    )


async def test_an_account_found_signed_in_as_an_operated_account_stands_aside(provider):
    async with _scratch("acct_operator") as scratch:
        async with scratch.sessions() as db:
            samuel, own, service = await _samuel_and_two_sign_ins(db)
            first = await _operated(db, samuel, service, "Executive Assistant", age=timedelta(days=1))
            # Saved while the provider could not say who its integration is signed in as.
            second = await _operated(
                db, samuel, own, "Executive Assistant (2)", age=timedelta(minutes=1), external_id=None
            )
            await _subscription(db, second, "sub-of-the-second", "/users/ea/chats/getAllMessages")
            await db.commit()
            first_id, second_id = first.id, second.id

        async with scratch.sessions() as db:
            later = await db.get(AgentAccount, second_id)
            await listening.sync_account(db, later)
            (problem,) = later.sync_report["problems"]
            assert later.status == "error" and problem["kind"] == "operated"
            # Samuel's own account is named to him.
            assert problem["holder"] == "Executive Assistant"
            # The platform's own reason travels as a note, worded in the viewer's language.
            assert problem["details"] == []
            assert problem["notes"] == [{"code": "operated_own", "values": {"holder": "Executive Assistant"}}]
            assert "'Executive Assistant'" in later.last_error
            # The identity is not stored: the unique index would refuse it.
            assert later.account_external_id is None
            assert provider.deleted == ["sub-of-the-second"] and provider.created == []
            assert (
                await db.execute(select(AgentAccountSubscription).where(AgentAccountSubscription.account_id == second_id))
            ).first() is None

        async with scratch.sessions() as db:
            operating = await db.get(AgentAccount, first_id)
            await listening.sync_account(db, operating)
            assert operating.status == "active" and len(provider.created) == 2


async def test_a_subscription_no_account_recorded_is_deleted_before_subscribing_again(provider):
    ours = listening.notification_url("fake")
    async with _scratch("acct_unrecorded") as scratch:
        async with scratch.sessions() as db:
            samuel, _own, service = await _samuel_and_two_sign_ins(db)
            account = await _operated(db, samuel, service, "Executive Assistant", age=timedelta(days=1))
            await _subscription(db, account, "sub-recorded", "/users/ea/chats/getAllMessages")
            await db.commit()
        provider.held = [
            ExistingSubscription("sub-recorded", "/users/ea/chats/getAllMessages", ours),
            # Created by a sync stopped before it could record it.
            ExistingSubscription("sub-stranded", "/teams/t/channels/c", ours),
            # Another platform on the same Microsoft application.
            ExistingSubscription("sub-of-staging", "/teams/t/channels/c", "https://staging.example.com/hook"),
        ]
        async with scratch.sessions() as db:
            stored = await db.get(AgentAccount, account.id)
            await listening.sync_account(db, stored)
        assert provider.deleted == ["sub-stranded"]
        assert provider.created == ["/teams/t/channels/c"]


async def test_a_message_from_a_conversation_the_rules_left_syncs_the_account_again(provider, monkeypatch):
    cleared: list[str] = []
    decided: list[str] = []

    async def _clear(account_id):
        cleared.append(account_id)

    async def _decide(account_id, event, **kwargs):
        decided.append(event.conversation_id)

    monkeypatch.setattr(state, "clear_synced", _clear)
    monkeypatch.setattr(listening, "process_event", _decide)
    async with _scratch("acct_unheard") as scratch:
        async with scratch.sessions() as db:
            samuel, _own, service = await _samuel_and_two_sign_ins(db)
            # Its one rule answers direct messages: it listens to no channel.
            account = await _operated(db, samuel, service, "Executive Assistant", age=timedelta(days=1))
            await db.commit()
            account_id = str(account.id)
        for conversation_type, conversation_id in (("direct", "chat-1"), ("channel", "19:general@thread.tacv2")):
            provider.notified = AccountEvent(
                provider="fake", conversation_type=conversation_type, conversation_id=conversation_id,
                thread_id=conversation_id, message_id="m1", sender_id="app", sender_name="Workflows",
                text="Build passed", created_at=datetime.now(UTC), from_application=True,
            )
            await listening.handle_notification(account_id, "fake", InboundNotification("s", "c", None, {}))
    # Both are still decided (the log says the channel is no rule's), and the
    # subscription that brought the channel's is due for the next sync.
    assert decided == ["chat-1", "19:general@thread.tacv2"]
    assert cleared == [account_id]


# ---------------------------------------------------------------------------
# The sync report: why a conversation is not heard, and what to do
# ---------------------------------------------------------------------------

_REFUSED = "Microsoft Graph answered 403: Operation: Create; Exception: [Status Code: Forbidden; Reason: ...]"


async def _sync_once(scratch, account_id) -> AgentAccount:
    async with scratch.sessions() as db:
        account = await db.get(AgentAccount, account_id)
        await listening.sync_account(db, account)
    async with scratch.sessions() as db:
        return await db.get(AgentAccount, account_id)


async def test_a_refused_conversation_another_account_holds_names_that_account_to_its_owner_only(provider):
    provider.refusals["/teams/t/channels/c"] = ProviderError(_REFUSED, kind="refused")
    async with _scratch("acct_report_taken") as scratch:
        async with scratch.sessions() as db:
            samuel, own, service = await _samuel_and_two_sign_ins(db)
            account = await _operated(db, samuel, service, "Executive Assistant", age=timedelta(days=1))
            twin = await _operated(db, samuel, own, "My digital twin", age=timedelta(days=2), external_id="samuel")
            await _subscription(db, twin, "sub-of-the-twin", "/teams/t/channels/c?$filter=mentions")
            other = User(email=f"o-{uuid.uuid4().hex[:6]}@example.com", hashed_password="x", full_name="Other")
            db.add(other)
            await db.flush()
            theirs = Integration(
                user_id=other.id, provider="microsoft", auth_type="oauth", name="Other's", status="connected",
                access_token_enc=encrypt_value("o"),
            )
            db.add(theirs)
            await db.flush()
            stranger = await _operated(db, other, theirs, "Other's twin", age=timedelta(days=3), external_id="other")
            await db.commit()
            account_id, twin_id, stranger_id = account.id, twin.id, stranger.id

        stored = await _sync_once(scratch, account_id)
        report = stored.sync_report
        # The account hears its chats: active, one of its two subscriptions held.
        assert stored.status == "active" and stored.last_error is None
        assert (report["wanted"], report["heard"]) == (2, 1) and report["checked_at"]
        (problem,) = report["problems"]
        assert problem["kind"] == "taken_by_account" and problem["holder"] == "My digital twin"
        assert problem["targets"] == [{"covers": "channel", "name": "Ops > General"}]
        assert problem["permission"] is None and problem["details"] == [_REFUSED]

        # Held by another user's account: said, never named.
        async with scratch.sessions() as db:
            await db.execute(
                AgentAccountSubscription.__table__.update()
                .where(AgentAccountSubscription.account_id == twin_id)
                .values(account_id=stranger_id)
            )
            await db.commit()
        (problem,) = (await _sync_once(scratch, account_id)).sync_report["problems"]
        assert problem["kind"] == "taken_by_account" and problem["holder"] is None


async def test_a_refused_conversation_held_at_another_address_names_that_host(provider):
    provider.refusals["/teams/t/channels/c"] = ProviderError(_REFUSED, kind="refused")
    provider.held = [
        ExistingSubscription("sub-of-staging", "teams/t/channels/c", "https://staging.example.com/api/v1/agent-accounts/x")
    ]
    async with _scratch("acct_report_elsewhere") as scratch:
        async with scratch.sessions() as db:
            samuel, _own, service = await _samuel_and_two_sign_ins(db)
            account = await _operated(db, samuel, service, "Executive Assistant", age=timedelta(days=1))
            await db.commit()
        (problem,) = (await _sync_once(scratch, account.id)).sync_report["problems"]
    assert problem["kind"] == "taken_elsewhere" and problem["holder"] == "staging.example.com"
    # Nothing of another platform is ever deleted.
    assert provider.deleted == []


async def test_a_refusal_nothing_explains_asks_for_the_permission_it_needs(provider):
    for resource in ("/users/ea/chats/getAllMessages", "/teams/t/channels/c"):
        provider.refusals[resource] = ProviderError(_REFUSED, kind="refused")
    async with _scratch("acct_report_refused") as scratch:
        async with scratch.sessions() as db:
            samuel, _own, service = await _samuel_and_two_sign_ins(db)
            account = await _operated(db, samuel, service, "Executive Assistant", age=timedelta(days=1))
            account.last_error = "An earlier sync's errors"
            await db.commit()
        stored = await _sync_once(scratch, account.id)
    # Nothing is heard: the account is in error, and says why per permission.
    assert stored.status == "error" and stored.transport == "none"
    # The earlier errors are replaced by this sync's reason.
    assert stored.last_error.startswith("The last sync failed (refused): ")
    assert "An earlier sync" not in stored.last_error
    report = stored.sync_report
    assert (report["wanted"], report["heard"]) == (2, 0)
    assert [(p["kind"], p["permission"], p["targets"]) for p in report["problems"]] == [
        ("refused", "Chat.Read", [{"covers": "chats", "name": None}]),
        ("refused", "ChannelMessage.Read.All", [{"covers": "channel", "name": "Ops > General"}]),
    ]


async def test_a_creation_the_provider_never_validated_says_the_platform_is_unreachable(provider, monkeypatch):
    validated: list[bool | None] = [False]

    async def _handshake_since(provider_key, since):
        return validated[0]

    monkeypatch.setattr(state, "handshake_since", _handshake_since)
    provider.refusals["/teams/t/channels/c"] = ProviderError(
        "Microsoft Graph answered 400: Subscription validation request failed.", kind="error"
    )
    async with _scratch("acct_report_unreachable") as scratch:
        async with scratch.sessions() as db:
            samuel, _own, service = await _samuel_and_two_sign_ins(db)
            account = await _operated(db, samuel, service, "Executive Assistant", age=timedelta(days=1))
            await db.commit()
        (problem,) = (await _sync_once(scratch, account.id)).sync_report["problems"]
        assert problem["kind"] == "unreachable"
        assert problem["details"] == ["Microsoft Graph answered 400: Subscription validation request failed."]
        # The provider did call back, or nobody knows: the provider's words, as they are.
        for answer in (True, None):
            validated[0] = answer
            (problem,) = (await _sync_once(scratch, account.id)).sync_report["problems"]
            assert problem["kind"] == "error"


async def test_an_integration_that_cannot_be_used_is_said_as_such(provider, monkeypatch):
    async def _unavailable(db, account, provider_):
        raise listening.AccountUnavailableError("The integration is not connected: sign it in again.")

    monkeypatch.setattr(listening, "open_session", _unavailable)
    async with _scratch("acct_report_integration") as scratch:
        async with scratch.sessions() as db:
            samuel, _own, service = await _samuel_and_two_sign_ins(db)
            account = await _operated(db, samuel, service, "Executive Assistant", age=timedelta(days=1))
            await db.commit()
        stored = await _sync_once(scratch, account.id)
    (problem,) = stored.sync_report["problems"]
    assert stored.status == "error" and problem["kind"] == "integration"
    assert problem["details"] == ["The integration is not connected: sign it in again."]


# ---------------------------------------------------------------------------
# A thread the account answered in, in a channel heard for mentions only
# ---------------------------------------------------------------------------

_MENTIONS_IN_CHANNELS = [
    {"id": "dm", "conversation_types": ["direct", "group"], "trigger": "any", "reply": "decide"},
    {"id": "mentions", "conversation_types": ["group", "channel"], "trigger": "mention", "reply": "decide"},
]
_ROUTE = {"kind": "channel", "team_id": "t", "channel_id": "c", "root_id": "root"}


def _reply_in_thread(minutes: int, message_id: str) -> AccountEvent:
    return AccountEvent(
        provider="fake", conversation_type="channel", conversation_id="c", thread_id="c:root",
        message_id=message_id, sender_id="alice", sender_name="Alice", text="and what about tomorrow?",
        created_at=datetime.now(UTC) + timedelta(minutes=minutes), route=dict(_ROUTE),
    )


async def test_a_followed_thread_brings_its_new_replies_to_the_pipeline_and_moves_its_cursor(provider, monkeypatch):
    dispatched: list[tuple[str, str]] = []
    reads: list[tuple[str, float]] = []
    since = datetime.now(UTC).timestamp()

    def _dispatch(account_id, event):
        dispatched.append((account_id, event.message_id))

    async def _read(account_id, conversation_id, thread_id, route, read_up_to):
        reads.append((thread_id, read_up_to))

    monkeypatch.setattr(listening, "dispatch", _dispatch)
    monkeypatch.setattr(state, "followed_thread_read", _read)
    provider.thread = [_reply_in_thread(-5, "older"), _reply_in_thread(1, "new-1"), _reply_in_thread(2, "new-2")]
    async with _scratch("acct_follow_threads") as scratch:
        async with scratch.sessions() as db:
            samuel, _own, service = await _samuel_and_two_sign_ins(db)
            account = await _operated(db, samuel, service, "My digital twin", age=timedelta(days=1))
            account.rules = normalize_rules(_MENTIONS_IN_CHANNELS)
            await db.commit()
            account_id = str(account.id)

        async def _due(limit):
            return [(account_id, "c", "c:root", dict(_ROUTE), since)]

        monkeypatch.setattr(state, "due_followed_threads", _due)
        assert await listening.follow_threads() == 1
        assert dispatched == [(account_id, "new-1"), (account_id, "new-2")]
        assert reads == [("c:root", provider.thread[2].created_at.timestamp())]
        assert provider.thread_reads[0][0] == _ROUTE

        # Heard in full now (a rule answers every channel message): the following ends.
        async with scratch.sessions() as db:
            stored = await db.get(AgentAccount, uuid.UUID(account_id))
            stored.rules = normalize_rules([{"conversation_types": ["channel"], "trigger": "any"}])
            await db.commit()
        dispatched.clear()
        reads.clear()
        assert await listening.follow_threads() == 0
        assert dispatched == [] and reads == []
