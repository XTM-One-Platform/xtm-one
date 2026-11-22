# SPDX-License-Identifier: Apache-2.0

"""#1156 end to end against PostgreSQL: an agent sends a work item's decision card itself.

A needs_human item with NO ``payload.notify``: an agent run posts the platform
link card to a Teams chat through its own Microsoft integration
(``decision_graph_cards.send_agent_card``, the ``send_decision_card`` tool).
Graph is the only stub. The card is the recipient's language with the
agent named in its kicker; a second send to the same chat sends nothing; the
scheduler tick sends nothing (no notify ask); the recipient answers through a
button; the message is then edited to the decided card as its sender; and the
item's end has nothing to notify.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401 - registers every table
from app.api.v1 import decisions as decisions_api
from app.core.config import settings
from app.integrations.providers.microsoft._cards import parse_card
from app.models import decision_hooks
from app.models.agent import Agent
from app.models.decision import Decision
from app.models.user import User
from app.models.work_item import WorkItem
from app.services import decision_graph_cards as cards
from app.services import decision_router
from app.services import work_item_notify as notify
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

INTEGRATION = "22222222-aaaa-4bbb-8ccc-dddddddddddd"
CHAT = {"chat_id": "19:alice_dm@unq.gbl.spaces"}
_ASK = {
    "question": "Ship the release today?",
    "context": "CI is green on main.",
    "options": [
        {"key": "A", "label": "Ship", "effect": "tagged now"},
        {"key": "B", "label": "Hold", "effect": "kept for Monday"},
    ],
    "recommendation": "A",
}


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "secret_key", "1156-e2e-secret-key-0123456789abcdef")
    monkeypatch.setattr(settings, "decision_link_previous_secrets", "")
    monkeypatch.setattr(settings, "frontend_url", "https://one.example.test/")
    monkeypatch.setattr(settings, "work_item_notify_test_recipients", "")


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    decision_hooks._HAS_TABLE.clear()
    async with scratch_database(
        "send_decision_card_1156", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        yield async_sessionmaker(db.engine, expire_on_commit=False)
    decision_hooks._HAS_TABLE.clear()


Seen = list[tuple[Any, str, str, dict[str, Any]]]


def _graph(answers: dict[str, tuple[bool, str]], seen: Seen) -> Callable[[Any, str], Any]:
    @asynccontextmanager
    async def factory(sender: Any, integration: str) -> AsyncIterator[Any]:
        async def call(tool: str, arguments: dict[str, Any]) -> tuple[bool, str]:
            seen.append((sender, integration, tool, arguments))
            return answers.get(tool, (False, f"{tool} refused"))

        yield call

    return factory


async def _setup(
    session: AsyncSession, *, title: str = "Release 7.2", key: str | None = None
) -> tuple[User, Agent, WorkItem]:
    alice = User(
        id=uuid.uuid4(), email=f"alice-{uuid.uuid4().hex[:6]}@example.com", full_name="Alice", language="fr-fr"
    )
    session.add(alice)
    await session.flush()
    agent = Agent(id=uuid.uuid4(), user_id=alice.id, name="Release desk")
    session.add(agent)
    await session.flush()
    now = datetime.now(UTC)
    item = WorkItem(
        id=uuid.uuid4(),
        user_id=alice.id,
        is_company_managed=False,
        title=title,
        correlation_key=key,
        status="blocked",
        priority="medium",
        source="agent",
        owner_agent_id=agent.id,
        created_by_agent_id=agent.id,
        needs_human=True,
        ask=_ASK,
        blocked_reason="ship or hold",
        payload={},
        created_at=now,
        last_activity_at=now,
    )
    session.add(item)
    await session.commit()
    return alice, agent, item


async def test_agent_sends_the_card_once_and_it_turns_decided(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    refreshed: list[uuid.UUID] = []
    monkeypatch.setattr(decision_router, "schedule_card_refresh", refreshed.append)
    async with sessions() as session:
        alice, agent, item = await _setup(session)

    seen: Seen = []
    graph = _graph({"send_microsoft_chat_message": (True, "Chat message sent. Message ID: 2001. Sent at: now.")}, seen)
    async with sessions() as session:
        message_id, already = await cards.send_agent_card(
            session,
            item,
            agent_id=agent.id,
            agent_name=agent.name,
            user_id=alice.id,
            integration=INTEGRATION,
            target=CHAT,
            caller=graph,
        )
    assert (message_id, already) == ("2001", False)
    [(sender, integration, tool, arguments)] = seen
    assert (sender.created_by_agent_id, sender.user_id, integration, tool) == (
        agent.id,
        alice.id,
        INTEGRATION,
        "send_microsoft_chat_message",
    )
    card = arguments["card"]
    parsed, err = parse_card(card)
    assert err is None and parsed is not None
    # The recipient's language (fr) and the agent named in the kicker.
    assert card["body"][0]["text"] == "Décision sur une tâche de Release desk"
    titles = [a["title"] for a in card["actions"]]
    assert titles == ["Ship", "Hold", "Répondre par écrit", "Ouvrir dans XTM One"]
    assert all("/decide/" in a["url"] for a in card["actions"][:3])

    # Sent once: a second call to the same chat sends nothing; the tick sends nothing either.
    async with sessions() as session:
        again = await cards.send_agent_card(
            session,
            item,
            agent_id=agent.id,
            agent_name=agent.name,
            user_id=alice.id,
            integration=INTEGRATION,
            target=CHAT,
            caller=graph,
        )
    assert again == ("2001", True) and len(seen) == 1
    assert await cards.deliver_work_item_cards(caller=graph) == 0 and len(seen) == 1

    # The recipient answers through the recommended button.
    token = urlparse(card["actions"][0]["url"]).path.rsplit("/", 1)[-1]
    async with sessions() as session:
        person = await session.get(User, alice.id)
        assert person is not None
        answered = await decisions_api.answer_by_link(
            token, decisions_api.LinkAnswer(), user=person, db=session, _agentic_gate=None
        )
    assert answered["answered_now"] is True
    async with sessions() as session:
        decision = (await session.execute(select(Decision).where(Decision.source_id == item.id))).scalar_one()
        assert refreshed == [decision.id]
        edits: Seen = []
        counts = await cards.update_sent_cards(
            session,
            decision.id,
            answered_by_name="Alice",
            caller=_graph({"update_microsoft_chat_message": (True, "Message updated. Message ID: 2001.")}, edits),
        )
        assert counts == {"updated": 1, "refused": 0}
        assert edits[0][3]["message_id"] == "2001" and "actions" not in edits[0][3]["card"]
        # No notify ask: the item's end sends no text.
        row = await session.get(WorkItem, item.id)
        assert row is not None
        assert notify.notify_spec(row.payload) is None
        assert notify.due_for_delivery(row, datetime.now(UTC)) is None


async def test_a_test_item_reaches_only_the_own_chat(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        alice, agent, item = await _setup(session, key="test:decision-card")
    seen: Seen = []
    graph = _graph({"send_microsoft_chat_message": (True, "Chat message sent. Message ID: 7. Sent at: now.")}, seen)
    async with sessions() as session:
        with pytest.raises(notify.PermanentNotifyError):
            await cards.send_agent_card(
                session,
                item,
                agent_id=agent.id,
                agent_name=agent.name,
                user_id=alice.id,
                integration=INTEGRATION,
                target=CHAT,
                caller=graph,
            )
        assert seen == []
        message_id, _ = await cards.send_agent_card(
            session,
            item,
            agent_id=agent.id,
            agent_name=agent.name,
            user_id=alice.id,
            integration=INTEGRATION,
            target={"chat_id": notify.SELF_CHAT_ID},
            caller=graph,
        )
    assert message_id == "7" and seen[0][3]["chat_id"] == notify.SELF_CHAT_ID


async def test_an_open_item_parked_by_another_agent_without_its_row_gets_its_card(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """#1244: needs_human + a complete ask, status open, parked by another agent, its row gone: the card goes."""
    async with sessions() as session:
        alice, agent, item = await _setup(session)
        parker = Agent(id=uuid.uuid4(), user_id=alice.id, name="Parking desk")
        session.add(parker)
        await session.flush()
        row = await session.get(WorkItem, item.id)
        assert row is not None
        row.created_by_agent_id = parker.id
        row.status = "open"
        decision = (await session.execute(select(Decision).where(Decision.source_id == item.id))).scalar_one()
        decision.status = "cancelled"
        await session.commit()
    seen: Seen = []
    graph = _graph({"send_microsoft_chat_message": (True, "Chat message sent. Message ID: 31. Sent at: now.")}, seen)
    async with sessions() as session:
        message_id, already = await cards.send_agent_card(
            session,
            item,
            agent_id=agent.id,
            agent_name=agent.name,
            user_id=alice.id,
            integration=INTEGRATION,
            target=CHAT,
            caller=graph,
        )
    assert (message_id, already) == ("31", False) and len(seen) == 1
    async with sessions() as session:
        rows = (await session.execute(select(Decision).where(Decision.source_id == item.id))).scalars().all()
    opened = [d for d in rows if d.status == "open"]
    assert len(opened) == 1 and opened[0].ask["question"] == _ASK["question"]
    assert opened[0].recipient == f"user:{alice.id}"


