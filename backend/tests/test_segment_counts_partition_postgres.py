"""Mine + Shared + Company == All on the segment counts, against PostgreSQL.

The visibility filter shows four numbers (``GET <list>/segment-counts``); Mine, Shared and Company must partition All, or a row is counted by
All and found under no segment. Two lists broke it:

* Files: ``mine_files_filter`` was not NULL-guarded. A run's file has no
  ``user_id`` and no ``conversation_id``, so ``user_id = me OR
  conversation_id IN (...)`` was NULL on it, ``NOT mine`` was NULL too, and
  the row fell out of Shared and Company alike;
* Background tasks offered Mine and All only. The tasks an admin sees that are
  not theirs are now split by the agent that delegated them: Company when it
  is company-managed, Shared otherwise. A non-admin still sees only their own.

A real database because what is under test IS the SQL's three-valued logic.
``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours; the temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

import uuid

import pytest

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.v1 import chat as chat_api
from app.api.v1 import files as files_api
from app.models.agent import Agent
from app.models.assignment import AssignmentRun
from app.models.chat import Conversation
from app.models.file_attachment import FileAttachment
from app.models.sub_agent_task import SubAgentTask
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

SEGMENTS = ("mine", "shared", "company")


def _id(n: int) -> uuid.UUID:
    return uuid.UUID(int=0x7633_0000_0000_0000_0000_0000_0000 + n)


ADMIN, OTHER = _id(1), _id(2)
ADMIN_AGENT, OTHER_AGENT, COMPANY_AGENT = _id(10), _id(11), _id(12)
ADMIN_RUN, COMPANY_RUN = _id(20), _id(21)
OPEN_CONVERSATION = _id(30)


def _file(n: int, **columns) -> FileAttachment:
    return FileAttachment(
        id=_id(100 + n),
        filename=f"file-{n}.txt",
        content_type="text/plain",
        size=1,
        s3_object_name=f"test/7633/{n}",
        **columns,
    )


def _task(n: int, user_id: uuid.UUID, parent: uuid.UUID) -> SubAgentTask:
    return SubAgentTask(
        id=_id(200 + n),
        user_id=user_id,
        parent_agent_id=parent,
        target_agent_id=parent,
        task_description=f"task {n}",
        status="completed",
    )


async def _seed(session) -> tuple[User, User]:
    admin = User(id=ADMIN, email="admin-7633@example.com", hashed_password="x", full_name="Admin", role="admin")
    other = User(id=OTHER, email="other-7633@example.com", hashed_password="x", full_name="Other", role="user")
    session.add_all([admin, other])
    await session.flush()
    session.add_all(
        [
            Agent(id=ADMIN_AGENT, user_id=ADMIN, name="Mine", slug="mine-7633", persona="p"),
            Agent(id=OTHER_AGENT, user_id=OTHER, name="Theirs", slug="theirs-7633", persona="p"),
            Agent(
                id=COMPANY_AGENT,
                user_id=OTHER,
                name="Company",
                slug="company-7633",
                persona="p",
                is_company_managed=True,
            ),
        ]
    )
    await session.flush()
    session.add_all(
        [
            AssignmentRun(id=ADMIN_RUN, agent_id=ADMIN_AGENT, user_id=ADMIN, status="completed"),
            AssignmentRun(
                id=COMPANY_RUN, agent_id=COMPANY_AGENT, user_id=OTHER, status="completed", is_company_managed=True
            ),
            Conversation(id=OPEN_CONVERSATION, user_id=OTHER, title="Open", link_access=files_api.LINK_ACCESS_ORG_READ),
        ]
    )
    await session.flush()
    session.add_all(
        [
            # Mine: uploaded by me, and a file my run wrote (no user_id).
            _file(1, user_id=ADMIN),
            _file(2, assignment_run_id=ADMIN_RUN),
            # Company: a company-managed run's file, no user_id, no conversation.
            _file(3, assignment_run_id=COMPANY_RUN),
            # Shared: a file of a colleague's org-wide conversation, no user_id, no run.
            _file(4, conversation_id=OPEN_CONVERSATION),
            # Background tasks: two of mine (one under a company agent), a
            # colleague's under their agent, a colleague's under a company agent.
            _task(1, ADMIN, ADMIN_AGENT),
            _task(2, ADMIN, COMPANY_AGENT),
            _task(3, OTHER, OTHER_AGENT),
            _task(4, OTHER, COMPANY_AGENT),
        ]
    )
    await session.commit()
    return admin, other


def _assert_partition(counts: dict[str, int]) -> None:
    assert sum(counts[segment] for segment in SEGMENTS) == counts["all"], counts


async def _file_counts(session, user: User) -> dict[str, int]:
    counts = await files_api.file_segment_counts(
        search=None, context=None, origin=None, conversation_id=None, assignment_run_id=None, user=user, db=session
    )
    return counts.model_dump()


async def _file_total(session, user: User, segment: str) -> int:
    page = await files_api.list_files(
        limit=1,
        offset=0,
        search=None,
        context=None,
        origin=None,
        ownership=None,
        vis=segment,
        conversation_id=None,
        assignment_run_id=None,
        user=user,
        db=session,
    )
    return page.total


async def _task_counts(session, user: User) -> dict[str, int]:
    counts = await chat_api.sub_agent_task_segment_counts(
        user=user, db=session, agent_id=None, task_status=None, search=None, fleet=None
    )
    return counts.model_dump()


async def _task_total(session, user: User, segment: str) -> int:
    answer = await chat_api.count_sub_agent_tasks(
        user=user, db=session, agent_id=None, task_status=None, scope="mine", vis=segment, search=None, fleet=None
    )
    return answer["count"]


async def test_file_segments_partition_all_with_ownerless_run_and_conversation_files():
    async with scratch_database("segpart", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            admin, _other = await _seed(session)
        async with db.sessions() as session:
            counts = await _file_counts(session, admin)
            assert counts == {"mine": 2, "shared": 1, "company": 1, "all": 4}
            _assert_partition(counts)
            for segment, value in counts.items():
                assert await _file_total(session, admin, segment) == value, segment


async def test_background_task_segments_split_by_the_delegating_agent():
    async with scratch_database("segpart", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            admin, other = await _seed(session)
        async with db.sessions() as session:
            counts = await _task_counts(session, admin)
            assert counts == {"mine": 2, "shared": 1, "company": 1, "all": 4}
            _assert_partition(counts)
            for segment, value in counts.items():
                assert await _task_total(session, admin, segment) == value, segment
            # A non-admin sees their own tasks only: no colleague's task leaks
            # through Shared or Company.
            theirs = await _task_counts(session, other)
            assert theirs == {"mine": 2, "shared": 0, "company": 0, "all": 2}
            _assert_partition(theirs)
            for segment, value in theirs.items():
                assert await _task_total(session, other, segment) == value, segment
