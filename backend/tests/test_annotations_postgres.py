"""Annotations against a real PostgreSQL.

``test_annotations_api.py`` pins the SQL the service emits; this pins what that
SQL does: the ``ON CONFLICT`` upsert converges concurrent ratings on one row,
an agent's feedback is the union of chat-message ratings and stored ratings,
the rating of a deleted run disappears from every read at once and is removed
by the sweep, and deleting the agent takes its annotations with it. Same
scratch-database recipe as ``test_provisioning_postgres.py``.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import delete, func, select

import app.core.database as database
from app.models.agent import Agent
from app.models.agent_delegation import AgentDelegation
from app.models.annotation import Annotation
from app.models.assignment import AssignmentRun
from app.models.chat import Conversation, Message, MessageFeedback
from app.models.sub_agent_task import SubAgentTask
from app.models.user import User
from app.services import annotations as annotation_service
from app.services.annotations import AnnotationTarget
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration


async def _seed(sessionmaker) -> dict:
    """One user, one agent, and one of every rateable thing that agent did."""
    async with sessionmaker() as session:
        user = User(
            id=uuid.uuid4(),
            email=f"annot-{uuid.uuid4().hex[:8]}@test.local",
            full_name="Annotation Test",
            hashed_password="x",
        )
        session.add(user)
        await session.flush()
        agent = Agent(id=uuid.uuid4(), name="Rated agent", user_id=user.id)
        session.add(agent)
        await session.flush()
        run = AssignmentRun(agent_id=agent.id, user_id=user.id, status="completed")
        task = SubAgentTask(
            user_id=user.id,
            parent_agent_id=agent.id,
            target_agent_id=agent.id,
            task_description="Summarise the week",
            status="completed",
        )
        delegation = AgentDelegation(
            user_id=user.id,
            parent_agent_id=agent.id,
            target_agent_id=agent.id,
            type="consult",
            description="Which feed?",
            status="completed",
        )
        conversation = Conversation(user_id=user.id, agent_id=agent.id)
        session.add_all([run, task, delegation, conversation])
        await session.flush()
        message = Message(conversation_id=conversation.id, role="assistant", content="Done.")
        session.add(message)
        await session.flush()
        session.add(MessageFeedback(message_id=message.id, user_id=user.id, rating="positive"))
        await session.commit()
        return {
            "user": user,
            "agent": agent,
            "run": run.id,
            "task": task.id,
            "delegation": delegation.id,
            "message": message.id,
        }


def _target(target_type: str, target_id: uuid.UUID, agent_id: uuid.UUID) -> AnnotationTarget:
    return AnnotationTarget(target_type, target_id, agent_id)


async def _annotation_count(sessionmaker) -> int:
    async with sessionmaker() as session:
        return int(await session.scalar(select(func.count()).select_from(Annotation)))


@pytest.mark.asyncio
async def test_rerating_replaces_the_rating_and_concurrent_ratings_converge():
    async with _temp_database("annot_upsert") as sessionmaker:
        seeded = await _seed(sessionmaker)
        user, agent = seeded["user"], seeded["agent"]
        target = _target("assignment_run", seeded["run"], agent.id)

        async with sessionmaker() as session:
            first = await annotation_service.upsert_annotation(session, target, user.id, "positive", None)
        async with sessionmaker() as session:
            second = await annotation_service.upsert_annotation(
                session, target, user.id, "negative", "Wrong numbers"
            )
        assert second.id == first.id
        assert (second.rating, second.comment) == ("negative", "Wrong numbers")

        # A double click: every writer on its own session, all racing the unique key.
        async def _rate(rating: str) -> None:
            async with sessionmaker() as session:
                await annotation_service.upsert_annotation(session, target, user.id, rating, None)

        await asyncio.gather(*(_rate("positive" if n % 2 else "negative") for n in range(6)))

        assert await _annotation_count(sessionmaker) == 1
        async with sessionmaker() as session:
            stored = await annotation_service.get_annotation(session, target, user.id)
            assert stored is not None and stored.id == first.id
            assert await annotation_service.delete_annotation(session, target, user.id) is True
        async with sessionmaker() as session:
            assert await annotation_service.delete_annotation(session, target, user.id) is False
        assert await _annotation_count(sessionmaker) == 0


@pytest.mark.asyncio
async def test_an_agents_feedback_unions_both_storages_and_drops_deleted_targets(monkeypatch):
    async with _temp_database("annot_list") as sessionmaker:
        seeded = await _seed(sessionmaker)
        user, agent = seeded["user"], seeded["agent"]
        async with sessionmaker() as session:
            await annotation_service.upsert_annotation(
                session, _target("assignment_run", seeded["run"], agent.id), user.id, "negative", "50% wrong"
            )
            await annotation_service.upsert_annotation(
                session, _target("sub_agent_task", seeded["task"], agent.id), user.id, "positive", "500 ok"
            )
            await annotation_service.upsert_annotation(
                session, _target("agent_delegation", seeded["delegation"], agent.id), user.id, "positive", None
            )

        async with sessionmaker() as session:
            entries, total = await annotation_service.list_agent_annotations(session, agent.id)
            summary = await annotation_service.summarize_agent_annotations(session, agent.id)
            searched, searched_total = await annotation_service.list_agent_annotations(
                session, agent.id, search="50%"
            )
        assert total == 4
        assert {entry.target_type for entry in entries} == {
            "message",
            "assignment_run",
            "sub_agent_task",
            "agent_delegation",
        }
        message_entry = next(entry for entry in entries if entry.target_type == "message")
        assert message_entry.target_id == seeded["message"]
        assert message_entry.conversation_id is not None
        assert message_entry.user_email == user.email
        assert summary == {
            "message": (1, 0),
            "assignment_run": (0, 1),
            "sub_agent_task": (1, 0),
            "agent_delegation": (1, 0),
            "agent_account_reply": (0, 0),
        }
        # "%" is matched literally: "500 ok" does not contain "50%".
        assert searched_total == 1
        assert searched[0].target_type == "assignment_run"

        # The table's sort reaches the query: negative ratings first, kinds in order, dates both ways.
        async with sessionmaker() as session:
            list_sorted = annotation_service.list_agent_annotations
            by_rating, _ = await list_sorted(session, agent.id, sort="rating", direction="asc")
            by_rating_desc, _ = await list_sorted(session, agent.id, sort="rating", direction="desc")
            by_kind, _ = await list_sorted(session, agent.id, sort="kind", direction="asc")
            by_kind_desc, _ = await list_sorted(session, agent.id, sort="kind", direction="desc")
            newest, _ = await list_sorted(session, agent.id)
            oldest, _ = await list_sorted(session, agent.id, sort="date", direction="asc")
        assert [entry.rating for entry in by_rating] == ["negative"] + ["positive"] * 3
        assert [entry.rating for entry in by_rating_desc] == ["positive"] * 3 + ["negative"]
        kinds = sorted(entry.target_type for entry in by_kind)
        assert [entry.target_type for entry in by_kind] == kinds
        assert [entry.target_type for entry in by_kind_desc] == kinds[::-1]
        assert [entry.updated_at for entry in newest] == sorted((e.updated_at for e in newest), reverse=True)
        assert [entry.updated_at for entry in oldest] == sorted(e.updated_at for e in oldest)

        # Several kinds at once (the table's Rated work filter): OR across both storages.
        async with sessionmaker() as session:
            mixed, mixed_total = await annotation_service.list_agent_annotations(
                session, agent.id, target_type=["message", "sub_agent_task"]
            )
            stored_only, stored_total = await annotation_service.list_agent_annotations(
                session, agent.id, target_type=["assignment_run", "agent_delegation"]
            )
        assert mixed_total == 2
        assert {entry.target_type for entry in mixed} == {"message", "sub_agent_task"}
        assert stored_total == 2
        assert {entry.target_type for entry in stored_only} == {"assignment_run", "agent_delegation"}

        async with sessionmaker() as session:
            await session.execute(delete(AssignmentRun).where(AssignmentRun.id == seeded["run"]))
            await session.commit()

        async with sessionmaker() as session:
            entries, total = await annotation_service.list_agent_annotations(session, agent.id)
            summary = await annotation_service.summarize_agent_annotations(session, agent.id)
        # Hidden at once, before any sweep ran.
        assert total == 3
        assert "assignment_run" not in {entry.target_type for entry in entries}
        assert summary["assignment_run"] == (0, 0)
        assert await _annotation_count(sessionmaker) == 3

        monkeypatch.setattr(database, "async_session", sessionmaker)
        assert await annotation_service.purge_orphan_annotations() == 1
        assert await annotation_service.purge_orphan_annotations() == 0
        assert await _annotation_count(sessionmaker) == 2


@pytest.mark.asyncio
async def test_the_sweep_leaves_a_target_type_it_does_not_know_alone(monkeypatch):
    """A newer replica may have written a type this code has no table for."""
    async with _temp_database("annot_unknown") as sessionmaker:
        seeded = await _seed(sessionmaker)
        async with sessionmaker() as session:
            session.add(
                Annotation(
                    target_type="flow_run_step",
                    target_id=uuid.uuid4(),
                    user_id=seeded["user"].id,
                    agent_id=seeded["agent"].id,
                    rating="positive",
                )
            )
            await session.commit()

        monkeypatch.setattr(database, "async_session", sessionmaker)
        assert await annotation_service.purge_orphan_annotations() == 0
        assert await _annotation_count(sessionmaker) == 1


@pytest.mark.asyncio
async def test_deleting_the_agent_deletes_its_annotations():
    async with _temp_database("annot_cascade") as sessionmaker:
        seeded = await _seed(sessionmaker)
        agent = seeded["agent"]
        async with sessionmaker() as session:
            await annotation_service.upsert_annotation(
                session, _target("sub_agent_task", seeded["task"], agent.id), seeded["user"].id, "positive", None
            )
        assert await _annotation_count(sessionmaker) == 1

        async with sessionmaker() as session:
            await session.execute(delete(Agent).where(Agent.id == agent.id))
            await session.commit()

        assert await _annotation_count(sessionmaker) == 0
