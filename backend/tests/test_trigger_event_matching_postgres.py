"""A trigger's event-matching mode across real saves and copies, against PostgreSQL.

``settle_event_matching`` runs in the model's ``before_update`` listener and
reads what SQLAlchemy recorded as the stored value - the part a fake session
cannot show: the REST route loads the row, locks it and re-reads it
(``populate_existing``), sets the fields the request sent, commits, refreshes.
A trigger saved before exact matching must keep its prefix match through every
save that leaves its events alone and through a copy, switch only when its
events are chosen again or the switch is asked for, and a row the agent
builder stored under ``event_type`` must be read through that key only
once switched. Same scratch-database recipe as ``test_provisioning_postgres.py``.
"""

from __future__ import annotations

import uuid
from copy import deepcopy

import pytest
from fastapi import Request
from sqlalchemy import select

from app.api.v1 import assignments as assignments_api
from app.api.v1.schemas.assignment import CreateAssignmentRequest
from app.core.utils import ensure_webhook_secret, strip_webhook_secret
from app.models.agent import Agent
from app.models.agent_copy import build_assignment_copy_kwargs
from app.models.assignment import (
    EVENT_MATCHING_EXACT,
    EVENT_MATCHING_KEY,
    Assignment,
    event_read_from_legacy_key,
)
from app.models.integration import Integration
from app.models.user import User
from tests.test_assignment_update_explicit_null_postgres import _put, _seed, _stored
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration

LEGACY = {"provider": "github", "event": "pull_request", "integration_id": "i-1"}
EXACT = {EVENT_MATCHING_KEY: EVENT_MATCHING_EXACT}


async def _stored_column(sessionmaker, assignment_id: uuid.UUID) -> dict:
    """The column as written, before the load listener reads it."""
    async with sessionmaker() as db:
        return (
            await db.execute(select(Assignment.trigger_config).where(Assignment.id == assignment_id))
        ).scalar_one()


async def _own_integration(sessionmaker, user: User) -> str:
    """An integration ``user`` owns: a save may only point a trigger at one its writer can use."""
    async with sessionmaker() as db:
        integration = Integration(provider="github", name="GitHub", user_id=user.id)
        db.add(integration)
        await db.flush()
        integration_id = str(integration.id)
        await db.commit()
        return integration_id


@pytest.mark.asyncio
async def test_saves_that_leave_the_events_alone_keep_a_legacy_trigger_on_its_prefix_match():
    async with _temp_database("asg_match") as sessionmaker:
        user, assignment_id = await _seed(
            sessionmaker, trigger_type="integration_event", trigger_config=dict(LEGACY)
        )
        other_integration = await _own_integration(sessionmaker, user)
        for payload in (
            {"name": "Renamed"},
            {"prompt": "Review it again."},
            {"enabled": False},
            {"enabled": True},
            {"delay": "5m"},
            # What the trigger card sends: the config rebuilt, a new
            # integration and filter, the type repeated.
            {
                "trigger_type": "integration_event",
                "trigger_config": {**LEGACY, "integration_id": other_integration, "filter": {"from_contains": "bot"}},
            },
            {"trigger_config": {"provider": "github", "event": "pull_request", "events": []}},
        ):
            await _put(sessionmaker, user, assignment_id, payload)
            assert EVENT_MATCHING_KEY not in await _stored_column(sessionmaker, assignment_id), payload


@pytest.mark.asyncio
async def test_choosing_the_events_again_or_asking_for_it_switches_to_exact_and_stays():
    async with _temp_database("asg_match") as sessionmaker:
        user, reselected = await _seed(
            sessionmaker, trigger_type="integration_event", trigger_config=dict(LEGACY)
        )
        other_integration = await _own_integration(sessionmaker, user)
        await _put(sessionmaker, user, reselected, {"trigger_config": {**LEGACY, "event": "issues"}})
        assert await _stored_column(sessionmaker, reselected) == {**LEGACY, "event": "issues", **EXACT}
        # A later save that drops the marker (the trigger card rebuilds the
        # config) keeps the trigger exact.
        await _put(
            sessionmaker,
            user,
            reselected,
            {"trigger_config": {**LEGACY, "event": "issues", "integration_id": other_integration}},
        )
        assert (await _stored_column(sessionmaker, reselected))[EVENT_MATCHING_KEY] == EVENT_MATCHING_EXACT

        user, switched = await _seed(
            sessionmaker, trigger_type="webhook", trigger_config={"path": "github", "events": ["pull_request"]}
        )
        # ``Use exact matching`` on the trigger card: the config as served, plus the marker.
        served = (await _stored(sessionmaker, switched)).trigger_config
        await _put(sessionmaker, user, switched, {"trigger_config": {**served, **EXACT}})
        stored = await _stored_column(sessionmaker, switched)
        assert stored["events"] == ["pull_request"] and stored[EVENT_MATCHING_KEY] == EVENT_MATCHING_EXACT


