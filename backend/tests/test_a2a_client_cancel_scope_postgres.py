"""What a per-registration cancel may reach, against a real database.

``build_cancel_tool`` makes ONE tool per registration, named after that
partner, described as stopping work "still running at '<partner>'". Since the
task id became optional, calling it bare says "stop everything this
conversation has left running there" — and "there" is the claim under test.

``cancel_outstanding_for`` scopes on the run or the conversation and narrows
on the task id. It never filtered on the registration: the join to
``Integration`` is only there to fetch each row's own partner to cancel
against. With a required id that was almost academic — one id, one row. Bare,
it is not: two registrations bound to one agent is a case this feature
supports on purpose (FR-042 exists because of it), so the Acme tool would end
the Globex work too and report a count including it.

Which is the one thing the tool's own description is careful about — "the work
is real and already being paid for, so abandoning it is their call and never
yours". A user saying "stop the Acme task" has not made that call about
Globex.

Which rows a WHERE clause reaches is decided by running it, so it is run here.
Same scratch-database recipe as ``test_a2a_hooks_postgres.py``. No target rows
are seeded, so every cancel concludes locally and nothing goes near a network.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 — registers every mapper on Base.metadata
from app.a2a.client.delegation import cancel_outstanding_for
from app.models.a2a import OUTCOME_CANCELLED, A2ADelegation
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("a2acancel", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _seed(session):
    """One user, one agent, and the two registrations bound to it."""
    from app.models.agent import Agent
    from app.models.integration import Integration
    from app.models.user import User

    user = User(
        id=uuid.uuid4(),
        email=f"a2a-{uuid.uuid4().hex[:8]}@test.local",
        full_name="A2A Cancel Scope Test",
        hashed_password="x",
    )
    session.add(user)
    await session.flush()
    agent = Agent(id=uuid.uuid4(), name="Caller", user_id=user.id)
    acme = Integration(
        id=uuid.uuid4(),
        provider="oauth_a2a",
        name=f"acme-{uuid.uuid4().hex[:6]}",
        user_id=user.id,
    )
    globex = Integration(
        id=uuid.uuid4(),
        provider="oauth_a2a",
        name=f"globex-{uuid.uuid4().hex[:6]}",
        user_id=user.id,
    )
    session.add_all([agent, acme, globex])
    await session.flush()
    return user, agent, acme, globex


async def _conversation(session, user):
    """``conversation_ref`` carries a foreign key, so the row has to exist."""
    from app.models.chat import Conversation

    conversation = Conversation(
        id=uuid.uuid4(), user_id=user.id, title="Cancel scope"
    )
    session.add(conversation)
    await session.flush()
    return conversation.id


async def _outstanding(session, *, user, agent, integration, conversation):
    row = A2ADelegation(
        id=uuid.uuid4(),
        integration_id=integration.id,
        agent_id=agent.id,
        user_id=user.id,
        conversation_ref=conversation,
        partner_task_id=f"task-{uuid.uuid4().hex[:8]}",
        protocol_version="0.3",
        signature_state="unsigned",
        submitted_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    return row


class TestAPerPartnerCancelStopsOnlyThatPartner:
    async def test_a_bare_cancel_leaves_the_other_registration_alone(self):
        async with _temp_database() as sessionmaker, sessionmaker() as session:
            user, agent, acme, globex = await _seed(session)
            conversation = await _conversation(session, user)
            mine = await _outstanding(
                session,
                user=user,
                agent=agent,
                integration=acme,
                conversation=conversation,
            )
            theirs = await _outstanding(
                session,
                user=user,
                agent=agent,
                integration=globex,
                conversation=conversation,
            )
            await session.commit()

            stopped = await cancel_outstanding_for(
                session,
                conversation_ref=conversation,
                integration_id=acme.id,
            )
            await session.commit()

            assert stopped.total == 1, (
                "the count reported to the user included work at a partner "
                "they did not name"
            )
            assert (await _read(session, mine.id)).outcome == OUTCOME_CANCELLED
            assert (await _read(session, theirs.id)).outcome is None, (
                "a user saying 'stop the Acme task' did not say that about "
                "Globex, and that work is real"
            )

    async def test_the_id_path_is_narrowed_too(self):
        """Two partners may issue the same task id string — it is theirs to
        choose — and a row carrying it under another registration was never
        this tool's to cancel."""
        async with _temp_database() as sessionmaker, sessionmaker() as session:
            user, agent, acme, globex = await _seed(session)
            conversation = await _conversation(session, user)
            mine = await _outstanding(
                session,
                user=user,
                agent=agent,
                integration=acme,
                conversation=conversation,
            )
            theirs = await _outstanding(
                session,
                user=user,
                agent=agent,
                integration=globex,
                conversation=conversation,
            )
            theirs.partner_task_id = mine.partner_task_id
            await session.commit()

            stopped = await cancel_outstanding_for(
                session,
                conversation_ref=conversation,
                partner_task_id=mine.partner_task_id,
                integration_id=acme.id,
            )
            await session.commit()

            assert stopped.total == 1
            assert (await _read(session, theirs.id)).outcome is None


