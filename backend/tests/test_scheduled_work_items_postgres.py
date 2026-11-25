"""Scheduled work items against PostgreSQL: the tick and the run.

On an agent with no ``work_items`` built-in tool and no assignment, a
scheduled item is dispatched by the scheduler tick to ONE assignment-less run,
in the same commit as its claim, and the run sends the stored action exactly
as stored. The run re-checks everything when it starts: a revoked binding, a
lost right to use the agent or a re-run after a lost attempt sends nothing.
The tool itself is a recording stand-in (a real Teams send needs a tenant);
everything around it - the claim, the run, the execution, the outcome, the
conversation and the notification - is the real code on a real database.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

import app.models  # noqa: F401 - registers every mapper
from app.models.activity import Activity
from app.models.agent import Agent
from app.models.assignment import AssignmentRun
from app.models.audit_log import AuditLog
from app.models.chat import Conversation, Message
from app.models.orchestration import OrchestrationExecution
from app.models.user import User
from app.models.work_item import WorkItem
from app.services import scheduled_work_item_runner as runner
from app.services import scheduled_work_items as swi
from app.services.agentic_gate import AgenticVerdict
from app.services.company_managed_writes import agent_writer
from app.tools.base import CompositeToolExecutor, ToolDefinition, ToolResult
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

PARIS = "Europe/Paris"
BODY = "Test <at>Testing XTM One</at>\n\nLe point est \u00e0 9h \u2014 merci."


class _RecordingTool:
    """A stand-in for one integration tool: records every call it receives."""

    def __init__(self, *, name="send_teams_channel_message", result=None):
        self.name = name
        self.calls: list[dict] = []
        self.result = result or ToolResult(
            tool_call_id="", content='{"message_id": "1700000000000", "link": "https://teams.example/m/1"}'
        )
        self.file_context = None

    async def execute_tool(self, call):
        self.calls.append(call.arguments)
        self.result.tool_call_id = call.id
        return self.result

    def toolset(self):
        composite = CompositeToolExecutor()
        composite.add_tools(
            [
                ToolDefinition(
                    name=self.name,
                    description="Post a message in a Teams channel",
                    input_schema={
                        "type": "object",
                        "properties": {"channel_id": {"type": "string"}, "message": {"type": "string"}},
                        "required": ["channel_id", "message"],
                    },
                    source="integration:microsoft_teams",
                )
            ],
            self,
        )
        return runner.ExactToolset(composite=composite, names=[self.name])


@pytest.fixture(autouse=True)
def _fresh_leftover_walk(monkeypatch):
    """Every test starts the blocked-item walk from the first row.

    ``runner._leftover_cursor`` is a process-local module global: without this
    reset a test inherits the cursor an earlier test on the same xdist worker
    left behind, and its one tick starts after rows it expects to reach.
    """
    monkeypatch.setattr(runner, "_leftover_cursor", None)


@pytest.fixture
def allowed(monkeypatch):
    """Agentic AI available, scheduling on, no Redis needed for the outcome."""

    async def _verdict(_db):
        return AgenticVerdict(None)

    monkeypatch.setattr("app.services.agentic_gate.agentic_verdict", _verdict)
    monkeypatch.setattr(swi, "scheduling_enabled", lambda: True)

    async def _quiet(_item):
        return None

    monkeypatch.setattr(swi, "publish_update", _quiet)


def _with_tool(monkeypatch, tool: _RecordingTool | None):
    async def _resolve(_db, _agent, _user_id, *, pinned=None):
        if tool is None:
            return runner.ExactToolset(composite=CompositeToolExecutor(), names=[])
        return tool.toolset()

    monkeypatch.setattr(runner, "resolve_exact_toolset", _resolve)

    async def _no_gate(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runner, "approval_refusal", _no_gate)


async def _seed(sessions, *, builtin_tool_ids=None):
    async with sessions() as db:
        user = User(
            id=uuid.uuid4(),
            email=f"samuel-{uuid.uuid4().hex[:6]}@filigran.io",
            full_name="Samuel",
            hashed_password="x",
            role="user",
            timezone=PARIS,
        )
        db.add(user)
        await db.flush()
        agent = Agent(
            id=uuid.uuid4(),
            name="[EMT & ExCo] Executive Assistant",
            user_id=user.id,
            builtin_tool_ids=builtin_tool_ids or [],
        )
        db.add(agent)
        await db.flush()
        conversation = Conversation(user_id=user.id, agent_id=agent.id, channel="web")
        db.add(conversation)
        await db.commit()
        return user, agent, conversation


async def _schedule(user, agent, conversation, *, minutes_ago: int = 1, mode: str = "exact") -> str:
    due = swi.parse_due_at(
        (datetime.now(UTC) + timedelta(hours=2)).isoformat(), tz_name=PARIS
    )
    action = (
        swi.build_action(
            "exact",
            tool="send_teams_channel_message",
            arguments={"channel_id": "19:testing@thread.tacv2", "message": BODY},
            target="Testing XTM One",
            notify=True,
        )
        if mode == "exact"
        else swi.build_action("instruction", prompt="Post the weekly summary", notify=True)
    )
    item = await swi.create_scheduled_work_item(
        user_id=str(user.id),
        agent_id=str(agent.id),
        agent_name=agent.name,
        title="Post the EMT reminder",
        due=due,
        action=action,
        conversation_id=str(conversation.id),
    )
    await _make_due(item["id"], minutes_ago=minutes_ago)
    return item["id"]


async def _make_due(item_id: str, *, minutes_ago: int) -> None:
    """Time passing: the item's ``due_at`` moves into the past."""
    from app.core.database import async_session

    async with async_session() as db:
        await db.execute(
            update(WorkItem)
            .where(WorkItem.id == uuid.UUID(item_id))
            .values(due_at=datetime.now(UTC) - timedelta(minutes=minutes_ago))
        )
        await db.commit()


