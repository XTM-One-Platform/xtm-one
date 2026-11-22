# SPDX-License-Identifier: Apache-2.0

"""Human gates S9b (#842) end to end against PostgreSQL: human-gates.md I.6, paths (b) and (c).

A work item waits for a person and names a Microsoft integration and a Teams
target in ``payload.notify``. The scheduler tick posts the link card as the
item's agent (Graph is the only stub: ``agent_graph_caller`` is replaced by a
recorder, the agent's own tools behind it are the integration's); the
recipient opens one answer link and the item resumes; a forwarded link is
refused and audited; a second click answers nothing; an expired link is
refused; each sent card is then edited to its decided state as its sender.
Path (b) (a user or service account) and path (c) (an agent account) differ
only by the integration the agent posts with, so one run per target covers
both: a 1:1 chat, a group chat (both a Graph ``chat_id``) and a channel.
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401 - registers every table
from app.api.v1 import decisions as decisions_api
from app.core.config import settings
from app.integrations.providers.microsoft._cards import parse_card
from app.models import decision_hooks
from app.models.agent import Agent
from app.models.decision import Decision, DecisionEvent
from app.models.user import User
from app.models.work_item import WorkItem
from app.services import decision_answer_links as answer_links
from app.services import decision_graph_cards as cards
from app.services import decision_router
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

INTEGRATION = "11111111-aaaa-4bbb-8ccc-dddddddddddd"
_ASK = {
    "question": "Ship the release today?",
    "options": [{"key": "A", "label": "Ship"}, {"key": "B", "label": "Hold"}],
    "recommendation": "A",
}
_TARGETS = {
    "one_to_one_chat": {"chat_id": "19:alice_bot@unq.gbl.spaces"},
    "group_chat": {"chat_id": "19:meeting_group@thread.v2"},
    "channel": {"team_id": "team-1", "channel_id": "19:general@thread.tacv2"},
}


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "secret_key", "s9b-e2e-secret-key-0123456789abcdef")
    monkeypatch.setattr(settings, "decision_link_previous_secrets", "")
    monkeypatch.setattr(settings, "frontend_url", "https://one.example.test/")


@pytest.fixture
async def sessions():
    decision_hooks._HAS_TABLE.clear()
    async with scratch_database(
        "decisions_s9b", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        yield async_sessionmaker(db.engine, expire_on_commit=False)
    decision_hooks._HAS_TABLE.clear()


def _graph(answers: dict[str, tuple[bool, str]], seen: list[tuple[Any, str, str, dict[str, Any]]]):
    """The agent's Graph tools as a recorder: ``(sender, integration, tool, arguments)`` per call."""

    @asynccontextmanager
    async def factory(sender: Any, integration: str):
        async def call(tool: str, arguments: dict[str, Any]) -> tuple[bool, str]:
            seen.append((sender, integration, tool, arguments))
            return answers.get(tool, (False, f"{tool} refused"))

        yield call

    return factory


def _token(url: str) -> str:
    return urlparse(url).path.rsplit("/", 1)[-1]


async def _people(session: AsyncSession) -> tuple[User, User, Agent]:
    alice = User(id=uuid.uuid4(), email=f"alice-{uuid.uuid4().hex[:6]}@example.com", full_name="Alice")
    bob = User(id=uuid.uuid4(), email=f"bob-{uuid.uuid4().hex[:6]}@example.com", full_name="Bob")
    session.add_all([alice, bob])
    await session.flush()
    agent = Agent(id=uuid.uuid4(), user_id=alice.id, name="Release desk")
    session.add(agent)
    await session.commit()
    return alice, bob, agent


async def _waiting_item(session: AsyncSession, alice: User, agent: Agent, target: dict[str, str]) -> WorkItem:
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
        payload={
            "notify": {
                "channel": "teams_channel" if "team_id" in target else "teams_dm",
                "integration": INTEGRATION,
                **target,
            }
        },
        created_at=now,
        last_activity_at=now,
    )
    session.add(item)
    await session.commit()
    return item


async def _events(session: AsyncSession, decision_id: uuid.UUID) -> list[DecisionEvent]:
    rows = await session.execute(
        select(DecisionEvent).where(DecisionEvent.decision_id == decision_id).order_by(DecisionEvent.created_at)
    )
    return list(rows.scalars())


