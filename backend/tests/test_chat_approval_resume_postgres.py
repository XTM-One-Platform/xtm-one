"""The chat approval resume, end to end against real PostgreSQL and Redis.

Every other test of this path asserts that the *source* contains something. That
caught a missing surface once, and it missed two defects that mattered more: the
request model rejected the very shape the contract documents, and the endpoint
handed the services the raw request body so a resume's text reached the model and
was never stored. Both were structurally present and behaviourally broken, so
every source-inspection test passed while the feature did not work.

So this file runs the path. What it proves, and nothing less:

* a proposal set written by a turn that ended is found again by a *later*
  request, from the database alone (FR-002c3 — the process that paused may be
  gone);
* the approved call executes **with the arguments the reviewer was shown**, its
  result is appended, and a rejected call becomes a structured refusal rather
  than an exception (FR-002c2, FR-010);
* the set is unanswerable afterwards, so a duplicate submission cannot run the
  approved call a second time (FR-002c4, FR-020);
* retirement clears the Redis copy *and* leaves the proposals on the message, so
  the expired state has something to render (FR-002c4).

What it deliberately does not cover: the FastAPI request/response layer. That is
where the schema defect lived, and it is covered by constructing
``SendMessageRequest`` directly in ``test_chat_approval_resume.py`` — a unit test,
because a 422 needs no database to observe.

Skips rather than fails when PostgreSQL or Redis is unreachable, matching the
other ``integration``-marked tests here.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, contextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.core.database import Base
from app.models.chat import Conversation, Message
from app.models.user import User
from app.services import chat_approval_resume as resume
from app.tools.base import ToolResult

pytestmark = pytest.mark.integration

TOOL = "create_agent"
ARGS = {"name": "phishing-triage", "prompt": "Triage reported phishing."}


def _base_url() -> str:
    # CRLF-safe: a .env dragged through Windows leaves \r on the value, which
    # asyncpg faithfully turns into a database named "copilot\r".
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://copilot:copilot@localhost:5432/copilot",
    ).strip()


@asynccontextmanager
async def _temp_database(prefix: str) -> AsyncIterator[async_sessionmaker]:
    """Isolated database with the full schema, dropped on exit.

    The whole schema rather than a hand-picked list: ``conversations`` carries
    foreign keys to ``agents``, which carries more of its own, so any narrow
    selection either fails to create or quietly drops the constraints this test
    relies on being real.
    """
    parsed = urllib.parse.urlparse(_base_url())
    admin_url = parsed._replace(path="/postgres").geturl()
    db_name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    test_url = parsed._replace(path=f"/{db_name}").geturl()

    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    except (DBAPIError, OperationalError, ProgrammingError) as exc:
        orig = getattr(exc, "orig", None)
        pgcode = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
        if pgcode == "42501":
            pytest.skip("DATABASE_URL user requires CREATEDB privilege")
        raise
    except Exception as exc:
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    finally:
        await admin_engine.dispose()

    engine = create_async_engine(test_url)
    try:
        async with engine.begin() as conn:
            # Some tables in the full schema carry ``vector`` columns, so the
            # extension has to exist before ``create_all``. Skipped rather than
            # failed when it is unavailable: this test is about chat metadata,
            # and an environment without pgvector should not report a defect
            # in the resume path.
            try:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            except Exception as exc:
                pytest.skip(f"pgvector unavailable: {exc}")
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        cleanup = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with cleanup.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        finally:
            await cleanup.dispose()


@contextmanager
def _rebound_redis():
    """Rebind the shared Redis client for the duration, then put it back.

    ``app.core.redis`` keeps a module-level singleton, so the first test to touch
    it binds the client to that test's event loop and every later test gets
    "Event loop is closed". The first version of this file swallowed that in the
    same ``except`` that skips when Redis is genuinely unreachable, turning a
    fixture problem into a green run with a plausible-looking skip.

    The restore matters as much as the rebuild. Rebuilding alone fixed this file
    and handed the same "Event loop is closed" to every Redis test scheduled
    after it — order-dependent, so it would have surfaced as an unrelated flake,
    in a suite that already carries one Redis flake and could not afford a second
    that looks identical.

    The client built here is left to garbage collection rather than disconnected:
    closing it needs the loop that is about to disappear, and a teardown clever
    enough to work around that is a worse risk than two short-lived pools.

    A context manager rather than only a fixture so the invariant it exists to
    hold can be asserted directly, in ``test_the_redis_fixture_leaves_the_module_as_it_found_it``.
    """
    from app.core import redis as redis_module

    saved = (
        getattr(redis_module, "_shared_client", None),
        getattr(redis_module, "_pool", None),
        getattr(redis_module, "_sentinel", None),
    )
    redis_module._build_client()
    try:
        yield
    finally:
        (
            redis_module._shared_client,
            redis_module._pool,
            redis_module._sentinel,
        ) = saved


@pytest.fixture
def fresh_redis():
    with _rebound_redis():
        yield


def test_the_redis_fixture_leaves_the_module_as_it_found_it():
    """The finding this test exists for: rebuilding without restoring fixes this
    file and breaks every Redis test after it. Asserted on the module globals
    directly, so it holds whatever order the suite runs in.
    """
    from app.core import redis as redis_module

    before = (
        redis_module._shared_client,
        redis_module._pool,
        redis_module._sentinel,
    )
    with _rebound_redis():
        # Inside, the client is a different object — that is the point of it.
        assert redis_module._shared_client is not before[0]
    assert (
        redis_module._shared_client,
        redis_module._pool,
        redis_module._sentinel,
    ) == before


def _proposal(call_id: str, tool: str = TOOL, **args) -> dict:
    return {
        "tool_call_id": call_id,
        "tool_name": tool,
        "arguments": args or dict(ARGS),
        "input_schema": {"type": "object"},
    }


async def _paused_turn(db, proposals: list[dict]) -> tuple[uuid.UUID, uuid.UUID]:
    """A conversation whose last turn ended at a gated call.

    Shaped exactly as the REST pause leaves it: a user message, then an
    assistant message whose *content is prose* — the explanation the client was
    shown — carrying the proposals in its metadata. No ``tool_calls`` column is
    written, which is the whole reason the resume has to rebuild them.
    """
    user = User(
        id=uuid.uuid4(),
        email=f"u-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name="Reviewer",
    )
    db.add(user)
    await db.flush()

    conv = Conversation(id=uuid.uuid4(), user_id=user.id, title="Approval")
    db.add(conv)
    await db.flush()

    db.add(
        Message(
            id=uuid.uuid4(),
            conversation_id=conv.id,
            role="user",
            content="Build me an agent that triages phishing reports.",
            sequence=1,
        )
    )
    assistant = Message(
        id=uuid.uuid4(),
        conversation_id=conv.id,
        role="assistant",
        content="I need approval before running: create_agent.",
        sequence=2,
        metadata_json=json.dumps({resume.PENDING_KEY: proposals}),
    )
    db.add(assistant)
    await db.commit()
    return conv.id, assistant.id


class _RecordingExecutor:
    """Executes an approved call and remembers exactly what it was asked to run."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def execute_tool(self, call):
        self.calls.append((call.name, dict(call.arguments)))
        return ToolResult(
            tool_call_id=call.id,
            content=f"created {call.arguments.get('name')}",
        )


