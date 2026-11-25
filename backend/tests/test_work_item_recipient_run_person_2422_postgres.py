"""A company-managed agent's decision reaches the person its run acts for (#2422).

A company-managed assignment run carries no file owner (``_resolve_run_file_owner_id``
keeps it empty, file-access isolation), so its work-item tools acted for nobody and
a decision filed with no recipient went to every administrator (before #2374: the
seed account, incident #2390). The run's person (triggering user, else run-as user)
now travels on the agent writer, apart from the file owner, and is tried after the
CTEM ``decide`` groups. An explicit recipient a company-managed agent names must be
a decider: the seed account, or a person who decides nothing, is dropped and the
routing applies, the decision is kept.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

import app.services.work_item as wi_service
from app.services.assignment_engine import _resolve_run_file_owner_id
from app.services.company_managed_writes import agent_writer
from tests.test_work_item_company_recipient_2367_postgres import (
    _ASK,
    _company_agent,
    _decide_group,
    _home_fleet,
)
from tests.test_work_item_workspace_scope_postgres import (
    _make_user,
    _pointed_at_db,
    _temp_database,
)

pytestmark = pytest.mark.integration


async def _decision(
    agent: Any, *, run_person: Any = None, recipient: str | None = None, key: str | None = None
) -> dict[str, Any]:
    """A needs_human item a company-managed run files with no file owner, as the run's tools do."""
    return await wi_service.create_work_item(
        user_id=None,
        title="decide",
        owner_agent_id=str(agent.id),
        created_by_agent_id=str(agent.id),
        status="blocked",
        needs_human=True,
        ask=_ASK,
        recipient=recipient,
        correlation_key=key,
        is_company_managed=True,
        writer=agent_writer(
            is_company_managed=True,
            agent_id=agent.id,
            run_person_id=getattr(run_person, "id", None),
        ),
    )


@pytest.mark.asyncio
async def test_regression_2390_a_webhook_run_decision_waits_for_its_run_as_person() -> None:
    async with _temp_database() as sessions:
        async with sessions() as session:
            seed_owner = await _make_user(session, email="seed-a@example.com")
            admin = await _make_user(session, email="admin-a@example.com", is_admin=True)
            run_as = await _make_user(session, email="run-as-a@example.com")
            agent = await _company_agent(session, seed_owner)
            await _home_fleet(session, agent, owner_id=seed_owner.id, slug="detection-like", builtin=True)
            await session.commit()

        async with _pointed_at_db(sessions):
            item = await _decision(agent, run_person=run_as)
            mine = await wi_service.work_items_waiting_for_person(user_id=str(run_as.id))
            admins = await wi_service.work_items_waiting_for_person(user_id=str(admin.id))
            seed = await wi_service.work_items_waiting_for_person(user_id=str(seed_owner.id))

    assert item["waits_for_user_id"] == str(run_as.id)
    assert not item.get("waits_for_group_ids")
    assert mine["count"] == 1
    assert seed["count"] == 0
    # An administrator is not the recipient: the item is not in the administrators' queue.
    assert admins["count"] == 0


@pytest.mark.asyncio
async def test_ctem_work_still_goes_to_the_decide_groups_before_the_run_person() -> None:
    async with _temp_database() as sessions:
        async with sessions() as session:
            seed_owner = await _make_user(session, email="seed-b@example.com")
            await _make_user(session, email="admin-b@example.com", is_admin=True)
            run_as = await _make_user(session, email="run-as-b@example.com")
            decider = await _make_user(session, email="decider-b@example.com")
            agent = await _company_agent(session, seed_owner)
            await _home_fleet(session, agent, owner_id=seed_owner.id, slug="ctem-loop", builtin=True)
            group = await _decide_group(session, decider)
            await session.commit()

        async with _pointed_at_db(sessions):
            item = await _decision(agent, run_person=run_as)

    assert item["waits_for_group_ids"] == [str(group.id)]
    assert item["waits_for_user_id"] is None


@pytest.mark.asyncio
async def test_no_run_person_and_no_fleet_owner_waits_for_the_administrators() -> None:
    async with _temp_database() as sessions:
        async with sessions() as session:
            seed_owner = await _make_user(session, email="seed-c@example.com")
            admin = await _make_user(session, email="admin-c@example.com", is_admin=True)
            agent = await _company_agent(session, seed_owner)
            await session.commit()

        async with _pointed_at_db(sessions):
            item = await _decision(agent)
            admins = await wi_service.work_items_waiting_for_person(user_id=str(admin.id))

    assert item["waits_for_user_id"] is None
    assert not item.get("waits_for_group_ids")
    assert admins["count"] == 1


