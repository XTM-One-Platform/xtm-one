"""The agent-builder ``update_assignment`` schedule check, against a real PostgreSQL.

``test_agent_builder_tool.py`` drives the tool on a fake session that cannot
roll back, so it can only prove nothing was committed. Here the real session
runs: a refused call that also renames the assignment leaves the stored row
exactly as it was, and an accepted one is read back as saved. Same
scratch-database recipe as ``test_provisioning_postgres.py``.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

import app.services.builtin_tools as builtin_tools_module
from app.models.assignment import Assignment
from app.tools.base import ToolCall
from tests.test_assignment_update_explicit_null_postgres import _seed, _stored
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration


async def _update(user_id: uuid.UUID, arguments: dict) -> str:
    _, executor = builtin_tools_module.build_agent_builder_tool(None, str(user_id))
    result = await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name="update_assignment", arguments=arguments)
    )
    return result.content


@pytest.mark.asyncio
async def test_a_refused_schedule_saves_none_of_the_call():
    async with _temp_database("asg_cron") as sessionmaker:
        user, assignment_id = await _seed(
            sessionmaker, trigger_type="schedule", trigger_config={"cron": "0 9 * * 1"}
        )

        content = await _update(
            user.id,
            {
                "assignment_id": str(assignment_id),
                "name": "Renamed",
                "trigger_config": '{"cron": "*/15 * * 1"}',
            },
        )

        assert "Invalid cron expression '*/15 * * 1'" in content
        assert content.endswith("Nothing was saved.")
        stored = await _stored(sessionmaker, assignment_id)
        assert (stored.name, stored.trigger_type, stored.trigger_config) == (
            "Digest",
            "schedule",
            {"cron": "0 9 * * 1"},
        )


@pytest.mark.asyncio
async def test_a_concurrent_switch_to_schedule_is_seen_before_the_check():
    """Another writer holds the row while switching it to a valid schedule. The
    tool's config-only bad cron must be checked against that committed switch,
    not against the manual snapshot it would have read without the row lock."""
    async with _temp_database("asg_cron") as sessionmaker:
        user, assignment_id = await _seed(sessionmaker)

        async with sessionmaker() as other:
            row = (
                await other.execute(
                    select(Assignment).where(Assignment.id == assignment_id).with_for_update()
                )
            ).scalar_one()
            row.trigger_type = "schedule"
            row.trigger_config = {"cron": "0 9 * * 1"}
            await other.flush()
            tool_call = asyncio.create_task(
                _update(
                    user.id,
                    {"assignment_id": str(assignment_id), "trigger_config": '{"cron": "nope"}'},
                )
            )
            await asyncio.sleep(1)
            assert not tool_call.done()
            await other.commit()

        content = await asyncio.wait_for(tool_call, timeout=30)

        assert "Invalid cron expression 'nope'" in content
        stored = await _stored(sessionmaker, assignment_id)
        assert (stored.trigger_type, stored.trigger_config) == ("schedule", {"cron": "0 9 * * 1"})


@pytest.mark.asyncio
async def test_a_valid_switch_to_schedule_is_saved():
    async with _temp_database("asg_cron") as sessionmaker:
        user, assignment_id = await _seed(sessionmaker)

        content = await _update(
            user.id,
            {
                "assignment_id": str(assignment_id),
                "trigger_type": "schedule",
                "trigger_config": '{"cron": "0 9 * * 1-5"}',
            },
        )

        assert "updated: trigger_type, trigger_config" in content
        stored = await _stored(sessionmaker, assignment_id)
        assert (stored.trigger_type, stored.trigger_config) == ("schedule", {"cron": "0 9 * * 1-5"})
