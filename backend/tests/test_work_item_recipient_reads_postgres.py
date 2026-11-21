"""A decision's recipient opens and answers the item the bell links to (#901).

An agent of one person's fleet files a ``needs_human`` item in that fleet's
workspace, owned by an agent private to another person who is its recipient
(``waits_for_user_id``). The bell listed it for the recipient, but the read,
the answer and the prefix lookup went through tiers that never named the
recipient: the dialog said "no longer available". The stored recipient now
reads and answers it; the filing person's view is unchanged and a stranger
still sees nothing.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.work_item as wi_service
from app.models.work_item import WorkItem
from app.services.company_managed_writes import person_writer
from app.services.workspaces import create_workspace
from tests.test_work_item_workspace_scope_postgres import (
    _make_agent,
    _make_user,
    _pointed_at_db,
    _temp_database,
)

pytestmark = pytest.mark.integration

_ASK = {
    "question": "Keep the hourly watch?",
    "options": [{"key": "A", "label": "Keep"}, {"key": "B", "label": "Stop"}],
    "recommendation": "A",
}


async def _cross_fleet_decision(sessions: async_sessionmaker[AsyncSession]) -> tuple[str, str, str, str]:
    """U1's workspace holds an item owned by U2's private agent, waiting for U2."""
    async with sessions() as session:
        filer = await _make_user(session, email="filer@example.com")
        recipient = await _make_user(session, email="recipient@example.com")
        stranger = await _make_user(session, email="stranger@example.com")
        filing_agent = await _make_agent(session, name="Other fleet assistant")
        filing_agent.user_id = filer.id
        owner_agent = await _make_agent(session, name="Recipient assistant")
        owner_agent.user_id = recipient.id
        ws = await create_workspace(session, user_id=filer.id, name="Other fleet")
        await session.flush()
        item = WorkItem(
            id=uuid.uuid4(),
            user_id=filer.id,
            title="Hourly watch decision",
            workspace_id=ws.id,
            owner_agent_id=owner_agent.id,
            created_by_agent_id=filing_agent.id,
            status="blocked",
            needs_human=True,
            ask=_ASK,
            waits_for_user_id=recipient.id,
        )
        session.add(item)
        await session.commit()
        return str(item.id), str(filer.id), str(recipient.id), str(stranger.id)


@pytest.mark.asyncio
async def test_the_recipient_opens_lists_and_answers_the_item_the_bell_links_to() -> None:
    async with _temp_database() as sessions:
        item_id, filer_id, recipient_id, stranger_id = await _cross_fleet_decision(sessions)
        async with _pointed_at_db(sessions):
            bell = await wi_service.list_work_items_waiting_for_person(user_id=recipient_id, limit=10)
            opened = await wi_service.get_work_item(item_id, user_id=recipient_id)
            filer_view = await wi_service.get_work_item(item_id, user_id=filer_id)
            stranger_view = await wi_service.get_work_item(item_id, user_id=stranger_id)
            answered = await wi_service.answer_work_item(
                item_id,
                user_id=recipient_id,
                option="A",
                answered_by="recipient",
                writer=person_writer(is_admin=False),
            )

    assert [row["id"] for row in bell] == [item_id]
    assert opened is not None and opened["ask"]["question"] == "Keep the hourly watch?"
    assert filer_view is not None, "the filing person keeps their workspace view"
    assert stranger_view is None
    assert answered is not None
    assert answered["status"] == "open"
    assert answered["payload"]["answer"]["option"] == "A"


@pytest.mark.asyncio
async def test_a_stranger_cannot_answer() -> None:
    async with _temp_database() as sessions:
        item_id, _filer_id, _recipient_id, stranger_id = await _cross_fleet_decision(sessions)
        async with _pointed_at_db(sessions):
            answered = await wi_service.answer_work_item(
                item_id, user_id=stranger_id, option="A", writer=person_writer(is_admin=False)
            )
    assert answered is None


@pytest.mark.asyncio
async def test_prefix_and_full_id_reads_agree_for_the_same_caller() -> None:
    async with _temp_database() as sessions:
        item_id, _filer_id, recipient_id, stranger_id = await _cross_fleet_decision(sessions)
        async with _pointed_at_db(sessions):
            mine = await wi_service.work_items_by_id_prefix(item_id[:8], user_id=recipient_id)
            full = await wi_service.get_work_item(item_id, user_id=recipient_id)
            theirs = await wi_service.work_items_by_id_prefix(item_id[:8], user_id=stranger_id)

    assert full is not None
    assert [row["id"] for row in mine] == [item_id]
    assert theirs == []