@pytest.mark.asyncio
async def test_a_later_request_finds_the_proposals_from_the_database_alone():
    """FR-002c3: the process that paused the turn may be gone. Nothing in memory
    carries over, so the set has to be reconstructable from what was stored."""
    async with _temp_database("hitl_resume") as session:
        async with session() as db:
            conv_id, _ = await _paused_turn(db, [_proposal("call_1")])

        # A different session entirely — the closest a test gets to "another
        # replica answered it".
        async with session() as other:
            pending = await resume.load_pending(other, conv_id)

        assert pending is not None
        _message, proposals = pending
        assert [p["tool_call_id"] for p in proposals] == ["call_1"]
        assert proposals[0]["arguments"] == ARGS


@pytest.mark.asyncio
async def test_the_approved_call_runs_with_the_arguments_the_reviewer_saw():
    """FR-002c2, and the requirement that forbids re-planning: what runs is what
    was displayed, not whatever the model would propose on a second attempt."""
    from app.services.assignment_engine import _apply_approval_decisions

    async with _temp_database("hitl_resume") as session, session() as db:
        conv_id, _ = await _paused_turn(
            db, [_proposal("call_1"), _proposal("call_2", "create_template")]
        )
        pending = await resume.load_pending(db, conv_id)
        assert pending is not None
        _message, proposals = pending

        decisions = {
            "call_1": {"decision": "approve"},
            "call_2": {"decision": "reject", "rejection_reason": "wrong template"},
        }
        resume.require_complete(proposals, decisions)
        assert resume.approved_ids(proposals, decisions) == {"call_1"}

        messages: list = []
        resume.seed_gated_assistant_message(messages, proposals)
        executor = _RecordingExecutor()
        await _apply_approval_decisions(
            executor=executor,
            messages=messages,
            proposals=proposals,
            decisions=decisions,
            outcomes={},
        )

        # The approved call ran, once, with the displayed arguments.
        assert executor.calls == [(TOOL, ARGS)]

        # One tool result per proposal, in order, each paired with its call —
        # anything less is the orphaned ``tool_use`` strict-mode providers reject.
        assert [m.role for m in messages] == ["assistant", "tool", "tool"]
        assert [c.id for c in messages[0].tool_calls] == ["call_1", "call_2"]
        assert [m.tool_call_id for m in messages[1:]] == ["call_1", "call_2"]
        assert "created phishing-triage" in messages[1].content
        # The refusal is readable guidance, not an exception (FR-010).
        assert "Rejected by human reviewer: wrong template" in messages[2].content


