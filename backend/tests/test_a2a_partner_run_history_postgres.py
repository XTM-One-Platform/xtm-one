"""A partner task reads the run history of that partner's own tasks, nothing else.

Inside the partner-task scope the agent builder acts as an anonymous caller,
which reads no run history. ``list_runs`` / ``get_run`` lend one read back,
scoped by the partner principal (exposure and OAuth client): the runs the
partner's own A2A tasks started and the runs those spawned. Another partner's
run, a chat or assignment run, and a run of the same agent a user started
answer exactly like an unknown id. Outside the scope nothing changes.

Against real PostgreSQL (the recursive walk), like its neighbours.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.core.database as database
import app.models  # noqa: F401 - registers every table on Base.metadata
from app.a2a.partner_scope import PartnerPrincipal, partner_task_scope
from app.models.a2a import A2AContext, A2AExposure, A2ATask
from app.models.agent import Agent
from app.models.assignment import AssignmentRun
from app.models.user import User
from app.services.builtin_tools import build_agent_builder_tool
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

PARTNER_A = "partner-a"
PARTNER_B = "partner-b"


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with scratch_database(
        "a2aprh",
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
        bind_app_sessions=False,
    ) as db:
        yield db.sessions


@dataclass
class World:
    admin: User
    exposure_id: uuid.UUID
    agent_id: uuid.UUID
    # Partner A: two tasks, the first one's run spawned a consult.
    a_runs: list[uuid.UUID]
    a_tasks: list[uuid.UUID]
    a_consult: uuid.UUID
    b_run: uuid.UUID
    chat_run: uuid.UUID


def _run(
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    created: datetime,
    parent: uuid.UUID | None = None,
    result: str | None = None,
    trigger: dict[str, Any] | None = None,
) -> AssignmentRun:
    return AssignmentRun(
        id=uuid.uuid4(),
        assignment_id=None,
        agent_id=agent_id,
        user_id=user_id,
        parent_run_id=parent,
        status="completed",
        created_at=created,
        started_at=created,
        completed_at=created + timedelta(seconds=5),
        result_summary=result,
        trigger_payload=trigger or {"message": "hello"},
        trigger_context={"source": "a2a"},
    )


async def _seed(sessions: async_sessionmaker[AsyncSession]) -> World:
    now = datetime.now(UTC)
    async with sessions() as db:
        admin = User(
            email=f"admin-{uuid.uuid4().hex[:8]}@example.com", hashed_password="x", full_name="admin", role="admin"
        )
        db.add(admin)
        await db.flush()
        agent = Agent(id=uuid.uuid4(), name="Exposed intake", user_id=admin.id, slug=f"intake-{uuid.uuid4().hex[:8]}")
        db.add(agent)
        await db.flush()
        exposure = A2AExposure(agent_id=agent.id, card_token=secrets.token_urlsafe(24), run_as_user_id=admin.id)
        db.add(exposure)
        await db.flush()

        a1 = _run(agent.id, admin.id, created=now - timedelta(minutes=30), result="Answer to A's first task.")
        a2 = _run(agent.id, admin.id, created=now - timedelta(minutes=10), result="Answer to A's second task.")
        consult = _run(
            agent.id,
            admin.id,
            created=now - timedelta(minutes=29),
            parent=a1.id,
            result="CONSULT INTERNAL RESULT",
            trigger={"trigger_type": "delegation", "delegation_type": "consult", "task": "secret prompt"},
        )
        b1 = _run(agent.id, admin.id, created=now - timedelta(minutes=20), result="Answer to B's task.")
        # The same agent, started by a user in chat: never a partner's.
        chat = _run(agent.id, admin.id, created=now - timedelta(minutes=5), result="A chat answer.")
        db.add_all([a1, a2, consult, b1, chat])
        await db.flush()

        tasks = []
        for client_id, run in ((PARTNER_A, a1), (PARTNER_A, a2), (PARTNER_B, b1)):
            context = A2AContext(client_id=client_id, responsible_agent_id=agent.id, exposure_id=exposure.id)
            db.add(context)
            await db.flush()
            task = A2ATask(
                exposure_id=exposure.id,
                client_id=client_id,
                run_id=run.id,
                message_id=f"m-{uuid.uuid4().hex}",
                inbound_parts=[{"kind": "text", "text": "hello"}],
                context_id=context.id,
            )
            db.add(task)
            await db.flush()
            tasks.append(task.id)
        await db.commit()
    return World(
        admin=admin,
        exposure_id=exposure.id,
        agent_id=agent.id,
        a_runs=[a1.id, a2.id],
        a_tasks=tasks[:2],
        a_consult=consult.id,
        b_run=b1.id,
        chat_run=chat.id,
    )


WorldFactory = Callable[[], AbstractAsyncContextManager[World]]


async def _tool(user_id: str, tool: str, /, **arguments: str) -> str:
    _, executor = build_agent_builder_tool(None, user_id)  # type: ignore[arg-type]  # the builder opens its own sessions
    result = await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=tool, arguments=arguments))
    return result.content


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> WorldFactory:
    @asynccontextmanager
    async def _world() -> AsyncIterator[World]:
        async with _temp_database() as sessions:
            monkeypatch.setattr(database, "async_session", sessions)
            yield await _seed(sessions)

    return _world


def _partner(w: World, client_id: str) -> PartnerPrincipal:
    return PartnerPrincipal(exposure_id=w.exposure_id, client_id=client_id)


class TestAPartnerReadsItsOwnRuns:
    async def test_list_runs_answers_the_partners_runs_newest_first(self, world: WorldFactory) -> None:
        async with world() as w:
            with partner_task_scope(principal=_partner(w, PARTNER_A)):
                text = await _tool("anonymous", "list_runs")
        assert "Found 3 run(s) of your tasks" in text
        a1, a2 = (str(r) for r in w.a_runs)
        assert text.index(a2) < text.index(str(w.a_consult)) < text.index(a1)
        for task_id in w.a_tasks:
            assert str(task_id) in text
        assert str(w.b_run) not in text and str(w.chat_run) not in text
        # A spawned run shows no result and no prompt.
        assert "CONSULT INTERNAL RESULT" not in text and "secret prompt" not in text

    async def test_get_run_answers_status_times_task_and_delivered_result(self, world: WorldFactory) -> None:
        async with world() as w:
            with partner_task_scope(principal=_partner(w, PARTNER_A)):
                text = await _tool("anonymous", "get_run", run_id=str(w.a_runs[1]))
        assert f"Partner task: `{w.a_tasks[1]}`" in text
        assert "Status: completed" in text and "Ended:" in text
        assert "Answer to A's second task." in text

    async def test_a_consult_under_the_task_reads_the_same_set(self, world: WorldFactory) -> None:
        """A consult runs in the task's asyncio context: same scope, same partner."""
        async with world() as w:
            with partner_task_scope(principal=_partner(w, PARTNER_A)):

                async def _consulted() -> str:
                    return await _tool("anonymous", "get_run", run_id=str(w.a_consult))

                text = await _consulted()
        assert f"Partner task: `{w.a_tasks[0]}`" in text
        assert "CONSULT INTERNAL RESULT" not in text and "secret prompt" not in text


