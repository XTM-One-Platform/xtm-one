"""What the home dashboard counts: the work items that wait for the person.

The notice used to read the stale count, so an item an agent owns and that
waits on a pull request or a deployment, past a due date, turned the
person's dashboard red although nothing was owed by them.
``work_items_waiting_for_person`` counts only a decision the person owes
(``needs_human``) or an item of theirs with no owner agent left open past due.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

import app.services.work_item as wi_service
from app.models.work_item import WorkItem
from tests.test_work_item_workspace_scope_postgres import (
    _make_agent,
    _make_user,
    _pointed_at_db,
    _temp_database,
)

pytestmark = pytest.mark.integration


def _row(user, **overrides) -> WorkItem:
    now = datetime.now(UTC)
    base = dict(
        id=uuid.uuid4(),
        user_id=user.id,
        is_company_managed=False,
        title="item",
        status="open",
        priority="medium",
        source="agent",
        payload={},
        created_at=now - timedelta(days=3),
        last_activity_at=now - timedelta(days=3),
    )
    base.update(overrides)
    return WorkItem(**base)


@pytest.mark.asyncio
async def test_agent_waits_are_not_the_persons_and_a_decision_owed_is() -> None:
    past = datetime.now(UTC) - timedelta(days=2)
    async with _temp_database() as sessions:
        async with sessions() as session:
            person = await _make_user(session, email="person@example.com")
            other = await _make_user(session, email="other@example.com")
            agent = await _make_agent(session)
            session.add_all(
                [
                    # An agent's item blocked on a pull request, past due: not the person's.
                    _row(
                        person,
                        owner_agent_id=agent.id,
                        status="blocked",
                        blocked_on_refs=[{"kind": "pull_request", "repo": "o/r", "number": 1}],
                        due_at=past,
                    ),
                    # An agent's open item past due (the inbox's pick-up): not the person's.
                    _row(person, owner_agent_id=agent.id, due_at=past),
                    # An agent's item quiet for days: not the person's.
                    _row(person, owner_agent_id=agent.id, status="in_progress"),
                    # A scheduled action past its time: never counted.
                    _row(person, due_at=past, action_mode="exact"),
                    # Done: never counted.
                    _row(person, due_at=past, status="done"),
                    # Someone else's decision: not this person's.
                    _row(other, owner_agent_id=agent.id, status="blocked", needs_human=True),
                    # A decision the person owes on an agent's item: counted.
                    _row(
                        person,
                        title="decide",
                        owner_agent_id=agent.id,
                        status="blocked",
                        needs_human=True,
                        blocked_reason="pick a pricing tier",
                        last_activity_at=datetime.now(UTC) - timedelta(days=5),
                    ),
                    # The person's own item, no owner agent, open past due: counted.
                    _row(person, title="mine", due_at=past),
                ]
            )
            await session.commit()

        async with _pointed_at_db(sessions):
            mine = await wi_service.work_items_waiting_for_person(user_id=str(person.id))
            theirs = await wi_service.work_items_waiting_for_person(user_id=str(other.id))
            nobody = await wi_service.work_items_waiting_for_person(user_id=None)
            # The Operations "Waiting for me" view and its count read the same
            # population as the notice (#9437 slice 2), oldest first.
            listed = await wi_service.list_work_items(user_id=str(person.id), waiting_for_me=True)
            counted = await wi_service.count_work_items(user_id=str(person.id), waiting_for_me=True)
            listed_other = await wi_service.list_work_items(user_id=str(other.id), waiting_for_me=True)
            listed_nobody = await wi_service.list_work_items(user_id=None, waiting_for_me=True)

    assert mine["count"] == 2
    assert [i["title"] for i in listed] == ["decide", "mine"]
    assert counted == mine["count"]
    assert len(listed_other) == theirs["count"] == 1
    assert listed_nobody == []
    assert mine["oldest"]["title"] == "decide"
    assert mine["oldest"]["reason"] == "pick a pricing tier"
    assert mine["oldest"]["since"]
    assert theirs["count"] == 1
    assert nobody == {"count": 0, "oldest": None}


@pytest.mark.asyncio
async def test_an_agent_owned_blocked_item_alone_counts_zero() -> None:
    async with _temp_database() as sessions:
        async with sessions() as session:
            person = await _make_user(session, email="solo@example.com")
            agent = await _make_agent(session)
            session.add(
                _row(
                    person,
                    owner_agent_id=agent.id,
                    status="blocked",
                    blocked_on_refs=[{"kind": "deployment", "repo": "o/r", "number": 2}],
                    due_at=datetime.now(UTC) - timedelta(hours=1),
                )
            )
            await session.commit()

        async with _pointed_at_db(sessions):
            out = await wi_service.work_items_waiting_for_person(user_id=str(person.id))

    assert out == {"count": 0, "oldest": None}


@pytest.mark.asyncio
async def test_the_deadline_and_the_pickup_time_are_two_fields_each_cleared_by_null() -> None:
    """``due_at`` is the deadline, ``not_before`` the pickup time (#8753):
    the inbox takes an item whose deadline is ahead at once, waits for a
    future ``not_before``, and an update clears either one with ``""``."""
    from app.services.company_managed_writes import PLATFORM_WRITER, agent_writer

    ahead = datetime.now(UTC) + timedelta(days=2)
    async with _temp_database() as sessions:
        async with sessions() as session:
            person = await _make_user(session, email="lever@example.com")
            agent = await _make_agent(session)
            dated = _row(person, title="dated", owner_agent_id=agent.id, due_at=ahead)
            later = _row(person, title="later", owner_agent_id=agent.id, not_before=ahead)
            session.add_all([dated, later])
            await session.commit()
            dated_id, later_id = str(dated.id), str(later.id)

        async with _pointed_at_db(sessions):
            writer = agent_writer(is_company_managed=False, agent_id=agent.id)
            first = await wi_service.list_dispatchable_work_items(
                owner_agent_id=str(agent.id), user_id=str(person.id), writer=writer
            )
            cleared = await wi_service.update_work_item(
                later_id, user_id=str(person.id), not_before="", writer=PLATFORM_WRITER
            )
            undated = await wi_service.update_work_item(
                dated_id, user_id=str(person.id), due_at="", writer=PLATFORM_WRITER
            )
            kept = await wi_service.update_work_item(
                later_id, user_id=str(person.id), priority="high", writer=PLATFORM_WRITER
            )
            second = await wi_service.list_dispatchable_work_items(
                owner_agent_id=str(agent.id), user_id=str(person.id), writer=writer
            )

    assert {d["id"] for d in first} == {dated_id}
    assert cleared["not_before"] is None
    assert undated["due_at"] is None
    # A write that names neither date leaves both as they are.
    assert kept["not_before"] is None and kept["priority"] == "high"
    assert {d["id"] for d in second} == {dated_id, later_id}