@pytest.mark.parametrize("where", sorted(_TARGETS))
async def test_link_card_answers_once_for_its_recipient_and_is_edited_to_decided(sessions, monkeypatch, where):
    target = _TARGETS[where]
    kind = cards.target_kind(target)
    send_tool, edit_tool = cards._SEND[kind], cards._UPDATE[kind]
    refreshed: list[uuid.UUID] = []
    monkeypatch.setattr(decision_router, "schedule_card_refresh", refreshed.append)

    async with sessions() as session:
        alice, bob, agent = await _people(session)
        item = await _waiting_item(session, alice, agent, target)
        decision = (await session.execute(select(Decision).where(Decision.source_id == item.id))).scalar_one()
        assert (decision.status, decision.recipient) == ("open", f"user:{alice.id}")

    # The tick posts the card once, as the item's agent acting for its owner.
    seen: list = []
    graph = _graph({send_tool: (True, "Message sent. Message ID: 1716. Sent at: now.")}, seen)
    assert await cards.deliver_work_item_cards(caller=graph) == 1
    assert await cards.deliver_work_item_cards(caller=graph) == 0
    [(sender, integration, tool, arguments)] = seen
    assert (sender.created_by_agent_id, sender.user_id, integration, tool) == (
        agent.id,
        alice.id,
        INTEGRATION,
        send_tool,
    )
    parse_card(arguments["card"])  # Graph accepts it as posted by a user or an agent account
    links = {a["title"]: a["url"] for a in arguments["card"]["actions"]}
    assert all(a["type"] == "Action.OpenUrl" for a in arguments["card"]["actions"])
    ship = _token(links["Ship"])

    async with sessions() as session:
        person = await session.get(User, bob.id)
        # A forwarded link answers nothing and is audited.
        for call in (
            decisions_api.read_answer_link(ship, user=person, db=session),
            decisions_api.answer_by_link(ship, decisions_api.LinkAnswer(), user=person, db=session, _agentic_gate=None),
        ):
            with pytest.raises(HTTPException) as refused:
                await call
            assert refused.value.status_code == 403

    async with sessions() as session:
        person = await session.get(User, alice.id)
        view = await decisions_api.read_answer_link(ship, user=person, db=session)
        assert (view["status"], view["option_label"], view["decided"]) == ("open", "Ship", None)
        first = await decisions_api.answer_by_link(
            ship, decisions_api.LinkAnswer(), user=person, db=session, _agentic_gate=None
        )
        assert first["answered_now"] is True
        assert (first["decided"]["status"], first["decided"]["option"]) == ("answered", "A")

    async with sessions() as session:
        person = await session.get(User, alice.id)
        # A second click, on the same or another option, shows the decided state and answers nothing.
        hold = _token(links["Hold"])
        second = await decisions_api.answer_by_link(
            hold, decisions_api.LinkAnswer(), user=person, db=session, _agentic_gate=None
        )
        assert second["answered_now"] is False
        assert second["decided"]["option"] == "A"
        row = await session.get(Decision, decision.id)
        assert (row.status, row.surface, row.answer.get("option")) == ("answered", cards._SURFACE[kind], "A")
        item_row = await session.get(WorkItem, item.id)
        assert not item_row.needs_human and item_row.payload["answer"]["option"] == "A"
        events = [e.event for e in await _events(session, decision.id)]
        assert events.count("answered") == 1
        assert events.count("refused") == 2
        assert events.count("notified") == 1

    # The answer scheduled the card refresh; each sent card is edited to the decided card as its sender.
    assert refreshed == [decision.id]
    edits: list = []
    async with sessions() as session:
        counts = await cards.update_sent_cards(
            session,
            decision.id,
            answered_by_name="Alice",
            caller=_graph({edit_tool: (True, "Message updated. Message ID: 1716.")}, edits),
        )
    assert counts == {"updated": 1, "refused": 0}
    [(sender, integration, tool, arguments)] = edits
    # The edit's sender is read back from the `notified` event, where ids are stored as text.
    assert (str(sender.created_by_agent_id), integration, tool, arguments["message_id"]) == (
        str(agent.id),
        INTEGRATION,
        edit_tool,
        "1716",
    )
    assert "actions" not in arguments["card"]
    assert "Answered: Ship" in arguments["card"]["body"][1]["text"]