async def _item(sessions, item_id: str) -> WorkItem:
    async with sessions() as db:
        return (await db.execute(select(WorkItem).where(WorkItem.id == uuid.UUID(item_id)))).scalar_one()


async def _run(sessions, run_id) -> AssignmentRun:
    async with sessions() as db:
        return (await db.execute(select(AssignmentRun).where(AssignmentRun.id == run_id))).scalar_one()


async def _tick(sessions) -> int:
    async with sessions() as db:
        return await runner.dispatch_due_scheduled_work_items(db)


@pytest.mark.asyncio
async def test_a_due_item_is_dispatched_to_one_assignment_less_run(allowed):
    async with scratch_database("swi", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        user, agent, conversation = await _seed(scratch.sessions)
        item_id = await _schedule(user, agent, conversation)
        # An ordinary work item of the same agent, due too.
        async with scratch.sessions() as db:
            ordinary = WorkItem(
                user_id=user.id,
                owner_agent_id=agent.id,
                title="Ordinary follow-up",
                payload={},
                status="open",
                due_at=datetime.now(UTC) - timedelta(minutes=5),
            )
            db.add(ordinary)
            await db.commit()

        assert await _tick(scratch.sessions) == 1

        item = await _item(scratch.sessions, item_id)
        assert item.status == "claimed" and item.claimed_by_agent_id == agent.id
        run = await _run(scratch.sessions, item.run_id)
        assert run.assignment_id is None
        assert run.agent_id == agent.id and run.user_id == user.id
        assert run.trigger_payload == {"trigger_type": "scheduled_work_item", "work_item_id": item_id}
        assert run.status == "pending"
        async with scratch.sessions() as db:
            execution = (
                await db.execute(
                    select(OrchestrationExecution).where(
                        OrchestrationExecution.dedup_key == f"assignment_run:{run.id}"
                    )
                )
            ).scalar_one()
            assert execution.adapter == "scheduled_work_item"
            assert execution.payload["work_item_id"] == item_id
            # The ordinary item was left to the work-item consumers.
            assert (await db.get(WorkItem, ordinary.id)).status == "open"
            created = (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == uuid.UUID(item_id), AuditLog.action == "create"
                    )
                )
            ).scalar_one()
            assert created.details["action"]["arguments"]["message"] == BODY

        # Neither consumer takes the other's items.
        from app.services.work_item import list_dispatchable_work_items

        offered = await list_dispatchable_work_items(
            owner_agent_id=str(agent.id),
            user_id=str(user.id),
            writer=agent_writer(is_company_managed=False, agent_id=agent.id),
        )
        assert [i["id"] for i in offered] == [str(ordinary.id)]
        assert await _tick(scratch.sessions) == 0


