"""Changing an integration's direction: refused while bound, and clearing.

FR-057 / FR-058. Refusing while bound is what lets this feature skip an
``Integration`` flush listener entirely: if direction cannot change under a
live binding, no exposure and no delegation tool is ever live across a change,
so there is nothing for a listener to reconcile. That saving is only real
while the refusal holds, which is what these tests pin.

Runs against a real database rather than a double, because the refusal depends
on ``agents_binding``'s JSON-containment query — the exact thing a fake would
have to reimplement, and therefore the exact thing a fake could get wrong.
"""

from __future__ import annotations

import uuid

import pytest

from app.a2a.providers import (
    DirectionChangeRefusedError,
    change_direction,
    create_settings,
    get_settings,
    outbound_targets,
    sync_targets,
)
from app.models.a2a import DIRECTION_INBOUND, DIRECTION_OUTBOUND
from app.models.agent import Agent
from app.models.integration import Integration
from app.models.user import User
from tests.test_a2a_hooks_postgres import _temp_database


async def _seed(
    session, *, direction=DIRECTION_INBOUND, bind_to_agent=False, card_urls=()
):
    """A user, an A2A integration, and optionally an agent binding it.

    ``card_urls`` seeds the partner agents an OUTBOUND registration can task,
    through ``sync_targets`` — the same door the save endpoint uses, so a test
    can never seed a shape the product cannot produce.
    """
    user = User(
        id=uuid.uuid4(),
        email=f"dir-{uuid.uuid4().hex[:8]}@test.local",
        full_name="Direction Test",
        hashed_password="x",
    )
    session.add(user)
    await session.flush()

    integration = Integration(
        id=uuid.uuid4(),
        user_id=user.id,
        provider="oauth_a2a",
        auth_type="none",
        name="Partner",
    )
    session.add(integration)
    await session.flush()
    if direction is not None:
        await create_settings(session, integration.id, direction)
    if card_urls:
        await sync_targets(session, integration.id, list(card_urls))

    agent = None
    if bind_to_agent:
        agent = Agent(
            name="Bound Agent",
            user_id=user.id,
            enabled=True,
            integration_bindings={"oauth_a2a": [str(integration.id)]},
        )
        session.add(agent)
        await session.flush()
    return user, integration, agent


class TestDirectionValidation:
    @pytest.mark.asyncio
    async def test_rejects_both_and_anything_else(self):
        # "both" is the option the clarification explicitly declined: the
        # directions need different credentials and confer opposite
        # capabilities, so one row serving both is two blast radii.
        for bad in ("both", "sideways", "", "INBOUND"):
            with pytest.raises(ValueError, match="inbound or outbound"):
                await create_settings(object(), uuid.uuid4(), bad)