class TestEverythingElseIsAnUnknownId:
    @pytest.mark.parametrize("which", ["other_partner", "chat_run", "unknown"])
    async def test_get_run_outside_the_set_is_not_found(self, world: WorldFactory, which: str) -> None:
        async with world() as w:
            run_id = {"other_partner": w.b_run, "chat_run": w.chat_run, "unknown": uuid.uuid4()}[which]
            with partner_task_scope(principal=_partner(w, PARTNER_A)):
                text = await _tool("anonymous", "get_run", run_id=str(run_id))
        assert text == f"Run `{run_id}` not found."

    async def test_two_partners_on_one_exposure_are_isolated(self, world: WorldFactory) -> None:
        async with world() as w:
            with partner_task_scope(principal=_partner(w, PARTNER_B)):
                listed = await _tool("anonymous", "list_runs")
                for run_id in (*w.a_runs, w.a_consult):
                    assert await _tool("anonymous", "get_run", run_id=str(run_id)) == f"Run `{run_id}` not found."
        assert "Found 1 run(s) of your tasks" in listed and str(w.b_run) in listed
        assert not any(str(r) in listed for r in (*w.a_runs, w.a_consult, w.chat_run))

    async def test_the_run_as_reach_never_widens_it(self, world: WorldFactory) -> None:
        """The exposure runs as an admin; the partner still reads only its own."""
        async with world() as w:
            with partner_task_scope(principal=_partner(w, PARTNER_A)):
                text = await _tool(str(w.admin.id), "get_run", run_id=str(w.chat_run))
        assert text == f"Run `{w.chat_run}` not found."


class TestOutsideThePartnerScopeNothingChanges:
    async def test_an_anonymous_caller_still_reads_no_run_history(self, world: WorldFactory) -> None:
        async with world():
            assert await _tool("anonymous", "list_runs") == "Run history is not available for anonymous users."

    async def test_a_scope_with_no_known_partner_lends_nothing(self, world: WorldFactory) -> None:
        async with world() as w:
            with partner_task_scope():
                text = await _tool("anonymous", "get_run", run_id=str(w.a_runs[0]))
        assert text == "Run history is not available for anonymous users."