@pytest.mark.asyncio
async def test_two_ticks_racing_create_one_run(allowed):
    async with scratch_database("swi", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        user, agent, conversation = await _seed(scratch.sessions)
        await _schedule(user, agent, conversation)
        results = await asyncio.gather(_tick(scratch.sessions), _tick(scratch.sessions))
        assert sorted(results) == [0, 1]
        async with scratch.sessions() as db:
            assert (await db.execute(select(func.count()).select_from(AssignmentRun))).scalar_one() == 1


@pytest.mark.asyncio
async def test_a_cancelled_item_creates_no_run_and_leaves_the_upcoming_view(allowed):
    async with scratch_database("swi", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        user, agent, _conversation = await _seed(scratch.sessions)
        due = swi.parse_due_at((datetime.now(UTC) + timedelta(hours=2)).isoformat())
        item = await swi.create_scheduled_work_item(
            user_id=str(user.id),
            agent_id=str(agent.id),
            agent_name=agent.name,
            title="To cancel",
            due=due,
            action=swi.build_action("instruction", prompt="Nothing"),
        )
        assert [i["id"] for i in await swi.list_scheduled_work_items(user_id=str(user.id))] == [item["id"]]
        cancelled = await swi.cancel_scheduled_work_item(item["id"], user_id=str(user.id), via="rest")
        assert cancelled["status"] == "cancelled"
        await _make_due(item["id"], minutes_ago=1)
        assert await _tick(scratch.sessions) == 0
        assert await swi.list_scheduled_work_items(user_id=str(user.id)) == []
        async with scratch.sessions() as db:
            assert (await db.execute(select(func.count()).select_from(AssignmentRun))).scalar_one() == 0
            audited = (
                await db.execute(
                    select(AuditLog.action).where(AuditLog.entity_id == uuid.UUID(item["id"]))
                )
            ).scalars().all()
            assert sorted(audited) == ["cancel", "create"]
        # Another agent cannot cancel it: the tools only reach their own agent's items.
        assert (
            await swi.cancel_scheduled_work_item(
                item["id"], user_id=str(user.id), agent_id=str(uuid.uuid4())
            )
            is None
        )


@pytest.mark.asyncio
async def test_the_cancel_tool_takes_an_id_prefix_of_the_persons_scheduled_item(allowed: object) -> None:
    from app.services.builtin_tools import build_scheduling_tools
    from app.tools.base import ToolCall

    async with scratch_database("swi", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        user, agent, _conversation = await _seed(scratch.sessions)  # type: ignore[no-untyped-call]
        due = swi.parse_due_at((datetime.now(UTC) + timedelta(hours=2)).isoformat())
        item = await swi.create_scheduled_work_item(
            user_id=str(user.id),
            agent_id=str(agent.id),
            agent_name=agent.name,
            title="To cancel by prefix",
            due=due,
            action=swi.build_action("instruction", prompt="Nothing"),
        )
        _definitions, executor = build_scheduling_tools(
            user_id=str(user.id), agent_id=str(agent.id), agent_name=agent.name
        )
        _stranger_definitions, stranger = build_scheduling_tools(
            user_id=str(uuid.uuid4()), agent_id=str(agent.id), agent_name=agent.name
        )
        # Another person's prefix finds nothing: the usual refusal, nothing changed.
        refused = await stranger.execute_tool(
            ToolCall(id="c0", name="cancel_scheduled_work_item", arguments={"work_item_id": item["id"][:8]})
        )
        assert refused.content.startswith("Error:")
        answer = await executor.execute_tool(
            ToolCall(id="c1", name="cancel_scheduled_work_item", arguments={"work_item_id": item["id"][:8]})
        )
        assert answer.content.startswith("Cancelled"), answer.content
        assert item["id"] in answer.content
        async with scratch.sessions() as db:
            stored = (await db.execute(select(WorkItem).where(WorkItem.id == uuid.UUID(item["id"])))).scalar_one()
            assert stored.status == "cancelled"


async def _block_as_interrupted(sessions, item_id: str) -> None:
    """A platform restart cut the item's run: it is left blocked."""
    async with sessions() as db:
        await db.execute(
            update(WorkItem)
            .where(WorkItem.id == uuid.UUID(item_id))
            .values(status="blocked", outcome_code=swi.OUTCOME_INTERRUPTED, result="Restarted.")
        )
        await db.commit()


@pytest.mark.asyncio
async def test_the_orchestrator_cancels_its_desks_action_and_a_stranger_does_not(allowed):
    async with scratch_database("swi", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        user, desk, _conversation = await _seed(scratch.sessions)
        async with scratch.sessions() as db:
            orchestrator = Agent(
                id=uuid.uuid4(),
                name="Orchestrator",
                user_id=user.id,
                handover_agent_ids=[str(desk.id)],
                handover_types={str(desk.id): "delegate"},
            )
            consultant = Agent(
                id=uuid.uuid4(),
                name="Consultant",
                user_id=user.id,
                handover_agent_ids=[str(desk.id)],
                handover_types={str(desk.id): "consult"},
            )
            db.add_all([orchestrator, consultant])
            await db.commit()
        due = swi.parse_due_at((datetime.now(UTC) + timedelta(hours=2)).isoformat())
        item = await swi.create_scheduled_work_item(
            user_id=str(user.id),
            agent_id=str(desk.id),
            agent_name=desk.name,
            title="Desk retest",
            due=due,
            action=swi.build_action("instruction", prompt="Retest"),
        )
        # Waiting to run: a consulted peer may not, the orchestrator may.
        assert (
            await swi.cancel_scheduled_work_item(
                item["id"], user_id=str(user.id), agent_id=str(consultant.id)
            )
            is None
        )
        cancelled = await swi.cancel_scheduled_work_item(
            item["id"], user_id=str(user.id), agent_id=str(orchestrator.id)
        )
        assert cancelled["status"] == "cancelled"
        assert "by Orchestrator" in cancelled["result"]
        async with scratch.sessions() as db:
            audit = (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == uuid.UUID(item["id"]), AuditLog.action == "cancel"
                    )
                )
            ).scalar_one()
            assert audit.details["acting_agent_id"] == str(orchestrator.id)
            assert audit.details["authority"] == swi.AUTHORITY_HANDOVER

        # A blocked leftover: any agent of the person may clear it.
        leftover = await swi.create_scheduled_work_item(
            user_id=str(user.id),
            agent_id=str(desk.id),
            agent_name=desk.name,
            title="Interrupted retest",
            due=due,
            action=swi.build_action("instruction", prompt="Retest"),
        )
        await _block_as_interrupted(scratch.sessions, leftover["id"])
        cleared = await swi.cancel_scheduled_work_item(
            leftover["id"], user_id=str(user.id), agent_id=str(consultant.id)
        )
        assert cleared["status"] == "cancelled"

        # Another person's agent never reaches it.
        stranger, stranger_agent, _ = await _seed(scratch.sessions)
        other = await swi.create_scheduled_work_item(
            user_id=str(user.id),
            agent_id=str(desk.id),
            agent_name=desk.name,
            title="Not theirs",
            due=due,
            action=swi.build_action("instruction", prompt="Retest"),
        )
        await _block_as_interrupted(scratch.sessions, other["id"])
        assert (
            await swi.cancel_scheduled_work_item(
                other["id"], user_id=str(stranger.id), agent_id=str(stranger_agent.id)
            )
            is None
        )


@pytest.mark.asyncio
async def test_an_agent_reaches_its_one_off_scheduled_for_another_person(allowed: object) -> None:
    """#2220: the owner's run of an agent lists and cancels the one-off it scheduled
    for another person; that person's own agent and an unrelated owner do not."""
    async with scratch_database("swi", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        owner, desk, _conversation = await _seed(scratch.sessions)  # type: ignore[no-untyped-call]
        other, other_agent, _ = await _seed(scratch.sessions)  # type: ignore[no-untyped-call]
        async with scratch.sessions() as db:
            orchestrator = Agent(
                id=uuid.uuid4(),
                name="Orchestrator",
                user_id=owner.id,
                handover_agent_ids=[str(desk.id)],
                handover_types={str(desk.id): "delegate"},
            )
            db.add(orchestrator)
            await db.commit()
        due = swi.parse_due_at((datetime.now(UTC) + timedelta(hours=2)).isoformat())
        # Scheduled by the owner's desk in a run acting for another person.
        item = await swi.create_scheduled_work_item(
            user_id=str(other.id),
            agent_id=str(desk.id),
            agent_name=desk.name,
            title="Renewal watch",
            due=due,
            action=swi.build_action("instruction", prompt="Watch the renewal"),
        )

        listed = await swi.list_scheduled_work_items(user_id=str(owner.id), agent_id=str(desk.id))
        assert [row["id"] for row in listed] == [item["id"]]
        # Another person's agent, and the person's own view, keep their scope.
        assert await swi.list_scheduled_work_items(user_id=str(other.id), agent_id=str(other_agent.id)) == []
        assert (
            await swi.cancel_scheduled_work_item(item["id"], user_id=str(other.id), agent_id=str(other_agent.id))
            is None
        )
        # Rescheduling keeps the person scope (cancel only widens).
        assert (
            await swi.reschedule_scheduled_work_item(item["id"], due, user_id=str(owner.id), agent_id=str(desk.id))
            is None
        )

        # The owner's orchestrator (delegates to the desk) cancels it.
        cancelled = await swi.cancel_scheduled_work_item(
            item["id"], user_id=str(owner.id), agent_id=str(orchestrator.id)
        )
        assert cancelled is not None and cancelled["status"] == "cancelled"


@pytest.mark.asyncio
async def test_the_scheduler_cancels_a_blocked_action_whose_work_item_is_done(allowed):
    async with scratch_database("swi", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        user, agent, conversation = await _seed(scratch.sessions)
        async with scratch.sessions() as db:
            target = WorkItem(user_id=user.id, owner_agent_id=agent.id, title="Retest me", payload={}, status="done")
            pending = WorkItem(user_id=user.id, owner_agent_id=agent.id, title="Still open", payload={}, status="open")
            db.add_all([target, pending])
            await db.commit()
        due = swi.parse_due_at((datetime.now(UTC) + timedelta(hours=2)).isoformat())
        served_done = await swi.create_scheduled_work_item(
            user_id=str(user.id),
            agent_id=str(agent.id),
            agent_name=agent.name,
            title="Retest, then complete",
            due=due,
            action=swi.build_action("instruction", prompt=f"Retest and complete item {target.id}"),
            conversation_id=str(conversation.id),
        )
        served_open = await swi.create_scheduled_work_item(
            user_id=str(user.id),
            agent_id=str(agent.id),
            agent_name=agent.name,
            title="Retest the open one",
            due=due,
            action=swi.build_action("instruction", prompt="Retest", serves_work_item_id=str(pending.id)),
        )
        for item_id in (served_done["id"], served_open["id"]):
            await _block_as_interrupted(scratch.sessions, item_id)

        assert await _tick(scratch.sessions) == 0

        done = await _item(scratch.sessions, served_done["id"])
        assert done.status == "cancelled" and done.outcome_code is None
        assert runner.TARGET_ALREADY_DONE in done.result
        assert (await _item(scratch.sessions, served_open["id"])).status == "blocked"
        async with scratch.sessions() as db:
            audit = (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == uuid.UUID(served_done["id"]), AuditLog.action == "cancel"
                    )
                )
            ).scalar_one()
            assert audit.details["reason"] == runner.TARGET_ALREADY_DONE
            assert audit.details["served_work_items"] == {str(target.id): "done"}


async def _served_action(user, agent, title: str, target_id) -> str:
    due = swi.parse_due_at((datetime.now(UTC) + timedelta(hours=2)).isoformat())
    item = await swi.create_scheduled_work_item(
        user_id=str(user.id),
        agent_id=str(agent.id),
        agent_name=agent.name,
        title=title,
        due=due,
        action=swi.build_action("instruction", prompt="Retest", serves_work_item_id=str(target_id)),
    )
    return item["id"]


async def _plain_item(sessions, user, agent, status: str) -> WorkItem:
    async with sessions() as db:
        item = WorkItem(user_id=user.id, owner_agent_id=agent.id, title=f"Target {status}", payload={}, status=status)
        db.add(item)
        await db.commit()
        return item


@pytest.mark.asyncio
async def test_an_older_leftover_is_not_starved_by_newer_blocked_items(allowed, monkeypatch):
    monkeypatch.setattr(runner, "_LEFTOVER_BATCH_SIZE", 2)
    async with scratch_database("swi", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        user, agent, _conversation = await _seed(scratch.sessions)
        done = await _plain_item(scratch.sessions, user, agent, "done")
        still_open = await _plain_item(scratch.sessions, user, agent, "open")
        waiting = [await _served_action(user, agent, f"Waiting {n}", still_open.id) for n in range(3)]
        leftover = await _served_action(user, agent, "Served", done.id)
        for item_id in [*waiting, leftover]:
            await _block_as_interrupted(scratch.sessions, item_id)

        # The walk is a keyset on random uuid4 ids: the leftover sorts in either
        # batch, so the contract is "reached within ceil(N / batch) ticks".
        blocked = len(waiting) + 1
        for _ in range(-(-blocked // runner._LEFTOVER_BATCH_SIZE)):
            await _tick(scratch.sessions)

        assert (await _item(scratch.sessions, leftover)).status == "cancelled"
        for item_id in waiting:
            assert (await _item(scratch.sessions, item_id)).status == "blocked"


@pytest.mark.asyncio
async def test_a_target_requeued_after_the_scan_keeps_its_action_blocked(allowed, monkeypatch):
    async with scratch_database("swi", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        user, agent, _conversation = await _seed(scratch.sessions)
        target = await _plain_item(scratch.sessions, user, agent, "done")
        leftover = await _served_action(user, agent, "Served", target.id)
        await _block_as_interrupted(scratch.sessions, leftover)
        read_statuses = runner._target_statuses

        async def _requeued_before_the_lock(db, served, *, lock=False):
            if lock:
                async with scratch.sessions() as other:
                    await other.execute(update(WorkItem).where(WorkItem.id == target.id).values(status="open"))
                    await other.commit()
            return await read_statuses(db, served, lock=lock)

        monkeypatch.setattr(runner, "_target_statuses", _requeued_before_the_lock)
        await _tick(scratch.sessions)

        assert (await _item(scratch.sessions, leftover)).status == "blocked"


@pytest.mark.asyncio
async def test_an_item_missed_and_cancelled_in_one_tick_is_delivered_once(allowed, monkeypatch):
    monkeypatch.setattr(swi.settings, "scheduled_work_item_grace_minutes", 15)
    delivered: list = []

    async def _record(item_id):
        delivered.append(item_id)

    monkeypatch.setattr(swi, "deliver_outcome", _record)
    async with scratch_database("swi", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        user, agent, _conversation = await _seed(scratch.sessions)
        target = await _plain_item(scratch.sessions, user, agent, "cancelled")
        item_id = await _served_action(user, agent, "Came due while down", target.id)
        await _make_due(item_id, minutes_ago=120)

        await _tick(scratch.sessions)

        assert (await _item(scratch.sessions, item_id)).status == "cancelled"
        assert delivered == [uuid.UUID(item_id)]


@pytest.mark.asyncio
async def test_an_item_nobody_dispatched_in_time_is_missed_and_notified(allowed, monkeypatch):
    monkeypatch.setattr(swi.settings, "scheduled_work_item_grace_minutes", 15)
    async with scratch_database("swi", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        user, agent, conversation = await _seed(scratch.sessions)
        # The platform was down across the due time.
        item_id = await _schedule(user, agent, conversation, minutes_ago=120)
        assert await _tick(scratch.sessions) == 0
        item = await _item(scratch.sessions, item_id)
        assert item.status == "blocked" and item.outcome_code == swi.OUTCOME_MISSED
        assert swi.schedule_state(item) == "missed"
        assert item.run_id is None
        async with scratch.sessions() as db:
            note = (
                await db.execute(select(Activity).where(Activity.user_id == user.id))
            ).scalar_one()
            assert note.extra_data["schedule_state"] == "missed"
            posted = (
                await db.execute(select(Message).where(Message.conversation_id == conversation.id))
            ).scalar_one()
            # The outcome is one readable sentence: the scheduling turn's card
            # already shows the item, so no second card marker (#10309).
            assert posted.content.startswith("**Missed**: ")
            assert swi.marker(item_id) not in posted.content

        # Rescheduled, it runs again.
        due = swi.parse_due_at((datetime.now(UTC) + timedelta(hours=1)).isoformat())
        moved = await swi.reschedule_scheduled_work_item(item_id, due, user_id=str(user.id))
        assert moved["status"] == "open" and moved["scheduled"]["state"] == "scheduled"
        await _make_due(item_id, minutes_ago=1)
        assert await _tick(scratch.sessions) == 1


@pytest.mark.asyncio
async def test_an_exact_action_sends_the_stored_body_byte_for_byte(allowed, monkeypatch):
    tool = _RecordingTool()
    _with_tool(monkeypatch, tool)
    async with scratch_database("swi", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        user, agent, conversation = await _seed(scratch.sessions)
        item_id = await _schedule(user, agent, conversation)
        await _tick(scratch.sessions)
        run_id = (await _item(scratch.sessions, item_id)).run_id

        outcome = await runner.execute_scheduled_run(str(run_id), attempt=1)

        assert outcome.kind == "done"
        assert tool.calls == [{"channel_id": "19:testing@thread.tacv2", "message": BODY}]
        item = await _item(scratch.sessions, item_id)
        assert item.status == "done" and "1700000000000" in item.result
        run = await _run(scratch.sessions, run_id)
        assert run.status == "completed" and run.actions_taken[0]["tool"] == tool.name
        async with scratch.sessions() as db:
            posted = (
                await db.execute(select(Message).where(Message.conversation_id == conversation.id))
            ).scalar_one()
            # The raw tool result stays on the item, never in the posted line,
            # which links the run instead of repeating the card (#10309).
            assert posted.role == "assistant" and posted.content.startswith("**Done**: ")
            assert "1700000000000" not in posted.content
            # Absolute since #11952: the link leaves the web app (#11950).
            assert f"({swi.run_url(run_id)})" in posted.content
            assert swi.marker(item_id) not in posted.content


@pytest.mark.asyncio
async def test_a_revoked_binding_blocks_the_item_and_sends_nothing(allowed, monkeypatch):
    _with_tool(monkeypatch, None)
    async with scratch_database("swi", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        user, agent, conversation = await _seed(scratch.sessions)
        item_id = await _schedule(user, agent, conversation)
        await _tick(scratch.sessions)
        run_id = (await _item(scratch.sessions, item_id)).run_id

        outcome = await runner.execute_scheduled_run(str(run_id), attempt=1)

        assert outcome.kind == "blocked"
        item = await _item(scratch.sessions, item_id)
        assert item.status == "blocked" and item.outcome_code == swi.OUTCOME_BINDING
        assert "Nothing was sent" in item.result
        run = await _run(scratch.sessions, run_id)
        assert run.status == "failed" and "Nothing was sent" in run.error


@pytest.mark.asyncio
async def test_a_person_who_lost_the_agent_is_blocked_before_anything_runs(allowed, monkeypatch):
    tool = _RecordingTool()
    _with_tool(monkeypatch, tool)
    async with scratch_database("swi", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        user, agent, conversation = await _seed(scratch.sessions)
        item_id = await _schedule(user, agent, conversation)
        await _tick(scratch.sessions)
        run_id = (await _item(scratch.sessions, item_id)).run_id
        async with scratch.sessions() as db:
            other = User(
                id=uuid.uuid4(), email="other@filigran.io", full_name="Other", hashed_password="x", role="user"
            )
            db.add(other)
            await db.flush()
            await db.execute(update(Agent).where(Agent.id == agent.id).values(user_id=other.id))
            await db.commit()

        outcome = await runner.execute_scheduled_run(str(run_id), attempt=1)

        assert outcome.kind == "blocked" and tool.calls == []
        item = await _item(scratch.sessions, item_id)
        assert item.outcome_code == swi.OUTCOME_PERMISSION


@pytest.mark.asyncio
async def test_an_unreadable_ai_state_is_retried_without_running(allowed, monkeypatch):
    tool = _RecordingTool()
    _with_tool(monkeypatch, tool)

    async def _transient(_db):
        return AgenticVerdict("ai_disabled", transient=True)

    monkeypatch.setattr("app.services.agentic_gate.agentic_verdict", _transient)
    async with scratch_database("swi", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        user, agent, conversation = await _seed(scratch.sessions)
        item_id = await _schedule(user, agent, conversation)
        await _tick(scratch.sessions)
        run_id = (await _item(scratch.sessions, item_id)).run_id

        outcome = await runner.execute_scheduled_run(str(run_id), attempt=1)

        assert outcome.kind == "retry" and tool.calls == []
        assert (await _item(scratch.sessions, item_id)).status == "claimed"
        assert (await _run(scratch.sessions, run_id)).status == "pending"


@pytest.mark.asyncio
async def test_a_re_run_after_a_lost_attempt_never_sends_twice_and_is_run_again_for_its_agent(allowed, monkeypatch):
    # A call that may change something is never sent again blind, and
    # the restart is never the person's: its agent gets a fresh run that reads
    # the target's state first.
    tool = _RecordingTool()
    _with_tool(monkeypatch, tool)

    async def _not_read_only(_db, _tool):
        return False

    monkeypatch.setattr(runner, "_is_read_only_tool", _not_read_only)
    async with scratch_database("swi", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        user, agent, conversation = await _seed(scratch.sessions)
        item_id = await _schedule(user, agent, conversation)
        await _tick(scratch.sessions)
        run_id = (await _item(scratch.sessions, item_id)).run_id
        async with scratch.sessions() as db:
            run = await db.get(AssignmentRun, run_id)
            run.trigger_context = {
                **(run.trigger_context or {}),
                "action_started_at": datetime.now(UTC).isoformat(),
                "attempt": 1,
            }
            run.status = "running"
            await db.commit()

        outcome = await runner.execute_scheduled_run(str(run_id), attempt=2)

        assert outcome.kind == runner.REQUEUED and tool.calls == []
        item = await _item(scratch.sessions, item_id)
        assert item.status == "claimed" and item.outcome_code is None
        assert item.run_id != run_id
        assert swi.restarts_of(item.payload) == {"resumed": 0, "requeued": 1}
        fresh = await _run(scratch.sessions, item.run_id)
        prompt = fresh.trigger_context[runner.OWNER_PROMPT_KEY]
        assert "First read the target's state" in prompt and "send_teams_channel_message" in prompt
        assert (await _run(scratch.sessions, run_id)).status == "failed"


@pytest.mark.asyncio
async def test_the_kill_switch_stops_dispatch(allowed, monkeypatch):
    async with scratch_database("swi", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        user, agent, conversation = await _seed(scratch.sessions)
        item_id = await _schedule(user, agent, conversation)
        monkeypatch.setattr(swi, "scheduling_enabled", lambda: False)
        assert await _tick(scratch.sessions) == 0
        assert (await _item(scratch.sessions, item_id)).status == "open"
