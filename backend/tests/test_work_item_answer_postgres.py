"""A person answers the decision a work item waits for (#9437, slice 3).

``answer_work_item`` records the answer (``payload.answer`` and a stamped
note), clears ``needs_human`` and the ask, and puts the item back in its
owner agent's queue, so the owner's inbox picks it up with the answer in
its context. An item that waits for nobody, or an option the ask does not
list, is refused and nothing changes.
"""

from __future__ import annotations

import pytest

import app.services.work_item as wi_service
from app.services.company_managed_writes import person_writer
from tests.test_work_item_waiting_for_person_postgres import _row
from tests.test_work_item_workspace_scope_postgres import (
    _make_agent,
    _make_user,
    _pointed_at_db,
    _temp_database,
)

pytestmark = pytest.mark.integration

_ASK = {
    "question": "Which pricing tier ships first?",
    "options": [
        {"key": "A", "label": "Starter", "effect": "the starter tier ships first"},
        {"key": "B", "label": "Enterprise"},
    ],
    "recommendation": "A",
}


@pytest.mark.asyncio
async def test_an_option_answer_reopens_the_item_to_its_owner() -> None:
    writer = person_writer(is_admin=False)
    async with _temp_database() as sessions:
        async with sessions() as session:
            person = await _make_user(session, email="answer@example.com")
            agent = await _make_agent(session)
            asked = _row(
                person,
                title="asked",
                owner_agent_id=agent.id,
                status="blocked",
                needs_human=True,
                blocked_reason="pick a tier",
                ask=_ASK,
                description="brief",
                payload={"keep": 1, "_dispatch_attempts": 3},
            )
            legacy = _row(
                person,
                title="legacy",
                owner_agent_id=agent.id,
                status="blocked",
                needs_human=True,
                blocked_reason="which customer environment?",
            )
            idle = _row(person, title="idle", owner_agent_id=agent.id, status="in_progress")
            session.add_all([asked, legacy, idle])
            await session.commit()
            asked_id, legacy_id, idle_id = str(asked.id), str(legacy.id), str(idle.id)

        async with _pointed_at_db(sessions):
            uid = str(person.id)
            with pytest.raises(ValueError, match="not one of the ask's options"):
                await wi_service.answer_work_item(asked_id, user_id=uid, option="Z", writer=writer)
            with pytest.raises(ValueError, match="needs an option or a text"):
                await wi_service.answer_work_item(asked_id, user_id=uid, writer=writer)
            with pytest.raises(wi_service.WorkItemNotWaitingError):
                await wi_service.answer_work_item(idle_id, user_id=uid, text="ok", writer=writer)
            answered = await wi_service.answer_work_item(
                asked_id, user_id=uid, option="b", text="the board asked for it", answered_by="Person", writer=writer
            )
            free = await wi_service.answer_work_item(legacy_id, user_id=uid, text="production", writer=writer)
            waiting = await wi_service.work_items_waiting_for_person(user_id=uid)
            with pytest.raises(wi_service.WorkItemNotWaitingError):
                await wi_service.answer_work_item(asked_id, user_id=uid, option="A", writer=writer)

    assert answered["status"] == "open"
    assert answered["needs_human"] is False
    assert answered["ask"] is None
    assert answered["blocked_reason"] is None
    answer = answered["payload"]["answer"]
    assert answer["option"] == "B" and answer["label"] == "Enterprise"
    assert answer["text"] == "the board asked for it"
    assert answer["answered_by"] == "Person"
    assert answer["question"] == "Which pricing tier ships first?"
    assert answer["answered_at"]
    assert answered["payload"]["keep"] == 1
    assert "_dispatch_attempts" not in answered["payload"]
    first_line = answered["description"].splitlines()[0]
    assert "ANSWERED by Person: [B] Enterprise: the board asked for it" in first_line
    assert answered["description"].endswith("brief")

    assert free["status"] == "open"
    assert free["payload"]["answer"]["text"] == "production"
    assert free["payload"]["answer"]["question"] == "which customer environment?"
    assert "option" not in free["payload"]["answer"]

    assert waiting["count"] == 0