@pytest.mark.asyncio
async def test_a_legacy_event_type_row_is_read_through_that_key_only_once_switched():
    async with _temp_database("asg_match") as sessionmaker:
        legacy = {"provider": "microsoft", "event_type": "new_email"}
        user, assignment_id = await _seed(sessionmaker, trigger_type="integration_event", trigger_config=dict(legacy))

        row = await _stored(sessionmaker, assignment_id)
        assert row.trigger_config == legacy and not event_read_from_legacy_key(row)

        # An agent-builder edit that keeps the key keeps the row as it was.
        edited = {**legacy, "filter": {"from_contains": "billing"}}
        await _put(sessionmaker, user, assignment_id, {"trigger_config": edited})
        row = await _stored(sessionmaker, assignment_id)
        assert row.trigger_config == edited and not event_read_from_legacy_key(row)

        await _put(sessionmaker, user, assignment_id, {"trigger_config": {**row.trigger_config, **EXACT}})
        row = await _stored(sessionmaker, assignment_id)
        assert row.trigger_config == {"provider": "microsoft", "event": "new_email", "filter": {"from_contains": "billing"}, **EXACT}
        # Read through the key, and flagged so its first poll starts from now;
        # the column keeps the key, which is what the flag is read from.
        assert event_read_from_legacy_key(row)
        assert await _stored_column(sessionmaker, assignment_id) == {**edited, **EXACT}


@pytest.mark.asyncio
async def test_a_duplicate_keeps_its_source_mode():
    async with _temp_database("asg_match") as sessionmaker:
        _user, legacy_id = await _seed(sessionmaker, trigger_type="integration_event", trigger_config=dict(LEGACY))
        _user, exact_id = await _seed(
            sessionmaker, trigger_type="integration_event", trigger_config={**LEGACY, **EXACT}
        )
        copies: dict[uuid.UUID, uuid.UUID] = {}
        async with sessionmaker() as db:
            for source_id in (legacy_id, exact_id):
                source = await db.get(Assignment, source_id)
                # As ``POST /agents/{id}/duplicate`` builds it.
                copy = Assignment(
                    **build_assignment_copy_kwargs(
                        source,
                        agent_id=source.agent_id,
                        user_id=source.user_id,
                        trigger_config=ensure_webhook_secret(
                            source.trigger_type, strip_webhook_secret(deepcopy(source.trigger_config or {}))
                        ),
                    )
                )
                db.add(copy)
                await db.flush()
                copies[source_id] = copy.id
            await db.commit()
        assert await _stored_column(sessionmaker, copies[legacy_id]) == LEGACY
        assert await _stored_column(sessionmaker, copies[exact_id]) == {**LEGACY, **EXACT}


@pytest.mark.asyncio
async def test_a_trigger_created_through_the_api_matches_exactly():
    async with _temp_database("asg_match") as sessionmaker:
        user, _assignment_id = await _seed(sessionmaker)
        async with sessionmaker() as db:
            agent = (await db.execute(select(Agent).where(Agent.user_id == user.id))).scalar_one()
            request = Request({"type": "http", "method": "POST", "path": "/x", "headers": []})
            created = await assignments_api.create_assignment(
                CreateAssignmentRequest(
                    agent_id=str(agent.id),
                    name="Review PRs",
                    trigger_type="integration_event",
                    trigger_config={"provider": "github", "event": "pull_request"},
                    prompt="Review it.",
                ),
                request,
                await db.get(User, user.id),
                db,
            )
        assert created.event_matching == "exact"
        assert await _stored_column(sessionmaker, uuid.UUID(created.id)) == {
            "provider": "github",
            "event": "pull_request",
            **EXACT,
        }
