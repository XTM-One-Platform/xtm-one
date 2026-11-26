"""Who a needs_human work item waits for (#11893).

A company-managed agent in a run that acts for no person stored ``user_id``
NULL on its decision item, which then reached no bell, no dashboard notice and
no "Waiting for me" view. The recipient is now resolved at the write: an
explicit recipient, else the run's person, else the agent's owner, else the
administrators; with nobody at all the write is refused, nothing written.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select

import app.services.work_item as wi_service
from app.models.work_item import WorkItem
from app.services.company_managed_writes import agent_writer
from tests.test_work_item_workspace_scope_postgres import (
    _make_agent,
    _make_user,
    _pointed_at_db,
    _temp_database,
)

pytestmark = pytest.mark.integration

_ASK = {
    "question": "Ship the change now or after the review?",
    "options": [
        {"key": "A", "label": "Now"},
        {"key": "B", "label": "After the review"},
    ],
    "recommendation": "B",
    "deadline": "2026-12-01T09:00:00Z",
}


async def _decision(agent: Any, *, user_id: str | None = None, recipient: str | None = None) -> dict[str, Any]:
    return await wi_service.create_work_item(
        user_id=user_id,
        title="decide",
        owner_agent_id=str(agent.id),
        created_by_agent_id=str(agent.id),
        status="blocked",
        needs_human=True,
        ask=_ASK,
        recipient=recipient,
        is_company_managed=True,
        writer=agent_writer(is_company_managed=True, agent_id=agent.id),
    )


async def _waiting(person: Any) -> tuple[int, list[str]]:
    counted = await wi_service.work_items_waiting_for_person(user_id=str(person.id))
    listed = await wi_service.list_work_items_waiting_for_person(user_id=str(person.id), limit=10)
    return counted["count"], [row["id"] for row in listed]


@pytest.mark.asyncio
async def test_a_company_managed_decision_with_no_acting_user_and_no_admin_reaches_the_agents_owner() -> None:
    # With an administrator the item waits for the administrators instead (#2367,
    # tests/test_work_item_company_recipient_2367_postgres.py): the owner of a
    # company-managed agent is the seed account, reached only when nobody else is.
    async with _temp_database() as sessions:
        async with sessions() as session:
            owner = await _make_user(session, email="owner@example.com")
            bystander = await _make_user(session, email="bystander@example.com")
            agent = await _make_agent(session)
            agent.user_id = owner.id
            agent.is_company_managed = True
            await session.commit()

        async with _pointed_at_db(sessions):
            item = await _decision(agent)
            mine = await _waiting(owner)
            theirs = await _waiting(bystander)

    assert item["user_id"] is None
    assert item["waits_for_user_id"] == str(owner.id)
    assert mine == (1, [item["id"]])
    assert theirs == (0, [])


@pytest.mark.asyncio
async def test_the_acting_user_is_the_recipient_when_the_run_acts_for_one() -> None:
    async with _temp_database() as sessions:
        async with sessions() as session:
            person = await _make_user(session, email="actor@example.com")
            owner = await _make_user(session, email="agent-owner@example.com")
            agent = await _make_agent(session)
            agent.user_id = owner.id
            await session.commit()

        async with _pointed_at_db(sessions):
            item = await _decision(agent, user_id=str(person.id))
            actor = await _waiting(person)
            agent_owner = await _waiting(owner)

    # The person the run acts for is the item's user: nothing else stored.
    assert item["waits_for_user_id"] is None
    assert actor == (1, [item["id"]])
    assert agent_owner == (0, [])


@pytest.mark.asyncio
async def test_an_explicit_recipient_wins_and_an_unknown_one_writes_nothing() -> None:
    async with _temp_database() as sessions:
        async with sessions() as session:
            owner = await _make_user(session, email="owner2@example.com")
            chosen = await _make_user(session, email="Chosen@example.com")
            agent = await _make_agent(session)
            agent.user_id = owner.id
            await session.commit()

        async with _pointed_at_db(sessions):
            item = await _decision(agent, recipient="chosen@example.com")
            chosen_waits = await _waiting(chosen)
            with pytest.raises(ValueError, match="not an active user"):
                await _decision(agent, recipient="nobody@example.com")
            with pytest.raises(ValueError, match="set needs_human too"):
                await wi_service.create_work_item(
                    user_id=None,
                    title="no decision",
                    created_by_agent_id=str(agent.id),
                    recipient=str(chosen.id),
                    is_company_managed=True,
                    writer=agent_writer(is_company_managed=True, agent_id=agent.id),
                )
        async with sessions() as session:
            total = (await session.execute(select(func.count()).select_from(WorkItem))).scalar_one()

    assert item["waits_for_user_id"] == str(chosen.id)
    assert chosen_waits == (1, [item["id"]])
    assert total == 1


@pytest.mark.asyncio
async def test_a_recipient_who_cannot_see_a_personal_item_is_refused() -> None:
    async with _temp_database() as sessions:
        async with sessions() as session:
            person = await _make_user(session, email="author@example.com")
            outsider = await _make_user(session, email="outsider@example.com")
            agent = await _make_agent(session)
            await session.commit()

        async with _pointed_at_db(sessions):
            with pytest.raises(ValueError, match="cannot see this work item"):
                await wi_service.create_work_item(
                    user_id=str(person.id),
                    title="private decision",
                    owner_agent_id=str(agent.id),
                    created_by_agent_id=str(agent.id),
                    status="blocked",
                    needs_human=True,
                    ask=_ASK,
                    recipient=str(outsider.id),
                    writer=agent_writer(is_company_managed=False, agent_id=agent.id),
                )


@pytest.mark.asyncio
async def test_a_decision_nobody_owns_waits_for_the_administrators_or_is_refused() -> None:
    async with _temp_database() as sessions:
        async with sessions() as session:
            agent = await _make_agent(session)
            await session.commit()

        async with _pointed_at_db(sessions):
            # No acting user, no agent owner, no administrator: refused.
            with pytest.raises(ValueError, match="nobody can be notified"):
                await _decision(agent)

        async with sessions() as session:
            admin = await _make_user(session, email="admin@example.com", is_admin=True)
            member = await _make_user(session, email="member@example.com")
            await session.commit()

        async with _pointed_at_db(sessions):
            item = await _decision(agent)
            admin_waits = await _waiting(admin)
            member_waits = await _waiting(member)

    assert item["user_id"] is None and item["waits_for_user_id"] is None
    assert admin_waits == (1, [item["id"]])
    assert member_waits == (0, [])


@pytest.mark.asyncio
async def test_update_to_needs_human_resolves_the_recipient() -> None:
    async with _temp_database() as sessions:
        async with sessions() as session:
            owner = await _make_user(session, email="owner3@example.com")
            agent = await _make_agent(session)
            agent.user_id = owner.id
            await session.commit()

        async with _pointed_at_db(sessions):
            writer = agent_writer(is_company_managed=True, agent_id=agent.id)
            item = await wi_service.create_work_item(
                user_id=None,
                title="later a decision",
                owner_agent_id=str(agent.id),
                created_by_agent_id=str(agent.id),
                is_company_managed=True,
                writer=writer,
            )
            before = await _waiting(owner)
            updated = await wi_service.update_work_item(
                item["id"],
                user_id=None,
                status="blocked",
                needs_human=True,
                ask=_ASK,
                writer=writer,
            )
            after = await _waiting(owner)

    assert before == (0, [])
    assert updated is not None
    assert updated["waits_for_user_id"] == str(owner.id)
    assert after == (1, [item["id"]])


def test_recipient_is_declared_on_create_and_update() -> None:
    from app.services.builtin_tools.work_items import _BLOCKED_WHY_SCHEMA

    assert "recipient" in _BLOCKED_WHY_SCHEMA
