"""Decision items stay right at both ends (#1719).

* A ``keeps_waiting`` option records the answer (``payload.answer`` and a
  stamped note) and leaves the item blocked on the person with its ask:
  no status change, no claim cleared, nothing for the owner's inbox.
* A needs_human item finished by ``complete_work_item`` or by
  ``update_work_item(status=...)`` no longer waits for a person: its
  ``needs_human`` and ask are cleared and the person's waiting count drops.
"""

from __future__ import annotations

from typing import Any

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
    "question": "Ship the patch now?",
    "context": "The fix is ready.",
    "options": [
        {"key": "ship", "label": "Ship it"},
        {"key": "wait", "label": "Keep waiting", "keeps_waiting": True},
    ],
    "recommendation": "ship",
}


def _asked(person: Any, agent: Any, title: str) -> Any:
    return _row(
        person,
        title=title,
        owner_agent_id=agent.id,
        status="blocked",
        needs_human=True,
        blocked_reason="ship decision",
        ask=_ASK,
        description="brief",
    )


@pytest.mark.asyncio
async def test_a_keep_waiting_answer_records_it_and_leaves_the_item_waiting() -> None:
    writer = person_writer(is_admin=False)
    async with _temp_database() as sessions:
        async with sessions() as session:
            person = await _make_user(session, email="keep@example.com")
            agent = await _make_agent(session)
            item = _asked(person, agent, "keep")
            session.add(item)
            await session.commit()
            item_id = str(item.id)

        async with _pointed_at_db(sessions):
            uid = str(person.id)
            kept = await wi_service.answer_work_item(
                item_id, user_id=uid, option="WAIT", answered_by="Person", writer=writer
            )
            waiting = await wi_service.work_items_waiting_for_person(user_id=uid)
            shipped = await wi_service.answer_work_item(item_id, user_id=uid, option="ship", writer=writer)

    assert kept is not None and shipped is not None
    assert kept["status"] == "blocked"
    assert kept["needs_human"] is True
    assert kept["ask"]["question"] == "Ship the patch now?"
    assert kept["blocked_reason"] == "ship decision"
    assert kept["payload"]["answer"]["option"] == "wait"
    assert "ANSWERED by Person: [wait] Keep waiting (keeps waiting)" in kept["description"].splitlines()[0]
    assert waiting["count"] == 1
    # A later answer that decides still releases it to the owner.
    assert shipped["status"] == "open"
    assert shipped["needs_human"] is False
    assert shipped["payload"]["answer"]["option"] == "ship"


@pytest.mark.asyncio
async def test_a_finished_item_no_longer_waits_for_a_person() -> None:
    writer = person_writer(is_admin=False)
    async with _temp_database() as sessions:
        async with sessions() as session:
            person = await _make_user(session, email="finish@example.com")
            agent = await _make_agent(session)
            completed = _asked(person, agent, "completed")
            cancelled = _asked(person, agent, "cancelled")
            session.add_all([completed, cancelled])
            await session.commit()
            completed_id, cancelled_id = str(completed.id), str(cancelled.id)

        async with _pointed_at_db(sessions):
            uid = str(person.id)
            done = await wi_service.complete_work_item(completed_id, user_id=uid, result="decided", writer=writer)
            dropped = await wi_service.update_work_item(cancelled_id, user_id=uid, status="cancelled", writer=writer)
            noted = await wi_service.update_work_item(completed_id, user_id=uid, note="after", writer=writer)
            waiting = await wi_service.work_items_waiting_for_person(user_id=uid)

    assert done is not None and dropped is not None and noted is not None
    for finished in (done, dropped, noted):
        assert finished["needs_human"] is False
        assert finished["ask"] is None
    assert done["status"] == "done" and dropped["status"] == "cancelled"
    assert waiting["count"] == 0
