"""The implicit-exposure listener against a real PostgreSQL.

Same scratch-database recipe as ``test_workset_ingestion_postgres.py``. A real
database rather than a fake session because what is under test IS the
persistence: the ``before_flush`` listener writes through the session, and the
two properties below are decided by things no double can model —

1. **Statement ordering inside one flush.** Revoking an exposure and minting
   its replacement in the same flush puts an UPDATE and an INSERT against
   ``a2a_exposures`` in the same unit of work, under the partial unique index
   ``uq_a2a_exposures_live_agent`` (``agent_id WHERE revoked_at IS NULL``). If
   the INSERT were emitted first there would momentarily be two live rows for
   one agent and the flush would fail. A session double cannot show that.

2. **Insert ordering across tables.** A new agent and its exposure are created
   in the same flush, so the ``agents`` row must be written before the
   ``a2a_exposures`` row that references it. Only a real foreign key says so.

Both are cheap to get wrong and silent in unit tests, which is exactly what
happened: the listener originally skipped the bind half after a revocation and
never looked at ``session.new`` at all.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import (
    DBAPIError,
    IntegrityError,
)
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 — registers every mapper on Base.metadata
from app.a2a.hooks import register_a2a_hooks
from app.models.a2a import REVOKED_SHARING_CHANGE, A2AExposure
from app.models.agent import Agent
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("a2ahooks", tables=ALL_TABLES, extensions=("vector",)) as db:
        # The listener is registered on the global Session class, so it is
        # already live for the sessionmaker below. Idempotent by contract.
        register_a2a_hooks()
        yield db.sessions


async def _seed_user(session) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"a2a-{uuid.uuid4().hex[:8]}@test.local",
        full_name="A2A Hook Test",
        hashed_password="x",
    )
    session.add(user)
    await session.flush()
    return user


async def _seed_inbound_integration(session, owner_id, provider="oauth_a2a") -> uuid.UUID:
    """An A2A integration that actually exposes, and its settings row.

    Before direction existed these tests bound a random UUID, because only the
    binding KEY mattered. Exposure now follows the integration's direction, so
    a binding must name an integration whose settings row says ``inbound`` —
    which is what every real deployment has, courtesy of the migration
    backfill. A random id has no row and is therefore neither direction, which
    is the fail-closed rule working as designed rather than a test bug.
    """
    from app.models.a2a import DIRECTION_INBOUND, A2AIntegrationSettings
    from app.models.integration import Integration

    integration = Integration(
        id=uuid.uuid4(),
        user_id=owner_id,
        provider=provider,
        auth_type="none",
        name="Partner",
    )
    session.add(integration)
    await session.flush()
    session.add(
        A2AIntegrationSettings(
            id=uuid.uuid4(),
            integration_id=integration.id,
            direction=DIRECTION_INBOUND,
        )
    )
    await session.flush()
    return integration.id


def _agent(owner_id, **kw) -> Agent:
    """An agent as the creation API builds one.

    ``enabled`` is deliberately NOT passed. It is a Python-side column default
    applied at INSERT and ``CreateAgentRequest`` has no field for it, so on the
    real creation path a pending agent's ``enabled`` is ``None`` — which is
    falsy. Setting it here once made this suite green against a listener that
    refused every genuine creation, so the omission is the test.
    """
    return Agent(
        name="Hook Test Agent",
        user_id=owner_id,
        is_company_managed=False,
        is_builtin=False,
        **kw,
    )


async def _live_exposures(session, agent_id) -> list[A2AExposure]:
    rows = await session.execute(
        select(A2AExposure).where(
            A2AExposure.agent_id == agent_id,
            A2AExposure.revoked_at.is_(None),
        )
    )
    return list(rows.scalars().all())


async def test_creating_an_agent_with_a_binding_exposes_it():
    """``session.new``, and the cross-table insert order it depends on.

    The exposure carries ``agent_id`` as a real foreign key, so the agent row
    has to be written first — and the primary key is a Python-side default the
    listener has to settle early to have anything to reference.
    """
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        owner = await _seed_user(session)
        agent = _agent(
            owner.id,
            integration_bindings={"oauth_a2a": [str(await _seed_inbound_integration(session, owner.id))]},
        )
        session.add(agent)
        await session.commit()

        live = await _live_exposures(session, agent.id)
        assert len(live) == 1
        assert live[0].run_as_user_id == owner.id
        assert live[0].card_token


async def test_creating_an_agent_without_a_binding_exposes_nothing():
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        owner = await _seed_user(session)
        agent = _agent(owner.id, integration_bindings={})
        session.add(agent)
        await session.commit()

        assert await _live_exposures(session, agent.id) == []


async def test_sharing_change_plus_new_binding_commits_a_fresh_exposure():
    """The ordering property, end to end.

    One flush revokes the old exposure (UPDATE) and mints its replacement
    (INSERT) against a partial unique index that permits one live row per
    agent. If the INSERT landed first this commit would raise
    ``UniqueViolation`` — so a green assertion here is the ordering claim.
    """
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        owner = await _seed_user(session)
        agent = _agent(
            owner.id,
            shared_group_ids=["g1"],
            integration_bindings={"oauth_a2a": [str(await _seed_inbound_integration(session, owner.id))]},
        )
        session.add(agent)
        await session.commit()

        first = (await _live_exposures(session, agent.id))[0]
        original_token = first.card_token

        # One request: sharing moves AND a second partner is bound.
        agent.shared_group_ids = ["g1", "g2"]
        agent.integration_bindings = {
            "oauth_a2a": [str(await _seed_inbound_integration(session, owner.id))],
            "entra_a2a": [
                str(await _seed_inbound_integration(session, owner.id, "entra_a2a"))
            ],
        }
        await session.commit()

        live = await _live_exposures(session, agent.id)
        assert len(live) == 1, "the binding must still leave one live row"
        assert live[0].card_token != original_token, "retired URL stays dead"
        assert live[0].run_as_user_id == owner.id

        await session.refresh(first)
        assert first.revoked_at is not None
        assert first.revoked_reason == REVOKED_SHARING_CHANGE


async def test_unbinding_the_last_integration_leaves_nothing_live():
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        owner = await _seed_user(session)
        agent = _agent(
            owner.id,
            integration_bindings={"oauth_a2a": [str(await _seed_inbound_integration(session, owner.id))]},
        )
        session.add(agent)
        await session.commit()
        assert len(await _live_exposures(session, agent.id)) == 1

        # Unbinding through the API leaves the key with an empty list
        # rather than removing it, which must still read as unbound.
        agent.integration_bindings = {"oauth_a2a": []}
        await session.commit()

        assert await _live_exposures(session, agent.id) == []


async def test_enabling_an_agent_bound_while_disabled_exposes_it():
    """A refusal that is never reconsidered is permanent.

    Binding to a disabled agent correctly produces no exposure — an agent
    nobody can reach has nothing to expose. But re-enabling is not a
    revocation, so nothing looked at the agent again: it went on binding an
    A2A integration, every read surface went on deriving "exposed" from that
    binding, and no exposure row ever appeared.
    """
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        owner = await _seed_user(session)
        agent = _agent(owner.id, integration_bindings={})
        agent.enabled = False
        session.add(agent)
        await session.commit()

        agent.integration_bindings = {"oauth_a2a": [str(await _seed_inbound_integration(session, owner.id))]}
        await session.commit()
        assert await _live_exposures(session, agent.id) == [], (
            "a disabled agent must not be reachable"
        )

        agent.enabled = True
        await session.commit()

        live = await _live_exposures(session, agent.id)
        assert len(live) == 1
        assert live[0].run_as_user_id == owner.id


async def test_giving_an_ownerless_agent_an_owner_exposes_it():
    """The same gap reached through the run-as refusal.

    An agent with nobody to run as fails closed, which is right. The seed pass
    backfills a company-managed row's owner exactly this way, so the recovery
    has to be automatic — otherwise the agent binds A2A for good with nothing
    live behind it.
    """
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        owner = await _seed_user(session)
        agent = Agent(
            name="Ownerless",
            user_id=None,
            enabled=True,
            is_company_managed=True,
            is_builtin=False,
            integration_bindings={},
        )
        session.add(agent)
        await session.commit()

        agent.integration_bindings = {"entra_a2a": [str(await _seed_inbound_integration(session, owner.id, "entra_a2a"))]}
        await session.commit()
        assert await _live_exposures(session, agent.id) == []

        agent.user_id = owner.id
        await session.commit()

        live = await _live_exposures(session, agent.id)
        assert len(live) == 1
        assert live[0].run_as_user_id == owner.id


async def test_a_sharing_change_alone_does_not_re_expose():
    """The other side of the rule, and why it is not simply "resync on any
    watched change".

    A sharing change is a REVOCATION: the authority the exposure stood on may
    have moved. Re-minting on the spot would turn the teardown FR-041 requires
    into a card-URL rotation nobody asked for. Only an explicit re-binding in
    the same request re-exposes.
    """
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        owner = await _seed_user(session)
        agent = _agent(
            owner.id,
            shared_group_ids=["g1"],
            integration_bindings={"oauth_a2a": [str(await _seed_inbound_integration(session, owner.id))]},
        )
        session.add(agent)
        await session.commit()
        assert len(await _live_exposures(session, agent.id)) == 1

        agent.shared_group_ids = ["g1", "g2"]
        await session.commit()

        assert await _live_exposures(session, agent.id) == []


async def test_a_personally_owned_agent_gaining_an_owner_is_exposed():
    """The same recovery on the tier that does NOT take the provenance skip.

    ``_revocation_reason`` returns ``owner_change`` for a personally-owned
    agent whose ``user_id`` moves — the company-managed skip does not apply —
    so the resync must not be narrowed by a reason that tore nothing down.
    There is no live exposure to revoke here, so nothing is; suppressing the
    resync anyway leaves the agent binding A2A for good with nothing live.

    The company-managed sibling passes either way, because its reason is
    ``None``. This is the case that distinguishes them.
    """
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        owner = await _seed_user(session)
        agent = Agent(
            name="Ownerless personal",
            user_id=None,
            enabled=True,
            is_company_managed=False,
            is_builtin=False,
            integration_bindings={},
        )
        session.add(agent)
        await session.commit()

        agent.integration_bindings = {"oauth_a2a": [str(await _seed_inbound_integration(session, owner.id))]}
        await session.commit()
        assert await _live_exposures(session, agent.id) == []

        agent.user_id = owner.id
        await session.commit()

        live = await _live_exposures(session, agent.id)
        assert len(live) == 1
        assert live[0].run_as_user_id == owner.id


async def test_a_pending_exposure_does_not_survive_a_failed_flush():
    """``session.info`` is not transaction-scoped, and ``rollback()`` does not
    clear it.

    A flush that raises after ``before_flush`` ran leaves the queue populated —
    ``_after_flush`` never fired to consume it. Without the rollback listener
    the next successful flush on that session inserts an exposure naming an
    ``agent_id`` that was rolled back, failing on the foreign key in a request
    with nothing to do with A2A.
    """
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        owner = await _seed_user(session)
        owner_id = owner.id

        first = _agent(
            owner_id,
            slug="collides",
            integration_bindings={"oauth_a2a": [str(await _seed_inbound_integration(session, owner.id))]},
        )
        session.add(first)
        await session.commit()
        first_id = first.id

        # A second agent whose INSERT fails on the unique slug, queued for
        # exposure in the same flush.
        clash = _agent(
            owner_id,
            slug="collides",
            integration_bindings={"oauth_a2a": [str(await _seed_inbound_integration(session, owner.id))]},
        )
        session.add(clash)
        # Read before the failure: the rollback expires every instance, and
        # touching one afterwards would attempt IO of its own.
        clash_id = clash.id
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

        # An unrelated write on the same session must not drag the dead
        # agent's exposure along with it.
        bystander = _agent(owner_id, integration_bindings={})
        session.add(bystander)
        await session.commit()
        bystander_id = bystander.id

        assert await _live_exposures(session, clash_id) == []
        assert await _live_exposures(session, bystander_id) == []
        assert len(await _live_exposures(session, first_id)) == 1


def _exposure_count(action: str, reason: str) -> float:
    from prometheus_client import REGISTRY

    return (
        REGISTRY.get_sample_value(
            "a2a_exposure_changes_total", {"action": action, "reason": reason}
        )
        or 0.0
    )


async def test_an_exposure_is_counted_once_it_commits():
    """The flush decides the exposure, the commit makes it true, and
    the counter follows the commit - a counter bumped at flush time counts
    changes a rollback then discards, and cannot be corrected afterwards."""
    from app.models.a2a import EXPOSED_BOUND

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        owner = await _seed_user(session)
        integration_id = await _seed_inbound_integration(session, owner.id)
        await session.commit()
        before = _exposure_count("expose", EXPOSED_BOUND)

        agent = _agent(owner.id, integration_bindings={"oauth_a2a": [str(integration_id)]})
        session.add(agent)
        await session.flush()
        assert _exposure_count("expose", EXPOSED_BOUND) == before, (
            "counted before the transaction committed"
        )
        await session.commit()

        assert len(await _live_exposures(session, agent.id)) == 1
        assert _exposure_count("expose", EXPOSED_BOUND) == before + 1


async def test_an_exposure_rolled_back_after_its_flush_is_not_counted():
    """The edit path decides in ``before_flush``; a rollback after a
    successful flush must leave the counter where it was, including at the
    next unrelated commit on the same session."""
    from app.models.a2a import EXPOSED_BOUND

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        owner = await _seed_user(session)
        owner_id = owner.id
        integration_id = await _seed_inbound_integration(session, owner_id)
        agent = _agent(owner_id, integration_bindings={})
        session.add(agent)
        await session.commit()
        agent_id = agent.id
        before = _exposure_count("expose", EXPOSED_BOUND)

        agent.integration_bindings = {"oauth_a2a": [str(integration_id)]}
        await session.flush()
        await session.rollback()

        session.add(_agent(owner_id, integration_bindings={}))
        await session.commit()

        assert await _live_exposures(session, agent_id) == []
        assert _exposure_count("expose", EXPOSED_BOUND) == before


async def test_a_revocation_lost_to_a_failed_flush_is_not_counted():
    """The revocation is decided in ``before_flush`` and the flush then fails
    on an unrelated row of the same unit of work: nothing was revoked, so
    nothing may be counted."""
    from app.models.a2a import REVOKED_AGENT_DISABLED

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        owner = await _seed_user(session)
        agent = _agent(
            owner.id,
            slug="taken",
            integration_bindings={"oauth_a2a": [str(await _seed_inbound_integration(session, owner.id))]},
        )
        session.add(agent)
        await session.commit()
        agent_id = agent.id
        before = _exposure_count("revoke", REVOKED_AGENT_DISABLED)

        agent.enabled = False
        session.add(_agent(owner.id, slug="taken", integration_bindings={}))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

        assert len(await _live_exposures(session, agent_id)) == 1
        assert _exposure_count("revoke", REVOKED_AGENT_DISABLED) == before


async def test_a_savepoint_rollback_keeps_the_exposure_decided_before_it():
    """The nested case end to end: an exposure flushed in the outer
    transaction still counts at its commit when a later SAVEPOINT that exposed
    another agent is rolled back - and only once, whatever commits follow."""
    from app.models.a2a import EXPOSED_BOUND

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        owner = await _seed_user(session)
        owner_id = owner.id
        integration_id = await _seed_inbound_integration(session, owner_id)
        kept = _agent(owner_id, integration_bindings={})
        dropped = _agent(owner_id, integration_bindings={})
        session.add_all([kept, dropped])
        await session.commit()
        kept_id, dropped_id = kept.id, dropped.id
        before = _exposure_count("expose", EXPOSED_BOUND)

        kept.integration_bindings = {"oauth_a2a": [str(integration_id)]}
        await session.flush()
        nested = await session.begin_nested()
        dropped.integration_bindings = {"oauth_a2a": [str(integration_id)]}
        await session.flush()
        await nested.rollback()
        await session.commit()
        assert _exposure_count("expose", EXPOSED_BOUND) == before + 1

        session.add(_agent(owner_id, integration_bindings={}))
        await session.commit()
        assert _exposure_count("expose", EXPOSED_BOUND) == before + 1, (
            "a later commit counted it again"
        )
        assert len(await _live_exposures(session, kept_id)) == 1
        assert await _live_exposures(session, dropped_id) == []


async def test_a_failed_flush_inside_a_savepoint_block_is_not_counted():
    """The ``async with session.begin_nested():`` idiom that absorbs an
    IntegrityError. The exposure decided inside the block went with the
    SAVEPOINT; counting it at the outer commit would report an exposure that
    does not exist next to the one decided before the block, which does."""
    from app.models.a2a import EXPOSED_BOUND

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        owner = await _seed_user(session)
        owner_id = owner.id
        integration_id = await _seed_inbound_integration(session, owner_id)
        kept = _agent(owner_id, integration_bindings={})
        lost = _agent(owner_id, integration_bindings={})
        session.add_all([kept, lost, _agent(owner_id, slug="taken", integration_bindings={})])
        await session.commit()
        kept_id, lost_id = kept.id, lost.id
        before = _exposure_count("expose", EXPOSED_BOUND)

        kept.integration_bindings = {"oauth_a2a": [str(integration_id)]}
        await session.flush()
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                lost.integration_bindings = {"oauth_a2a": [str(integration_id)]}
                session.add(_agent(owner_id, slug="taken", integration_bindings={}))
                await session.flush()
        await session.commit()

        assert len(await _live_exposures(session, kept_id)) == 1
        assert await _live_exposures(session, lost_id) == []
        assert _exposure_count("expose", EXPOSED_BOUND) == before + 1, (
            "the exposure lost with the SAVEPOINT was counted"
        )


async def test_an_exposure_whose_commit_fails_is_not_counted():
    """The flush succeeds and the COMMIT is refused - by a deferred constraint
    trigger here, by a serialization failure or a dropped connection in the
    field. ``after_commit`` never fires, and the rollback drops the queue
    before the next commit on the same session could count it."""
    from app.models.a2a import EXPOSED_BOUND

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        await session.execute(
            text(
                "CREATE FUNCTION pr3117_refuse_commit() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'refused at commit'; END $$"
            )
        )
        await session.execute(
            text(
                "CREATE CONSTRAINT TRIGGER pr3117_refuse_commit "
                "AFTER INSERT ON a2a_exposures DEFERRABLE INITIALLY DEFERRED "
                "FOR EACH ROW EXECUTE FUNCTION pr3117_refuse_commit()"
            )
        )
        owner = await _seed_user(session)
        owner_id = owner.id
        integration_id = await _seed_inbound_integration(session, owner_id)
        await session.commit()
        before = _exposure_count("expose", EXPOSED_BOUND)

        agent = _agent(owner_id, integration_bindings={"oauth_a2a": [str(integration_id)]})
        session.add(agent)
        await session.flush()
        agent_id = agent.id
        with pytest.raises(DBAPIError, match="refused at commit"):
            await session.commit()
        await session.rollback()
        assert _exposure_count("expose", EXPOSED_BOUND) == before

        session.add(_agent(owner_id, integration_bindings={}))
        await session.commit()
        assert await _live_exposures(session, agent_id) == []
        assert _exposure_count("expose", EXPOSED_BOUND) == before


async def test_an_exposure_left_uncommitted_by_close_is_not_counted():
    """``close()`` ends the transaction without any rollback event, and the
    session is reusable afterwards: its next commit must not count what the
    close discarded."""
    from app.models.a2a import EXPOSED_BOUND

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        owner = await _seed_user(session)
        owner_id = owner.id
        integration_id = await _seed_inbound_integration(session, owner_id)
        agent = _agent(owner_id, integration_bindings={})
        session.add(agent)
        await session.commit()
        agent_id = agent.id
        before = _exposure_count("expose", EXPOSED_BOUND)

        agent.integration_bindings = {"oauth_a2a": [str(integration_id)]}
        await session.flush()
        await session.close()

        session.add(_agent(owner_id, integration_bindings={}))
        await session.commit()
        assert await _live_exposures(session, agent_id) == []
        assert _exposure_count("expose", EXPOSED_BOUND) == before
