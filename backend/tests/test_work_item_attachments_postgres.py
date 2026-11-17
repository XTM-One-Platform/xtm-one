"""A work item's description, payload and attached file reach the owner's run.

Against a real PostgreSQL (``tests/scratch_database.py``), as NON-admins
(rule 19): an agent in a chat creates an item with a 2,000-character
description, a payload and one file of that chat; the owner's inbox picks it
up; the run it starts carries the whole description and the payload, holds
the file, and the owner's own tools read all three from outside the chat. The
file keeps the chat it belongs to, and nobody who cannot read the item reads
its file. Marked ``integration``: CI's integration job is the one with a
PostgreSQL service.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - every table, for ALL_TABLES
from app.llm.base import FileRef
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.chat import Conversation
from app.models.file_attachment import FileAttachment
from app.models.user import User
from app.services import work_item as wi_service
from app.services import work_item_context
from app.services.builtin_tools import build_generate_file_tool, build_work_item_tools
from app.services.work_item import WorkItemOrigin
from app.tools.base import FileContext, ToolCall, offering_tools
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

DESCRIPTION = ("Review every clause of the attached contract and flag the risky ones. " * 40)[:2000]
PAYLOAD = {"vendor": "Acme", "deadline": "2026-11-15", "clauses": [4, 7]}
CONTRACT = b"# Contract\n\nClause 7: the vendor may terminate with 30 days notice.\n"


@asynccontextmanager
async def _database() -> AsyncIterator:
    async with scratch_database("wiatt", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


def _user(name: str) -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{name}-{uuid.uuid4().hex[:8]}@test.local",
        full_name=name.title(),
        hashed_password="x",
    )


def _agent(owner: User, name: str) -> Agent:
    return Agent(
        id=uuid.uuid4(),
        user_id=owner.id,
        name=name,
        slug=f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}",
        is_company_managed=False,
        builtin_tool_ids=["work_items", "generate_file"],
    )


def _upload(owner: User, conversation: Conversation, name: str) -> FileAttachment:
    return FileAttachment(
        id=uuid.uuid4(),
        user_id=owner.id,
        conversation_id=conversation.id,
        filename=name,
        content_type="text/markdown",
        size=len(CONTRACT),
        s3_object_name=f"chat/{conversation.id}/{uuid.uuid4().hex}-{name}",
        source="chat",
    )


class _World:
    """A member chatting with one of their agents, the agent that owns the work, and someone else."""

    async def create(self, sessions) -> _World:
        self.sessions = sessions
        self.member = _user("member")
        self.other = _user("other")
        self.creator = _agent(self.member, "Contract Desk")
        self.owner = _agent(self.member, "Legal Reviewer")
        self.theirs = _agent(self.other, "Their Agent")
        self.chat = Conversation(id=uuid.uuid4(), user_id=self.member.id, channel="web", title="Acme contract")
        self.their_chat = Conversation(id=uuid.uuid4(), user_id=self.other.id, channel="web", title="Private")
        self.upload = _upload(self.member, self.chat, "contract.md")
        self.their_upload = _upload(self.other, self.their_chat, "their-notes.md")
        self.inbox = Assignment(
            id=uuid.uuid4(),
            agent_id=self.owner.id,
            user_id=self.member.id,
            name="Legal inbox",
            trigger_type="work_item",
            trigger_config={},
            prompt="Work the item you were given.",
            enabled=True,
        )
        async with sessions() as db:
            db.add_all([self.member, self.other])
            await db.flush()
            db.add_all([self.creator, self.owner, self.theirs, self.chat, self.their_chat])
            await db.flush()
            db.add_all([self.upload, self.their_upload, self.inbox])
            await db.commit()
        return self

    def chat_tools(self):
        """The creator's work-item tools in the member's chat, holding its upload."""
        _defs, executor = build_work_item_tools(
            None,
            str(self.member.id),
            str(self.creator.id),
            origin=WorkItemOrigin(conversation_id=self.chat.id),
        )
        executor.file_context = FileContext()
        executor.file_context.add(FileRef.from_attachment(self.upload))
        return executor


async def _call(executor, name: str, **arguments):
    return await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments))


@pytest.fixture(autouse=True)
def _contract_in_storage(monkeypatch):
    from app.services import file_storage

    async def _download(object_name):
        return CONTRACT

    monkeypatch.setattr(file_storage.file_storage, "download", _download)


async def test_the_owners_pickup_run_reads_the_description_the_payload_and_the_file(monkeypatch):
    from app.services import autonomous
    from app.services.assignment_engine import (
        _link_run_output_files,
        load_dispatched_work_item_files,
    )
    from app.services.assignment_helpers import _build_trigger_information

    async with _database() as sessions:
        world = await _World().create(sessions)
        created = await _call(
            world.chat_tools(),
            "create_work_item",
            title="Review the Acme contract",
            description=DESCRIPTION,
            owner_agent_id=str(world.owner.id),
            payload_json=json.dumps(PAYLOAD),
            attachment_file_ids=[str(world.upload.id)],
        )
        assert not created.is_error, created.content
        [item] = await wi_service.list_work_items(
            user_id=str(world.member.id), owner_agent_id=str(world.owner.id)
        )
        assert item["description"] == DESCRIPTION
        assert item["attachment_file_ids"] == [str(world.upload.id)]

        # The owner's inbox picks it up; the run it starts is recorded as is.
        async def _record(*, assignment, agent, payload, trigger_context, **_kwargs):
            async with sessions() as own:
                run = AssignmentRun(
                    id=uuid.uuid4(),
                    assignment_id=assignment.id,
                    agent_id=agent.id,
                    user_id=assignment.user_id,
                    status="pending",
                    trigger_payload=payload,
                    trigger_context=trigger_context,
                )
                own.add(run)
                await own.commit()
            return str(run.id)

        monkeypatch.setattr(autonomous, "trigger_assignment", _record)
        async with sessions() as db:
            assert await autonomous._process_work_item_assignments(db) == 1
        async with sessions() as db:
            run = (
                await db.execute(select(AssignmentRun).where(AssignmentRun.assignment_id == world.inbox.id))
            ).scalar_one()

        # The run's prompt carries the whole description and the payload...
        trigger_information = _build_trigger_information(run)
        assert DESCRIPTION in trigger_information
        assert '"vendor": "Acme"' in trigger_information
        # ...and the item's file is one of its trigger files.
        assert [ref.file_id for ref in await load_dispatched_work_item_files(run)] == [str(world.upload.id)]

        # The owner's own tools, in the run - not in the chat the file belongs to.
        wi_defs, owner_tools = build_work_item_tools(
            None, str(world.member.id), str(world.owner.id), origin=WorkItemOrigin(run_id=run.id)
        )
        gf_defs, file_tools = build_generate_file_tool(
            db=None, user_id=str(world.member.id), conversation_id=None, assignment_run_id=str(run.id)
        )
        run_files = FileContext()
        owner_tools.file_context = run_files
        file_tools.file_context = run_files
        with offering_tools([*wi_defs, *gf_defs]):
            full = await _call(owner_tools, "get_work_item", work_item_id=item["id"])
            read = await _call(file_tools, "read_file_content", file_id=str(world.upload.id))

        assert DESCRIPTION in full.content
        assert '"vendor": "Acme"' in full.content
        assert full.structured["status"] == "claimed"
        assert not read.is_error, read.content
        assert "Clause 7: the vendor may terminate" in read.content

        # The run ends: the chat's file stays the chat's.
        async with sessions() as db:
            run_row = (await db.execute(select(AssignmentRun).where(AssignmentRun.id == run.id))).scalar_one()
            await _link_run_output_files(db, run_row, run_files, [])
            await db.commit()
        async with sessions() as db:
            upload = (
                await db.execute(select(FileAttachment).where(FileAttachment.id == world.upload.id))
            ).scalar_one()
        assert upload.conversation_id == world.chat.id
        assert upload.assignment_run_id is None


async def test_who_may_not_read_the_item_does_not_read_its_file(monkeypatch):
    async def _unknown(item):
        return None

    monkeypatch.setattr(work_item_context, "new_item_pickup_state", _unknown)
    async with _database() as sessions:
        world = await _World().create(sessions)
        created = await _call(
            world.chat_tools(),
            "create_work_item",
            title="Review the Acme contract",
            owner_agent_id=str(world.owner.id),
            attachment_file_ids=[str(world.upload.id)],
        )
        assert not created.is_error, created.content
        [item] = await wi_service.list_work_items(
            user_id=str(world.member.id), owner_agent_id=str(world.owner.id)
        )

        _defs, their_tools = build_work_item_tools(None, str(world.other.id), str(world.theirs.id))
        their_files = FileContext()
        their_tools.file_context = their_files
        answer = await _call(their_tools, "get_work_item", work_item_id=item["id"])

    assert "not found or not accessible" in answer.content
    assert their_files.get(str(world.upload.id)) is None


async def test_a_file_of_another_conversation_cannot_be_attached(monkeypatch):
    async def _unknown(item):
        return None

    monkeypatch.setattr(work_item_context, "new_item_pickup_state", _unknown)
    async with _database() as sessions:
        world = await _World().create(sessions)
        refused = await _call(
            world.chat_tools(),
            "create_work_item",
            title="Smuggle a file",
            owner_agent_id=str(world.owner.id),
            attachment_file_ids=[str(world.their_upload.id)],
        )
        items = await wi_service.list_work_items(user_id=str(world.member.id))

    assert "cannot be attached" in refused.content
    assert items == []


async def test_a_file_of_another_conversation_of_the_same_user_is_attached(monkeypatch):
    """An event run names a file another chat of its user produced: admitted, not copied."""

    async def _unknown(item):
        return None

    monkeypatch.setattr(work_item_context, "new_item_pickup_state", _unknown)
    async with _database() as sessions:
        world = await _World().create(sessions)
        # No conversation or run of its own holds the file: only its user owns it.
        _defs, event_run = build_work_item_tools(None, str(world.member.id), str(world.creator.id))
        event_run.file_context = FileContext()
        created = await _call(
            event_run,
            "create_work_item",
            title="Review against the reference",
            owner_agent_id=str(world.owner.id),
            attachment_file_ids=[f"[[FILE:{world.upload.id}]]"],
        )
        items = await wi_service.list_work_items(user_id=str(world.member.id))
        async with sessions() as db:
            upload = (await db.execute(select(FileAttachment).where(FileAttachment.id == world.upload.id))).scalar_one()

    assert not created.is_error, created.content
    assert [item["attachment_file_ids"] for item in items] == [[str(world.upload.id)]]
    # A reference, never a copy: the row keeps the chat it belongs to.
    assert upload.conversation_id == world.chat.id
