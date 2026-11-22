# SPDX-License-Identifier: Apache-2.0

"""Human gates S11 (#842) end to end against PostgreSQL: human-gates.md I.6, path (d).

A work item waits for a person and its ``payload.notify`` names Google Chat.
App auth: the scheduler tick posts the cardsV2 card as the Chat app, in a
space or in the app's DM with the person (``spaces.findDirectMessage`` on the
identity the person linked by messaging the app); a ``CARD_CLICKED`` from
anyone but the recipient answers a private notice and leaves the card; the
recipient's click answers the item and replaces the buttons with the decided
state; a second click answers nothing; an answer given elsewhere edits every
card the app posted. User auth (no Chat app): the item's Google integration
posts the question with the signed answer links of S9b, as a user, into the
space (a DM is a space too); the recipient's link answers, a forwarded one is
refused, a second click answers nothing.

Only Google is stubbed: the Chat REST calls (``post_message``,
``direct_message_space``, ``patch_message``, ``app_token``) and the agent's
integration tool call are recorders. The JWT check of the webhook is the
unit tests' (``test_google_chat_decision_cards_842_s11.py``); the click enters
at ``_decision_click``, the handler the verified webhook calls.
"""

from __future__ import annotations

import re
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401 - registers every table
from app.api.v1 import decisions as decisions_api
from app.channels import google_chat
from app.core.config import settings
from app.models import decision_hooks
from app.models.agent import Agent
from app.models.decision import Decision, DecisionEvent
from app.models.user import User
from app.models.work_item import WorkItem
from app.services import decision_graph_cards, decision_router
from app.services import google_chat_decision_cards as cards
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

INTEGRATION = "22222222-aaaa-4bbb-8ccc-dddddddddddd"
CHAT_USER = "users/1001"
DM_SPACE = "spaces/DMALICE"
ROOM = "spaces/RELEASEROOM"
_ASK = {
    "question": "Ship the release today?",
    "options": [{"key": "A", "label": "Ship"}, {"key": "B", "label": "Hold"}],
    "recommendation": "A",
}


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "secret_key", "s11-e2e-secret-key-0123456789abcdef")
    monkeypatch.setattr(settings, "decision_link_previous_secrets", "")
    monkeypatch.setattr(settings, "frontend_url", "https://one.example.test/")


