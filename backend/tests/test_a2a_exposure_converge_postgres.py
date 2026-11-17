"""An agent bound before its A2A integration had a direction gets exposed (#11786).

The flush listener (``app.a2a.hooks``) exposes an agent when its bindings
move. A binding made while the integration had no settings row was neither
direction, so it minted nothing, and the row arriving later (a migration
backfill, ``provision_settings``, ``change_direction``) changes no agent. These
tests pin the converge step that closes that order of events, against a real
PostgreSQL because what is decided is the persisted exposure row.
"""

from __future__ import annotations

import uuid

import pytest

from app.a2a.hooks import converge_exposures
from app.a2a.providers import change_direction, create_settings, provision_settings
from app.models.a2a import DIRECTION_INBOUND, DIRECTION_OUTBOUND, A2AIntegrationSettings
from app.models.integration import Integration
from tests.test_a2a_hooks_postgres import _agent, _live_exposures, _seed_user, _temp_database

pytestmark = pytest.mark.integration


async def _integration_without_row(session, owner_id) -> Integration:
    integration = Integration(
        id=uuid.uuid4(),
        user_id=owner_id,
        provider="oauth_a2a",
        auth_type="none",
        name="Partner",
    )
    session.add(integration)
    await session.flush()
    return integration


async def _bound_agent(session, owner_id, integration_id, **kw):
    agent = _agent(owner_id, integration_bindings={"oauth_a2a": [str(integration_id)]}, **kw)
    session.add(agent)
    await session.commit()
    return agent


async def test_binding_without_a_row_exposes_nothing_until_converged():
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        owner = await _seed_user(session)
        integration = await _integration_without_row(session, owner.id)
        agent = await _bound_agent(session, owner.id, integration.id)
        # Neither direction at bind time: the fail-closed rule.
        assert await _live_exposures(session, agent.id) == []

        # The backfill writes the row behind the ORM's back, as the migration does.
        session.add(A2AIntegrationSettings(id=uuid.uuid4(), integration_id=integration.id, direction=DIRECTION_INBOUND))
        await session.commit()
        assert await _live_exposures(session, agent.id) == []

        assert await converge_exposures(session) == 1
        await session.commit()
        live = await _live_exposures(session, agent.id)
        assert len(live) == 1
        assert live[0].run_as_user_id == owner.id
        assert live[0].card_token

        # Idempotent: a second pass finds nothing to do.
        assert await converge_exposures(session) == 0


async def test_change_direction_creating_the_row_converges():
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        owner = await _seed_user(session)
        integration = await _integration_without_row(session, owner.id)
        agent = await _bound_agent(session, owner.id, integration.id)

        await change_direction(session, integration, DIRECTION_INBOUND)
        await session.commit()

        assert len(await _live_exposures(session, agent.id)) == 1


async def test_provision_settings_inbound_converges_and_outbound_does_not():
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        owner = await _seed_user(session)
        inbound = await _integration_without_row(session, owner.id)
        outbound = await _integration_without_row(session, owner.id)
        exposed = await _bound_agent(session, owner.id, inbound.id)
        delegating = await _bound_agent(session, owner.id, outbound.id)

        await provision_settings(session, inbound, DIRECTION_INBOUND)
        await provision_settings(session, outbound, DIRECTION_OUTBOUND)
        await session.commit()

        assert len(await _live_exposures(session, exposed.id)) == 1
        # Direction rules unchanged: an outbound binding never exposes.
        assert await _live_exposures(session, delegating.id) == []


async def test_converge_keeps_the_listener_refusals():
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        owner = await _seed_user(session)
        integration = await _integration_without_row(session, owner.id)
        disabled = await _bound_agent(session, owner.id, integration.id, enabled=False)
        await create_settings(session, integration.id, DIRECTION_INBOUND)
        await session.commit()

        assert await converge_exposures(session) == 0
        assert await _live_exposures(session, disabled.id) == []


async def test_converge_scoped_to_one_integration_leaves_the_others():
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        owner = await _seed_user(session)
        first = await _integration_without_row(session, owner.id)
        second = await _integration_without_row(session, owner.id)
        a = await _bound_agent(session, owner.id, first.id)
        b = await _bound_agent(session, owner.id, second.id)
        for integration in (first, second):
            session.add(
                A2AIntegrationSettings(id=uuid.uuid4(), integration_id=integration.id, direction=DIRECTION_INBOUND)
            )
        await session.commit()

        assert await converge_exposures(session, first.id) == 1
        await session.commit()
        assert len(await _live_exposures(session, a.id)) == 1
        assert await _live_exposures(session, b.id) == []