@pytest.mark.asyncio
async def test_a_decided_set_cannot_be_answered_twice():
    """FR-002c4 and FR-020: a duplicate submission must find nothing pending, or
    the approved call runs a second time."""
    async with _temp_database("hitl_resume") as session, session() as db:
        conv_id, assistant_id = await _paused_turn(db, [_proposal("call_1")])

        await resume.mark_answered_by_id(db, assistant_id)
        await db.commit()

        assert await resume.load_pending(db, conv_id) is None

        # The record of what was asked survives; only its answerability is gone.
        row = await db.get(Message, assistant_id)
        stored = json.loads(row.metadata_json)
        assert stored[resume.RETIRED_KEY] == "answered"


@pytest.mark.asyncio
async def test_marking_answered_tolerates_a_vanished_message():
    """It runs after a whole agent turn, so the row it names may be gone. Failing
    there would discard a turn that already succeeded."""
    async with _temp_database("hitl_resume") as session, session() as db:
        await resume.mark_answered_by_id(db, uuid.uuid4())


@pytest.mark.asyncio
async def test_retirement_clears_redis_and_leaves_the_expired_record(fresh_redis):
    """FR-002c4 on both stores. Retiring only the message left the socket's
    prompt live, because the conversation response reads Redis."""
    from app.services.chat_turn_guard import (
        get_pending_approvals,
        set_pending_approvals,
    )

    async with _temp_database("hitl_resume") as session, session() as db:
        conv_id, assistant_id = await _paused_turn(db, [_proposal("call_1")])

        try:
            await set_pending_approvals(str(conv_id), [_proposal("call_live")])
            assert await get_pending_approvals(str(conv_id))
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Cannot reach Redis: {exc}")

        assert await resume.retire_for_new_turn(db, conv_id, surface="test") is True
        await db.commit()

        # Redis no longer serves it, so a reload cannot render a retired prompt
        # as answerable.
        assert not await get_pending_approvals(str(conv_id))

        row = await db.get(Message, assistant_id)
        stored = json.loads(row.metadata_json)
        assert stored[resume.RETIRED_KEY] == "superseded"
        # Both copies kept, so the expired state has everything to render.
        ids = {p["tool_call_id"] for p in stored[resume.PENDING_KEY]}
        assert ids == {"call_1", "call_live"}

        # And it is no longer answerable.
        assert await resume.load_pending(db, conv_id) is None


@pytest.mark.asyncio
async def test_a_settled_message_is_not_relabelled_by_an_orphaned_redis_set(
    fresh_redis,
):
    """When the paused turn never persisted a message, the latest assistant
    message belongs to an earlier turn. Stamping the Redis proposals there would
    render "needed your approval and was never run" under a message whose
    approval was given and whose tool ran."""
    from app.services.chat_turn_guard import (
        get_pending_approvals,
        set_pending_approvals,
    )

    async with _temp_database("hitl_resume") as session, session() as db:
        conv_id, assistant_id = await _paused_turn(db, [_proposal("call_1")])
        await resume.mark_answered_by_id(db, assistant_id)
        await db.commit()

        try:
            await set_pending_approvals(str(conv_id), [_proposal("orphan")])
            assert await get_pending_approvals(str(conv_id))
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Cannot reach Redis: {exc}")

        assert await resume.retire_for_new_turn(db, conv_id, surface="test") is True
        await db.commit()

        row = await db.get(Message, assistant_id)
        stored = json.loads(row.metadata_json)
        assert stored[resume.RETIRED_KEY] == "answered", (
            "an answered message must not be relabelled as superseded"
        )
        # ``mark_answered`` drops the pending set — answered is not expired, so
        # there is nothing to render as expired. The point of this assertion is
        # that the *orphaned* proposals were not stamped in to fill the gap,
        # which would have rendered "never run" under a call that did run.
        assert resume.PENDING_KEY not in stored, (
            "another turn's proposals must not be stamped onto a settled message"
        )
        assert not await get_pending_approvals(str(conv_id)), (
            "the orphaned set is still cleared — it is simply not rendered"
        )