@pytest.mark.asyncio
async def test_the_run_person_comes_before_the_fleet_owner_and_is_never_the_seed_account() -> None:
    async with _temp_database() as sessions:
        async with sessions() as session:
            seed_owner = await _make_user(session, email="seed-d@example.com")
            await _make_user(session, email="admin-d@example.com", is_admin=True)
            fleet_owner = await _make_user(session, email="fleet-owner-d@example.com")
            run_as = await _make_user(session, email="run-as-d@example.com")
            agent = await _company_agent(session, seed_owner)
            await _home_fleet(session, agent, owner_id=fleet_owner.id, slug="persons-desk", builtin=False)
            await session.commit()

        async with _pointed_at_db(sessions):
            by_run = await _decision(agent, run_person=run_as)
            # A run acting as the seed account (the assignment's owner) is no decider: the fleet owner.
            by_seed = await _decision(agent, run_person=seed_owner)

    assert by_run["waits_for_user_id"] == str(run_as.id)
    assert by_seed["waits_for_user_id"] == str(fleet_owner.id)


@pytest.mark.asyncio
async def test_an_explicit_seed_account_is_dropped_and_the_routing_applies(monkeypatch: Any) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "admin_email", "local-admin-e@example.com")
    async with _temp_database() as sessions:
        async with sessions() as session:
            seed_owner = await _make_user(session, email="seed-e@example.com")
            local_admin = await _make_user(session, email="local-admin-e@example.com", is_admin=True)
            await _make_user(session, email="admin-e@example.com", is_admin=True)
            run_as = await _make_user(session, email="run-as-e@example.com")
            bystander = await _make_user(session, email="bystander-e@example.com")
            agent = await _company_agent(session, seed_owner)
            await session.commit()

        async with _pointed_at_db(sessions):
            to_owner = await _decision(agent, run_person=run_as, recipient=str(seed_owner.id))
            to_admin_email = await _decision(agent, run_person=run_as, recipient="local-admin-e@example.com")
            to_bystander = await _decision(agent, run_person=run_as, recipient=str(bystander.id))

    for item in (to_owner, to_admin_email, to_bystander):
        assert item["waits_for_user_id"] == str(run_as.id)
    assert to_admin_email["waits_for_user_id"] != str(local_admin.id)


@pytest.mark.asyncio
async def test_an_explicit_run_person_or_administrator_is_kept() -> None:
    async with _temp_database() as sessions:
        async with sessions() as session:
            seed_owner = await _make_user(session, email="seed-f@example.com")
            admin = await _make_user(session, email="admin-f@example.com", is_admin=True)
            run_as = await _make_user(session, email="run-as-f@example.com")
            agent = await _company_agent(session, seed_owner)
            await session.commit()

        async with _pointed_at_db(sessions):
            to_run = await _decision(agent, run_person=run_as, recipient="run-as-f@example.com")
            to_admin = await _decision(agent, run_person=run_as, recipient=str(admin.id))

    assert to_run["waits_for_user_id"] == str(run_as.id)
    assert to_admin["waits_for_user_id"] == str(admin.id)


@pytest.mark.asyncio
async def test_a_ctem_decide_member_may_be_named_for_ctem_work() -> None:
    async with _temp_database() as sessions:
        async with sessions() as session:
            seed_owner = await _make_user(session, email="seed-g@example.com")
            await _make_user(session, email="admin-g@example.com", is_admin=True)
            decider = await _make_user(session, email="decider-g@example.com")
            agent = await _company_agent(session, seed_owner)
            await _home_fleet(session, agent, owner_id=seed_owner.id, slug="ctem-loop", builtin=True)
            await _decide_group(session, decider)
            await session.commit()

        async with _pointed_at_db(sessions):
            item = await _decision(agent, recipient=str(decider.id))

    assert item["waits_for_user_id"] == str(decider.id)


def test_a_company_managed_run_keeps_no_file_owner() -> None:
    """The run's person routes decisions; file access of a company-managed run is unchanged."""
    person = uuid.uuid4()
    company_run = SimpleNamespace(is_company_managed=True, user_id=person)
    personal_run = SimpleNamespace(is_company_managed=False, user_id=person)

    assert _resolve_run_file_owner_id(company_run) is None  # type: ignore[arg-type]
    assert _resolve_run_file_owner_id(personal_run) == str(person)  # type: ignore[arg-type]
    assert agent_writer(is_company_managed=True, run_person_id=person).run_person_id == person
