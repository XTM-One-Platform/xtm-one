"""An explicit null on an assignment update, against a real PostgreSQL.

``test_assignment_update_explicit_null.py`` drives the route on a fake session
whose commit cannot fail: it proves the NOT NULL fields are skipped, not that
the database takes what the route writes. Before the fix the same PUT failed in
two ways only a real database shows: ``SET dry_run = NULL`` (and every other
NOT NULL scalar) is refused by the constraint, and ``trigger_config`` - a JSON
column, where SQLAlchemy writes ``None`` as the JSON literal ``null`` - commits
and leaves a row every later read fails to serialize. Same scratch-database
recipe as ``test_provisioning_postgres.py``.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import Request
from sqlalchemy import select

from app.api.v1 import assignments as assignments_api
from app.api.v1.schemas.assignment import UpdateAssignmentRequest
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.user import User
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration

#: A stored value for every NOT NULL field of the update loop, none of them the
#: column default.
_NOT_NULL_VALUES = {
    "name": "Nightly digest",
    "trigger_type": "schedule",
    "trigger_config": {"cron": "0 9 * * 1"},
    "prompt": "Summarise the week.",
    "use_knowledge_bases": False,
    "write_knowledge_base_enabled": True,
    "ai_label": False,
    "notify_on_complete": True,
    "show_in_digest": True,
    "run_as_all_users": True,
    "timeout_seconds": 900,
    "ignore_self_triggered_events": True,
    "retry_on_failure": True,
    "max_retries": 5,
    "dry_run": True,
    "enabled": False,
}

#: A stored value for every nullable field of the update loop.
_NULLABLE_VALUES = {
    "description": "Weekly summary",
    "activation_prompt": "Is there anything new?",
    "activation_check": {"tool": "list_work_items", "arguments": {"open_only": True}, "condition": "non_empty"},
    "delay": "10m",
    "tool_sources": ["github"],
    "integration_bindings": {"github": [str(uuid.uuid4())]},
    "integration_allowed_tools": {"github": ["list_issues"]},
    "builtin_allowed_tools": {"agent_builder": ["list_agents"]},
    "integration_element_bindings": {"github:repository": [{"id": "o/r", "name": "r"}]},
    "integration_element_enforcement": ["github:repository"],
    "read_knowledge_base_ids": [str(uuid.uuid4())],
    "write_knowledge_base_ids": [str(uuid.uuid4())],
    "runner_ids": [str(uuid.uuid4())],
    "ai_label_prompt": "Label it.",
    "human_in_the_loop": True,
    "human_in_the_loop_prompt": "Ask first.",
    "hide_runs": True,
    "manual_trigger_parameters": [{"key": "topic", "label": "Topic", "type": "text"}],
}


def test_the_seed_values_cover_every_loop_field():
    """A field added to the loop is exercised against the database too."""
    assert set(_NOT_NULL_VALUES) == assignments_api._NON_NULLABLE_UPDATE_FIELDS
    assert set(_NULLABLE_VALUES) == (
        set(assignments_api._UPDATE_LOOP_FIELDS) - assignments_api._NON_NULLABLE_UPDATE_FIELDS
    )


async def _seed(sessionmaker, **fields) -> tuple[User, uuid.UUID]:
    async with sessionmaker() as session:
        user = User(
            id=uuid.uuid4(),
            email=f"asg-null-{uuid.uuid4().hex[:8]}@test.local",
            full_name="Explicit Null Test",
            hashed_password="x",
        )
        session.add(user)
        await session.flush()
        agent = Agent(id=uuid.uuid4(), name="Digest agent", user_id=user.id)
        session.add(agent)
        await session.flush()
        assignment = Assignment(
            agent_id=agent.id,
            user_id=user.id,
            **{"name": "Digest", "trigger_type": "manual", "prompt": "Go.", **fields},
        )
        session.add(assignment)
        await session.commit()
        return user, assignment.id


async def _put(sessionmaker, user: User, assignment_id: uuid.UUID, payload: dict) -> None:
    """``PUT /assignments/{id}`` on a real session: the route commits itself."""
    request = Request({"type": "http", "method": "PUT", "path": "/x", "headers": []})
    async with sessionmaker() as db:
        await assignments_api.update_assignment(
            str(assignment_id),
            UpdateAssignmentRequest.model_validate(payload),
            request,
            await db.get(User, user.id),
            db,
        )


async def _stored(sessionmaker, assignment_id: uuid.UUID) -> Assignment:
    async with sessionmaker() as db:
        return (
            await db.execute(select(Assignment).where(Assignment.id == assignment_id))
        ).scalar_one()


@pytest.mark.asyncio
async def test_null_on_every_not_null_field_commits_and_changes_nothing():
    async with _temp_database("asg_null") as sessionmaker:
        user, assignment_id = await _seed(sessionmaker, **_NOT_NULL_VALUES)

        await _put(sessionmaker, user, assignment_id, dict.fromkeys(_NOT_NULL_VALUES))

        stored = await _stored(sessionmaker, assignment_id)
        assert {field: getattr(stored, field) for field in _NOT_NULL_VALUES} == _NOT_NULL_VALUES
        # The row still reads back: a JSON-null trigger_config would not.
        assert assignments_api._to_response(stored, user=user).trigger_config == {
            "cron": "0 9 * * 1"
        }


@pytest.mark.asyncio
async def test_null_still_clears_every_nullable_field():
    async with _temp_database("asg_null") as sessionmaker:
        user, assignment_id = await _seed(sessionmaker, **_NULLABLE_VALUES)

        await _put(sessionmaker, user, assignment_id, dict.fromkeys(_NULLABLE_VALUES))

        stored = await _stored(sessionmaker, assignment_id)
        assert {field: getattr(stored, field) for field in _NULLABLE_VALUES} == dict.fromkeys(
            _NULLABLE_VALUES
        )
        assignments_api._to_response(stored, user=user)


@pytest.mark.asyncio
async def test_a_null_deprecated_write_flag_keeps_the_inherit_all_write_scope():
    """Mapped through the compat normalizer, the null would store ``[]`` (no
    writes) over ``NULL`` (inherit every writable KB of the agent)."""
    async with _temp_database("asg_null") as sessionmaker:
        user, assignment_id = await _seed(sessionmaker, write_knowledge_base_enabled=True)

        await _put(sessionmaker, user, assignment_id, {"write_knowledge_base_enabled": None})

        stored = await _stored(sessionmaker, assignment_id)
        assert stored.write_knowledge_base_ids is None
        assert stored.write_knowledge_base_enabled is True


@pytest.mark.asyncio
async def test_a_null_trigger_leaves_a_legacy_secretless_webhook_alone():
    """A null the loop skipped is not a trigger edit, so no secret is minted (it
    would switch the sender to signature-required); a real edit still mints."""
    async with _temp_database("asg_null") as sessionmaker:
        user, assignment_id = await _seed(
            sessionmaker, trigger_type="webhook", trigger_config={"path": "legacy"}
        )
        # As seeded (a row written before exact event matching).
        seeded = (await _stored(sessionmaker, assignment_id)).trigger_config
        assert "webhook_secret" not in seeded

        await _put(
            sessionmaker, user, assignment_id, {"trigger_type": None, "trigger_config": None}
        )
        assert (await _stored(sessionmaker, assignment_id)).trigger_config == seeded

        await _put(sessionmaker, user, assignment_id, {"trigger_config": {"path": "legacy"}})
        assert (await _stored(sessionmaker, assignment_id)).trigger_config["webhook_secret"]