@pytest.fixture
async def sessions():
    decision_hooks._HAS_TABLE.clear()
    async with scratch_database(
        "decisions_s11", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        yield async_sessionmaker(db.engine, expire_on_commit=False)
    decision_hooks._HAS_TABLE.clear()


class _Chat:
    """The Chat app's REST calls as recorders: one posted message per call, every edit kept."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, app: bool = True) -> None:
        self.posted: list[tuple[str, dict[str, Any]]] = []
        self.patched: list[tuple[str, dict[str, Any]]] = []
        self.lookups: list[str] = []

        async def app_token(*, strict: bool = False) -> str:
            return "app-token" if app else ""

        async def direct_message_space(token: str, user_name: str, *, strict: bool = False) -> str | None:
            self.lookups.append(user_name)
            return DM_SPACE if user_name == CHAT_USER else None

        async def post_message(
            token: str, space: str, message: dict[str, Any], *, strict: bool = False
        ) -> dict[str, Any]:
            self.posted.append((space, message))
            return {"name": f"{space}/messages/m{len(self.posted)}"}

        async def patch_message(token: str, message_name: str, message: dict[str, Any]) -> bool:
            self.patched.append((message_name, message))
            return True

        for name, stub in (
            ("app_token", app_token),
            ("direct_message_space", direct_message_space),
            ("post_message", post_message),
            ("patch_message", patch_message),
        ):
            monkeypatch.setattr(cards, name, stub)


def _integration(answers: dict[str, tuple[bool, str]], seen: list[tuple[Any, str, str, str, dict[str, Any]]]):
    """The agent's integration tools as a recorder: ``(sender, integration, provider, tool, arguments)``."""

    @asynccontextmanager
    async def factory(sender: Any, integration: str, provider: str = "microsoft"):
        async def call(tool: str, arguments: dict[str, Any]) -> tuple[bool, str]:
            seen.append((sender, integration, provider, tool, arguments))
            return answers.get(tool, (False, f"{tool} refused"))

        yield call

    return factory


async def _people(session: AsyncSession) -> tuple[User, User, Agent]:
    alice = User(
        id=uuid.uuid4(),
        email=f"alice-{uuid.uuid4().hex[:6]}@example.com",
        full_name="Alice",
        channel_identities={f"google_chat:{CHAT_USER}": True},
    )
    bob = User(id=uuid.uuid4(), email=f"bob-{uuid.uuid4().hex[:6]}@example.com", full_name="Bob")
    session.add_all([alice, bob])
    await session.flush()
    agent = Agent(id=uuid.uuid4(), user_id=alice.id, name="Release desk")
    session.add(agent)
    await session.commit()
    return alice, bob, agent


async def _waiting_item(session: AsyncSession, alice: User, agent: Agent, notify: dict[str, str]) -> WorkItem:
    now = datetime.now(UTC)
    item = WorkItem(
        id=uuid.uuid4(),
        user_id=alice.id,
        is_company_managed=False,
        title="Release 7.1",
        status="blocked",
        priority="medium",
        source="agent",
        owner_agent_id=agent.id,
        created_by_agent_id=agent.id,
        needs_human=True,
        ask=_ASK,
        blocked_reason="ship or hold",
        payload={"notify": {"channel": "google_chat", **notify}},
        created_at=now,
        last_activity_at=now,
    )
    session.add(item)
    await session.commit()
    return item


async def _events(session: AsyncSession, decision_id: uuid.UUID) -> list[str]:
    rows = await session.execute(
        select(DecisionEvent.event).where(DecisionEvent.decision_id == decision_id).order_by(DecisionEvent.created_at)
    )
    return list(rows.scalars())


def _buttons(message: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The answer buttons of a posted card, by their text."""
    found: dict[str, dict[str, Any]] = {}
    for section in message["cardsV2"][0]["card"]["sections"]:
        for widget in section["widgets"]:
            for button in (widget.get("buttonList") or {}).get("buttons") or []:
                action = (button.get("onClick") or {}).get("action")
                if action:
                    found[button["text"]] = action
    return found


def _click(action: dict[str, Any], person: User, chat_name: str) -> dict[str, Any]:
    """The ``CARD_CLICKED`` event Chat sends for a click on *action* by *person*."""
    return {
        "type": "CARD_CLICKED",
        "user": {"name": chat_name, "email": person.email, "displayName": person.full_name},
        "action": {"actionMethodName": action["function"], "parameters": action["parameters"]},
        "common": {"invokedFunction": action["function"]},
    }


@pytest.mark.parametrize("where", ["space", "dm"])
async def test_the_app_card_answers_once_in_place_for_its_recipient(sessions, monkeypatch, where):
    chat = _Chat(monkeypatch)
    refreshed: list[uuid.UUID] = []
    monkeypatch.setattr(decision_router, "schedule_card_refresh", refreshed.append)

    async with sessions() as session:
        alice, bob, agent = await _people(session)
        notify = {"space": ROOM} if where == "space" else {"user": alice.email}
        item = await _waiting_item(session, alice, agent, notify)
        decision = (await session.execute(select(Decision).where(Decision.source_id == item.id))).scalar_one()
        assert (decision.status, decision.recipient) == ("open", f"user:{alice.id}")

    # The tick posts the card once, as the Chat app, in the space or the app's DM with Alice.
    assert await decision_graph_cards.deliver_work_item_cards() == 1
    assert await decision_graph_cards.deliver_work_item_cards() == 0
    [(space, message)] = chat.posted
    assert space == (ROOM if where == "space" else DM_SPACE)
    assert chat.lookups == ([] if where == "space" else [CHAT_USER])
    buttons = _buttons(message)
    assert {"Ship", "Hold"} <= set(buttons)
    assert "Waiting for <b>Alice</b>" in str(message)

    # Bob is in the space: his click answers nothing, he alone reads why, the card stays.
    refused = await google_chat._decision_click(_click(buttons["Ship"], bob, "users/2002"))
    assert refused["actionResponse"]["type"] == "NEW_MESSAGE"
    assert refused["privateMessageViewer"] == {"name": "users/2002"}
    assert "cardsV2" not in refused

    # Alice's click answers the item and replaces the buttons with the decided state.
    answered = await google_chat._decision_click(_click(buttons["Ship"], alice, CHAT_USER))
    assert answered["actionResponse"]["type"] == "UPDATE_MESSAGE"
    assert not _buttons(answered)
    assert "Ship" in str(answered["cardsV2"])

    # A second click, on the same or another option, answers nothing.
    again = await google_chat._decision_click(_click(buttons["Hold"], alice, CHAT_USER))
    assert again["actionResponse"]["type"] == "NEW_MESSAGE"
    assert again["privateMessageViewer"] == {"name": CHAT_USER}

    async with sessions() as session:
        row = await session.get(Decision, decision.id)
        assert (row.status, row.surface, row.answer.get("option")) == ("answered", cards.SURFACE, "A")
        item_row = await session.get(WorkItem, item.id)
        assert not item_row.needs_human and item_row.payload["answer"]["option"] == "A"
        events = await _events(session, decision.id)
        assert events.count("answered") == 1
        assert events.count("refused") == 1
        assert events.count("notified") == 1

    # The answer scheduled the edit of the app's cards: every one shows the decided state.
    assert refreshed == [decision.id]
    async with sessions() as session:
        row = await session.get(Decision, decision.id)
        assert await cards.update_sent_cards(session, row, token="app-token") == {"updated": 1, "refused": 0}
    [(name, edited)] = chat.patched
    assert name == f"{space}/messages/m1"
    assert not _buttons(edited)


async def test_an_answer_given_on_the_web_edits_the_app_card(sessions, monkeypatch):
    chat = _Chat(monkeypatch)
    refreshed: list[uuid.UUID] = []
    monkeypatch.setattr(decision_router, "schedule_card_refresh", refreshed.append)
    async with sessions() as session:
        alice, _bob, agent = await _people(session)
        item = await _waiting_item(session, alice, agent, {"space": ROOM})
        decision = (await session.execute(select(Decision).where(Decision.source_id == item.id))).scalar_one()
    assert await decision_graph_cards.deliver_work_item_cards() == 1

    async with sessions() as session:
        person = await session.get(User, alice.id)
        row = await session.get(Decision, decision.id)
        await decision_router.answer_decision(session, row.public_id, person, option="B", surface="web")
        await session.commit()
    assert refreshed == [decision.id]

    async with sessions() as session:
        row = await session.get(Decision, decision.id)
        assert (row.status, row.surface) == ("answered", "web")
        assert await cards.update_sent_cards(session, row, token="app-token") == {"updated": 1, "refused": 0}
    [(_name, edited)] = chat.patched
    assert not _buttons(edited) and "Hold" in str(edited["cardsV2"])


def _token(url: str) -> str:
    return urlparse(url).path.rsplit("/", 1)[-1]


@pytest.mark.parametrize("space", [DM_SPACE, ROOM])
async def test_without_the_app_the_user_posts_the_answer_links_and_they_answer_once(sessions, monkeypatch, space):
    _Chat(monkeypatch, app=False)
    monkeypatch.setattr(decision_router, "schedule_card_refresh", lambda _id: None)
    async with sessions() as session:
        alice, bob, agent = await _people(session)
        item = await _waiting_item(session, alice, agent, {"space": space, "integration": INTEGRATION})
        decision = (await session.execute(select(Decision).where(Decision.source_id == item.id))).scalar_one()

    # No Chat app: the item's agent posts the question and its links through the Google integration, as a user.
    seen: list = []
    caller = _integration({"send_google_chat_message": (True, f"Message sent to {space}.")}, seen)
    assert await decision_graph_cards.deliver_work_item_cards(caller=caller) == 1
    assert await decision_graph_cards.deliver_work_item_cards(caller=caller) == 0
    [(sender, integration, provider, tool, arguments)] = seen
    assert (sender.created_by_agent_id, sender.user_id, integration, provider, tool) == (
        agent.id,
        alice.id,
        INTEGRATION,
        "google",
        "send_google_chat_message",
    )
    assert arguments["space_name"] == space
    lines = arguments["message"].splitlines()
    assert lines[0] == "Ship the release today?"
    # Each answer link is a Google Chat labelled link, "<url|label>" (UX round 1, B3): no raw URL is shown.
    links = {label: url for url, label in re.findall(r"<(https://[^|>]+)\|([^>]+)>", arguments["message"])}
    assert "Ship" in links and "Hold" in links, arguments["message"]
    ship, hold = _token(links["Ship"]), _token(links["Hold"])

    async with sessions() as session:
        person = await session.get(User, bob.id)
        with pytest.raises(HTTPException) as forwarded:
            await decisions_api.answer_by_link(
                ship, decisions_api.LinkAnswer(), user=person, db=session, _agentic_gate=None
            )
        assert forwarded.value.status_code == 403

    async with sessions() as session:
        person = await session.get(User, alice.id)
        first = await decisions_api.answer_by_link(
            ship, decisions_api.LinkAnswer(), user=person, db=session, _agentic_gate=None
        )
        assert first["answered_now"] is True
    async with sessions() as session:
        person = await session.get(User, alice.id)
        second = await decisions_api.answer_by_link(
            hold, decisions_api.LinkAnswer(), user=person, db=session, _agentic_gate=None
        )
        assert second["answered_now"] is False and second["decided"]["option"] == "A"
        row = await session.get(Decision, decision.id)
        assert (row.status, row.surface) == ("answered", cards.LINK_SURFACE)
        item_row = await session.get(WorkItem, item.id)
        assert not item_row.needs_human and item_row.payload["answer"]["option"] == "A"
        # A message posted as a user is never edited: nothing names a card to patch.
        assert await cards.has_sent_cards(session, decision.id) is False