class TestTheBoundariesThatEndEverythingStillDo:
    """The filter is the TOOL's, not the function's. A run reaching its end
    and a conversation being abandoned must reach every partner the work
    touched — there is nobody left to collect any of it — so the scope stays
    optional and those callers pass none.
    """

    async def test_abandoning_the_conversation_reaches_both(self):
        async with _temp_database() as sessionmaker, sessionmaker() as session:
            user, agent, acme, globex = await _seed(session)
            conversation = await _conversation(session, user)
            rows = [
                await _outstanding(
                    session,
                    user=user,
                    agent=agent,
                    integration=integration,
                    conversation=conversation,
                )
                for integration in (acme, globex)
            ]
            await session.commit()

            stopped = await cancel_outstanding_for(
                session, conversation_ref=conversation
            )
            await session.commit()

            assert stopped.total == 2
            for row in rows:
                assert (await _read(session, row.id)).outcome == OUTCOME_CANCELLED


async def _read(session, row_id):
    """Re-read from the database, not from the identity map.

    ``expire_on_commit=False`` keeps the loaded objects, which is the point of
    the sessionmaker above — so a plain select would hand back the stale
    instance and the assertions would pass on what the test itself wrote.
    """
    return (
        await session.execute(
            select(A2ADelegation)
            .where(A2ADelegation.id == row_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


class TestOutstandingPartnerWorkIsListable:
    """The model looked in ``list_background_tasks``, found nothing, and
    concluded the partner had finished. Partner work was never in that list —
    a delegation is not a ``SubAgentTask`` — so the answer was empty and wrong,
    and the agent stated it with confidence.

    Correcting the model with a description asks it to distrust a tool that
    answers. Putting the work in the list makes the instinct right.

    Scoped like the cancel it precedes: this conversation or this run, never
    the user's whole history.
    """

    async def test_it_lists_what_this_conversation_left_running(self):
        from app.a2a.client.delegation import outstanding_for_listing

        async with _temp_database() as sessionmaker, sessionmaker() as session:
            user, agent, acme, globex = await _seed(session)
            conversation = await _conversation(session, user)
            mine = await _outstanding(
                session,
                user=user,
                agent=agent,
                integration=acme,
                conversation=conversation,
            )
            await _outstanding(
                session,
                user=user,
                agent=agent,
                integration=globex,
                conversation=conversation,
            )
            await session.commit()

            rows = await outstanding_for_listing(
                session, conversation_ref=conversation
            )
            assert len(rows) == 2, "both partners' work is this conversation's"
            entry = next(r for r in rows if r["delegation_id"] == str(mine.id))
            assert entry["partner"] == acme.name
            assert entry["status"] == "running"

    async def test_a_concluded_delegation_is_not_listed(self):
        from app.a2a.client.delegation import outstanding_for_listing
        from app.models.a2a import OUTCOME_COMPLETED

        async with _temp_database() as sessionmaker, sessionmaker() as session:
            user, agent, acme, _globex = await _seed(session)
            conversation = await _conversation(session, user)
            done = await _outstanding(
                session,
                user=user,
                agent=agent,
                integration=acme,
                conversation=conversation,
            )
            done.outcome = OUTCOME_COMPLETED
            done.terminal_at = datetime.now(UTC)
            await session.commit()

            assert await outstanding_for_listing(
                session, conversation_ref=conversation
            ) == []

    async def test_another_conversation_s_work_is_not_listed(self):
        from app.a2a.client.delegation import outstanding_for_listing

        async with _temp_database() as sessionmaker, sessionmaker() as session:
            user, agent, acme, _globex = await _seed(session)
            mine = await _conversation(session, user)
            theirs = await _conversation(session, user)
            await _outstanding(
                session,
                user=user,
                agent=agent,
                integration=acme,
                conversation=theirs,
            )
            await session.commit()

            assert await outstanding_for_listing(
                session, conversation_ref=mine
            ) == []

    async def test_no_scope_lists_nothing(self):
        """The same rule the cancel keeps: without a run or a conversation
        there is no scope, and a user's whole history is not one."""
        from app.a2a.client.delegation import outstanding_for_listing

        async with _temp_database() as sessionmaker, sessionmaker() as session:
            assert await outstanding_for_listing(session) == []