async def test_a_refused_edit_leaves_the_decided_page_and_an_expired_link_answers_nothing(sessions, monkeypatch):
    monkeypatch.setattr(decision_router, "schedule_card_refresh", lambda _id: None)
    target = _TARGETS["one_to_one_chat"]
    async with sessions() as session:
        alice, _bob, agent = await _people(session)
        item = await _waiting_item(session, alice, agent, target)
        decision = (await session.execute(select(Decision).where(Decision.source_id == item.id))).scalar_one()
    seen: list = []
    graph = _graph({"send_microsoft_chat_message": (True, "Chat message sent. Message ID: 42. Sent at: now.")}, seen)
    assert await cards.deliver_work_item_cards(caller=graph) == 1

    expired = answer_links.mint_token(decision.id, "A", alice.id, "teams_chat", int(time.time()) - 60)
    async with sessions() as session:
        person = await session.get(User, alice.id)
        with pytest.raises(HTTPException) as gone:
            await decisions_api.answer_by_link(
                expired, decisions_api.LinkAnswer(), user=person, db=session, _agentic_gate=None
            )
        assert gone.value.status_code == 410
        assert (await session.get(Decision, decision.id)).status == "open"

    links = {a["title"]: a["url"] for a in seen[0][3]["card"]["actions"]}
    async with sessions() as session:
        person = await session.get(User, alice.id)
        answered = await decisions_api.answer_by_link(
            _token(links["Hold"]), decisions_api.LinkAnswer(), user=person, db=session, _agentic_gate=None
        )
        assert answered["answered_now"] is True

    # Graph refuses the edit: counted, never raised; the link page stays the decided view.
    async with sessions() as session:
        counts = await cards.update_sent_cards(session, decision.id, caller=_graph({}, []))
        assert counts == {"updated": 0, "refused": 1}
        person = await session.get(User, alice.id)
        view = await decisions_api.read_answer_link(_token(links["Ship"]), user=person, db=session)
        assert (view["status"], view["decided"]["option"]) == ("answered", "B")


async def test_an_email_target_gets_the_links_as_text_and_answers_with_the_email_surface(sessions, monkeypatch):
    monkeypatch.setattr(decision_router, "schedule_card_refresh", lambda _id: None)
    async with sessions() as session:
        alice, _bob, agent = await _people(session)
        item = await _waiting_item(session, alice, agent, {})
        item.payload = {"notify": {"channel": "email", "integration": INTEGRATION, "user": alice.email}}
        await session.commit()
        decision = (await session.execute(select(Decision).where(Decision.source_id == item.id))).scalar_one()
    seen: list = []
    graph = _graph({"send_microsoft_email": (True, "Email sent.")}, seen)
    assert await cards.deliver_work_item_cards(caller=graph) == 1
    [(_sender, _integration, tool, arguments)] = seen
    assert (tool, arguments["to"], arguments["content_type"]) == ("send_microsoft_email", alice.email, "text")
    ship = next(line for line in arguments["body"].splitlines() if line.startswith("Ship: "))
    async with sessions() as session:
        person = await session.get(User, alice.id)
        answered = await decisions_api.answer_by_link(
            _token(ship.removeprefix("Ship: ")), decisions_api.LinkAnswer(), user=person, db=session, _agentic_gate=None
        )
        assert answered["answered_now"] is True
        row = await session.get(Decision, decision.id)
        assert (row.status, row.surface) == ("answered", "email")
    # An email has no message to edit: nothing to refresh.
    async with sessions() as session:
        assert await cards.has_sent_cards(session, decision.id) is False
    assert datetime.now(UTC) - row.answered_at < timedelta(minutes=5)


async def test_the_text_link_page_answers_an_option_and_an_option_link_answers_its_own_only(
    sessions: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The text link's page shows the shared card: an option picked there answers; an option link refuses another."""
    monkeypatch.setattr(decision_router, "schedule_card_refresh", lambda _id: None)
    async with sessions() as session:
        alice, _bob, agent = await _people(session)
        item = await _waiting_item(session, alice, agent, _TARGETS["one_to_one_chat"])
        decision = (await session.execute(select(Decision).where(Decision.source_id == item.id))).scalar_one()
    expires = int(time.time()) + 3600
    ship = answer_links.mint_token(decision.id, "A", alice.id, "teams_chat", expires)
    text = answer_links.mint_token(decision.id, answer_links.TEXT_OPTION, alice.id, "teams_chat", expires)
    async with sessions() as session:
        person = await session.get(User, alice.id)
        with pytest.raises(HTTPException) as other:
            await decisions_api.answer_by_link(
                ship, decisions_api.LinkAnswer(option="B"), user=person, db=session, _agentic_gate=None
            )
        assert other.value.status_code == 400
        assert (await session.get(Decision, decision.id)).status == "open"
    async with sessions() as session:
        person = await session.get(User, alice.id)
        answered = await decisions_api.answer_by_link(
            text, decisions_api.LinkAnswer(option="B"), user=person, db=session, _agentic_gate=None
        )
        assert answered["answered_now"] is True
        assert answered["decided"]["option"] == "B"
