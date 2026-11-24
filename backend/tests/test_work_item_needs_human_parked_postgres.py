"""A work item that needs a person waits for the answer (#497).

An item created open with ``needs_human`` (or turned ``needs_human`` while
open) was claimable by its owner's work-item inbox in the same second, before
any person answered. It now lands ``blocked``: the inbox dispatches open
items only, and ``answer_work_item`` reopens it with the answer.
"""

from __future__ import annotations

import pytest

import app.services.work_item as wi_service
from app.services.company_managed_writes import agent_writer, person_writer
from tests.test_work_item_workspace_scope_postgres import (
    _make_agent,
    _make_user,
    _pointed_at_db,
    _temp_database,
)

pytestmark = pytest.mark.integration

_ASK = {
    "question": "Ship now or after the review?",
    "options": [{"key": "A", "label": "Now"}, {"key": "B", "label": "After the review"}],
    "recommendation": "B",
}


@pytest.mark.asyncio
async def test_a_decision_created_open_lands_blocked_and_reopens_on_the_answer() -> None:
    async with _temp_database() as sessions:
        async with sessions() as session:
            person = await _make_user(session, email="decider@example.com")
            agent = await _make_agent(session)
            await session.commit()

        async with _pointed_at_db(sessions):
            uid = str(person.id)
            writer = agent_writer(is_company_managed=False, agent_id=agent.id)
            item = await wi_service.create_work_item(
                user_id=uid,
                title="decide",
                owner_agent_id=str(agent.id),
                created_by_agent_id=str(agent.id),
                needs_human=True,
                ask=_ASK,
                writer=writer,
            )
            inbox = await wi_service.list_dispatchable_work_items(
                owner_agent_id=str(agent.id), user_id=uid, writer=writer
            )
            answered = await wi_service.answer_work_item(
                item["id"], user_id=uid, option="A", writer=person_writer(is_admin=False)
            )

    assert item["status"] == "blocked"
    assert item["needs_human"] is True
    assert item["claimed_by_run_id"] is None
    assert item["id"] not in [row["id"] for row in inbox]
    assert answered["status"] == "open"
    assert answered["needs_human"] is False


@pytest.mark.asyncio
async def test_an_open_item_turned_needs_human_leaves_the_queue() -> None:
    async with _temp_database() as sessions:
        async with sessions() as session:
            person = await _make_user(session, email="decider2@example.com")
            agent = await _make_agent(session)
            await session.commit()

        async with _pointed_at_db(sessions):
            uid = str(person.id)
            writer = agent_writer(is_company_managed=False, agent_id=agent.id)
            item = await wi_service.create_work_item(
                user_id=uid,
                title="work",
                owner_agent_id=str(agent.id),
                created_by_agent_id=str(agent.id),
                writer=writer,
            )
            parked = await wi_service.update_work_item(
                item["id"], user_id=uid, needs_human=True, ask=_ASK, writer=writer
            )
            done = await wi_service.create_work_item(
                user_id=uid,
                title="finished",
                owner_agent_id=str(agent.id),
                created_by_agent_id=str(agent.id),
                status="in_progress",
                needs_human=True,
                ask=_ASK,
                blocked_reason="later",
                writer=writer,
            )

    assert item["status"] == "open"
    assert parked is not None
    assert parked["status"] == "blocked"
    assert parked["needs_human"] is True
    # Only an item that would land open is parked: another status is kept as given.
    assert done["status"] == "in_progress"