async def test_an_item_no_longer_waiting_is_refused_with_the_missing_field(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        alice, agent, item = await _setup(session)
        decision = (await session.execute(select(Decision).where(Decision.source_id == item.id))).scalar_one()
        decision.status = "cancelled"
        await session.commit()
        row = await session.get(WorkItem, item.id)
        assert row is not None
        row.ask = {"question": "Ship?", "options": [{"key": "A", "label": "Ship"}]}
        await session.commit()
    seen: Seen = []
    async with sessions() as session:
        with pytest.raises(notify.PermanentNotifyError, match=r"ask\.options"):
            await cards.send_agent_card(
                session,
                item,
                agent_id=agent.id,
                agent_name=agent.name,
                user_id=alice.id,
                integration=INTEGRATION,
                target=CHAT,
                caller=_graph({}, seen),
            )
    assert seen == []


def test_missing_decision_field_names_the_field() -> None:
    from types import SimpleNamespace

    ok = SimpleNamespace(status="open", needs_human=True, ask=_ASK)
    assert cards.missing_decision_field(ok) is None
    assert cards.missing_decision_field(SimpleNamespace(status="blocked", needs_human=True, ask=_ASK)) is None
    cases = [
        (SimpleNamespace(status="done", needs_human=True, ask=_ASK), "status"),
        (SimpleNamespace(status="open", needs_human=None, ask=_ASK), "needs_human"),
        (SimpleNamespace(status="open", needs_human=True, ask={}), "ask.question"),
    ]
    for item, field in cases:
        assert (cards.missing_decision_field(item) or "").startswith(field)