@pytest.mark.integration
class TestDirectionChange:
    @pytest.mark.asyncio
    async def test_unbound_switches_and_clears_credentials(self):
        async with _temp_database() as sm, sm() as session:
            _, integration, _ = await _seed(session)
            integration.client_secret_enc = "encrypted-partner-secret"
            integration.api_key_enc = "encrypted-key"
            await session.flush()

            await change_direction(session, integration, DIRECTION_OUTBOUND)

            settings = await get_settings(session, integration.id)
            assert settings.direction == DIRECTION_OUTBOUND
            # FR-058: a credential kept for a direction no longer served is a
            # stored secret with no purpose and no expiry.
            assert integration.client_secret_enc is None
            assert integration.api_key_enc is None

    @pytest.mark.asyncio
    async def test_switching_away_takes_the_partner_agents_with_it(self):
        # FR-058's other half. Every target belonged to the direction being
        # left: an outbound one describes an agent this registration will no
        # longer task. DELETED rather than blanked — a target IS a card, and a
        # row with nothing verified about it is not a cleared registration,
        # it is an agent nobody asked to reach.
        async with _temp_database() as sm, sm() as session:
            _, integration, _ = await _seed(
                session,
                direction=DIRECTION_OUTBOUND,
                card_urls=[
                    "https://partner.example/one/agent-card.json",
                    "https://partner.example/two/agent-card.json",
                ],
            )
            assert len(await outbound_targets(session, integration.id)) == 2

            await change_direction(session, integration, DIRECTION_INBOUND)

            assert await outbound_targets(session, integration.id) == []

    @pytest.mark.asyncio
    async def test_the_partner_s_own_credential_is_retired(self):
        """FR-058 for the credential that actually authenticates.

        Clearing the integration's columns retires what this platform
        PRESENTS; the ``OAuthClient`` row is what a partner presents back, and
        ``_client_covers_exposure`` admits it on binding alone with no
        direction check. Left behind, a registration flipped outbound and
        rebound still accepts the old partner's tasks.
        """
        from sqlalchemy import select

        from app.models.oauth_client import OAuthClient

        async with _temp_database() as sm, sm() as session:
            _, integration, _ = await _seed(session)
            session.add(
                OAuthClient(
                    client_id="partner-holds-this",
                    client_secret_hash="bcrypt",
                    client_name="partner",
                    a2a_integration_id=integration.id,
                )
            )
            await session.flush()

            await change_direction(session, integration, DIRECTION_OUTBOUND)

            rows = (
                await session.execute(
                    select(OAuthClient).where(
                        OAuthClient.a2a_integration_id == integration.id
                    )
                )
            ).scalars().all()
            assert rows == []

    @pytest.mark.asyncio
    async def test_unchanged_value_is_not_a_change(self):
        # Re-saving a form must not clear credentials, the same rule an
        # agent re-save follows.
        async with _temp_database() as sm, sm() as session:
            _, integration, _ = await _seed(session)
            integration.client_secret_enc = "keep-me"
            await session.flush()

            await change_direction(session, integration, DIRECTION_INBOUND)

            assert integration.client_secret_enc == "keep-me"

    @pytest.mark.asyncio
    async def test_missing_row_is_created_rather_than_refused(self):
        # Reachable only for a row predating the backfill. Creating it beats
        # leaving the integration inert in both directions.
        async with _temp_database() as sm, sm() as session:
            _, integration, _ = await _seed(session, direction=None)
            assert await get_settings(session, integration.id) is None

            await change_direction(session, integration, DIRECTION_OUTBOUND)

            settings = await get_settings(session, integration.id)
            assert settings is not None
            assert settings.direction == DIRECTION_OUTBOUND

    @pytest.mark.asyncio
    async def test_refused_while_bound_and_names_the_agents(self):
        async with _temp_database() as sm, sm() as session:
            _, integration, agent = await _seed(session, bind_to_agent=True)
            integration.client_secret_enc = "must-survive-a-refusal"
            await session.flush()

            with pytest.raises(DirectionChangeRefusedError) as exc:
                await change_direction(session, integration, DIRECTION_OUTBOUND)

            # Naming the agents is the difference between an actionable
            # refusal and an operator hunting for what to unbind.
            assert agent.name in exc.value.agent_names
            assert agent.name in str(exc.value)

            # A refusal leaves everything untouched. A half-applied refusal —
            # credentials cleared, direction not changed — would be worse than
            # either outcome.
            settings = await get_settings(session, integration.id)
            assert settings.direction == DIRECTION_INBOUND
            assert integration.client_secret_enc == "must-survive-a-refusal"

    @pytest.mark.asyncio
    async def test_switch_succeeds_after_unbinding(self):
        async with _temp_database() as sm, sm() as session:
            _, integration, agent = await _seed(session, bind_to_agent=True)
            agent.integration_bindings = {}
            await session.flush()

            await change_direction(session, integration, DIRECTION_OUTBOUND)

            settings = await get_settings(session, integration.id)
            assert settings.direction == DIRECTION_OUTBOUND
