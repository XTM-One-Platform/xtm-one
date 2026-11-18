"""Version history of an agent written through the agent builder, on a real PostgreSQL.

``create_agent`` and ``update_agent`` (the Copilot chat and the platform MCP
server) wrote no version, so an agent built that way had an empty Versioning
tab and its first recorded edit lost what it replaced. Here the tools run on a
real session and the version rows are read back, with the other ways of
creating an agent that share the seeding (``clone_agent``, the REST duplicate
and import) and the one baseline rule.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import Request
from sqlalchemy import select

import app.services.builtin_tools as builtin_tools_module
from app.models.agent import Agent
from app.models.agent_config_version import AgentConfigVersion
from app.models.agent_template import AgentTemplate
from app.models.audit_log import AuditLog
from app.models.prompt_version import PromptVersion
from app.models.user import User
from app.services.agent_config_versioning import (
    BASELINE_COMMIT_MESSAGE,
    INITIAL_COMMIT_MESSAGE,
    record_agent_version_changes,
    versioned_agent_values,
)
from app.services.builtin_tools.agent_builder import BUILDER_EDIT_COMMIT_MESSAGE
from app.tools.base import ToolCall
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration


async def _user(sessionmaker) -> User:
    async with sessionmaker() as session:
        user = User(
            id=uuid.uuid4(),
            email=f"versions-{uuid.uuid4().hex[:8]}@test.local",
            full_name="Versions Test",
            hashed_password="x",
        )
        session.add(user)
        await session.commit()
        return user


async def _agent(sessionmaker, user: User, **fields) -> uuid.UUID:
    async with sessionmaker() as session:
        agent = Agent(id=uuid.uuid4(), name="Weekly builder", user_id=user.id, **fields)
        session.add(agent)
        await session.commit()
        return agent.id


async def _builder(user_id: uuid.UUID, name: str, arguments: dict) -> str:
    _, executor = builtin_tools_module.build_agent_builder_tool(None, str(user_id))
    result = await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments)
    )
    return result.content


async def _only_agent_id(sessionmaker, user: User) -> uuid.UUID:
    async with sessionmaker() as session:
        return (
            await session.execute(select(Agent.id).where(Agent.user_id == user.id))
        ).scalar_one()


async def _persona_versions(sessionmaker, agent_id) -> list[tuple[int, str, str | None, bool]]:
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(PromptVersion)
                .where(PromptVersion.agent_id == agent_id)
                .order_by(PromptVersion.version_number)
            )
        ).scalars().all()
    return [(r.version_number, r.content, r.commit_message, r.author_id is not None) for r in rows]


async def _config_versions(sessionmaker, agent_id, field_name) -> list[tuple[int, object, str | None]]:
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(AgentConfigVersion)
                .where(
                    AgentConfigVersion.agent_id == agent_id,
                    AgentConfigVersion.field_name == field_name,
                )
                .order_by(AgentConfigVersion.version_number)
            )
        ).scalars().all()
    return [(r.version_number, r.value_json, r.commit_message) for r in rows]


@pytest.mark.asyncio
async def test_create_agent_seeds_the_initial_version():
    async with _temp_database("abv_create") as sessionmaker:
        user = await _user(sessionmaker)

        content = await _builder(
            user.id,
            "create_agent",
            {"name": "Weekly builder", "persona": "You build groupings.", "max_iterations": 40},
        )

        agent_id = await _only_agent_id(sessionmaker, user)
        assert "Weekly builder" in content
        assert await _persona_versions(sessionmaker, agent_id) == [
            (1, "You build groupings.", "Initial version", True)
        ]
        assert await _config_versions(sessionmaker, agent_id, "max_iterations") == [
            (1, 40, "Initial version")
        ]


@pytest.mark.asyncio
async def test_update_agent_versions_what_it_changes_and_keeps_what_it_replaced():
    """An agent with no history (built before the fix): its first builder edit
    records the replaced persona as a baseline, then the new one."""
    async with _temp_database("abv_update") as sessionmaker:
        user = await _user(sessionmaker)
        agent_id = await _agent(sessionmaker, user, persona="First prompt.", max_iterations=20)

        content = await _builder(
            user.id,
            "update_agent",
            {"agent_id": str(agent_id), "persona": "Second prompt.", "max_iterations": "100"},
        )
        assert "updated:" in content
        await _builder(
            user.id, "update_agent", {"agent_id": str(agent_id), "persona": "Third prompt."}
        )

        assert await _persona_versions(sessionmaker, agent_id) == [
            (1, "First prompt.", BASELINE_COMMIT_MESSAGE, False),
            (2, "Second prompt.", BUILDER_EDIT_COMMIT_MESSAGE, True),
            (3, "Third prompt.", BUILDER_EDIT_COMMIT_MESSAGE, True),
        ]
        assert await _config_versions(sessionmaker, agent_id, "max_iterations") == [
            (1, 20, BASELINE_COMMIT_MESSAGE),
            (2, 100, BUILDER_EDIT_COMMIT_MESSAGE),
        ]


@pytest.mark.asyncio
async def test_an_unchanged_or_unwritten_value_is_not_versioned():
    async with _temp_database("abv_noop") as sessionmaker:
        user = await _user(sessionmaker)
        agent_id = await _agent(sessionmaker, user, persona="Same.", max_iterations=20)

        async with sessionmaker() as session:
            agent = await session.get(Agent, agent_id)
            before = versioned_agent_values(agent)
            agent.max_iterations = 25  # changed, but not by this write
            persona_version, config_versions = await record_agent_version_changes(
                session,
                agent=agent,
                before=before,
                written={"persona", "name"},
                author_id=user.id,
            )
            await session.commit()

        assert (persona_version, config_versions) == (None, {})
        assert await _persona_versions(sessionmaker, agent_id) == []
        assert await _config_versions(sessionmaker, agent_id, "max_iterations") == []


@pytest.mark.asyncio
async def test_an_agent_with_history_gets_no_baseline():
    async with _temp_database("abv_history") as sessionmaker:
        user = await _user(sessionmaker)
        await _builder(user.id, "create_agent", {"name": "Weekly builder", "persona": "One."})
        agent_id = await _only_agent_id(sessionmaker, user)

        await _builder(user.id, "update_agent", {"agent_id": str(agent_id), "persona": "Two."})

        assert await _persona_versions(sessionmaker, agent_id) == [
            (1, "One.", "Initial version", True),
            (2, "Two.", BUILDER_EDIT_COMMIT_MESSAGE, True),
        ]


# ── Every tracked field the builder writes, and its audit entry ──────

_TRACKED_EDITS = [
    # field, value at creation, update_agent argument, value stored, clear argument
    ("guardrails", "Stay in scope.", "Never share keys.", "Never share keys.", "none"),
    ("temperature", 0.3, "0.7", 0.7, "default"),
    ("reasoning_effort", "low", "high", "high", "none"),
    ("max_iterations", 20, "100", 100, "default"),
]


async def _agent_audit_details(sessionmaker, agent_id) -> list[dict]:
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.entity_id == agent_id, AuditLog.action == "update")
                .order_by(AuditLog.created_at)
            )
        ).scalars().all()
    return [r.details for r in rows]


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "initial", "argument", "stored", "clear"), _TRACKED_EDITS)
async def test_update_agent_versions_every_tracked_field_and_its_clear(
    field, initial, argument, stored, clear
):
    """Each field is versioned under its own name, whatever label the tool
    prints, and a clear leaves a ``None`` marker as the REST reset does."""
    async with _temp_database(f"abv_field_{field[:6]}") as sessionmaker:
        user = await _user(sessionmaker)
        agent_id = await _agent(sessionmaker, user, **{field: initial})

        await _builder(user.id, "update_agent", {"agent_id": str(agent_id), field: argument})
        await _builder(user.id, "update_agent", {"agent_id": str(agent_id), field: clear})

        assert await _config_versions(sessionmaker, agent_id, field) == [
            (1, initial, BASELINE_COMMIT_MESSAGE),
            (2, stored, BUILDER_EDIT_COMMIT_MESSAGE),
            (3, None, BUILDER_EDIT_COMMIT_MESSAGE),
        ]
        details = await _agent_audit_details(sessionmaker, agent_id)
        # Every edit names the fields it changed (#251), under the tool's labels.
        assert all(d.pop("fields") for d in details)
        assert details == [
            {"slug": None, "config_field_versions": {field: 2}, "via": "agent"},
            {"slug": None, "config_field_versions": {field: 3}, "via": "agent"},
        ]


# ── One baseline rule: an unset value is a state too ─────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "initial", "argument", "stored", "clear"), _TRACKED_EDITS)
async def test_the_first_set_of_an_unset_field_keeps_the_default_to_roll_back_to(
    field, initial, argument, stored, clear
):
    async with _temp_database(f"abv_unset_{field[:6]}") as sessionmaker:
        user = await _user(sessionmaker)
        agent_id = await _agent(sessionmaker, user)

        await _builder(user.id, "update_agent", {"agent_id": str(agent_id), field: argument})

        assert await _config_versions(sessionmaker, agent_id, field) == [
            (1, None, BASELINE_COMMIT_MESSAGE),
            (2, stored, BUILDER_EDIT_COMMIT_MESSAGE),
        ]


@pytest.mark.asyncio
async def test_the_first_persona_of_an_agent_without_one_keeps_the_empty_persona():
    async with _temp_database("abv_unset_persona") as sessionmaker:
        user = await _user(sessionmaker)
        agent_id = await _agent(sessionmaker, user)

        await _builder(user.id, "update_agent", {"agent_id": str(agent_id), "persona": "First."})

        assert await _persona_versions(sessionmaker, agent_id) == [
            (1, "", BASELINE_COMMIT_MESSAGE, False),
            (2, "First.", BUILDER_EDIT_COMMIT_MESSAGE, True),
        ]


# ── Every way of creating an agent seeds the same history ────────────


@pytest.mark.asyncio
async def test_create_agent_labels_what_it_configures_as_the_initial_version():
    """Fields create_agent sets through the update path start the history:
    "Initial version", no baseline of the creation defaults."""
    async with _temp_database("abv_create_cfg") as sessionmaker:
        user = await _user(sessionmaker)

        await _builder(
            user.id,
            "create_agent",
            {
                "name": "Weekly builder",
                "persona": "You build groupings.",
                "guardrails": "Stay in scope.",
                "temperature": "0.4",
            },
        )

        agent_id = await _only_agent_id(sessionmaker, user)
        assert await _persona_versions(sessionmaker, agent_id) == [
            (1, "You build groupings.", INITIAL_COMMIT_MESSAGE, True)
        ]
        assert await _config_versions(sessionmaker, agent_id, "guardrails") == [
            (1, "Stay in scope.", INITIAL_COMMIT_MESSAGE)
        ]
        assert await _config_versions(sessionmaker, agent_id, "temperature") == [
            (1, 0.4, INITIAL_COMMIT_MESSAGE)
        ]
        # The creation is its audit entry; configuring it is not a later edit.
        assert await _agent_audit_details(sessionmaker, agent_id) == []


@pytest.mark.asyncio
async def test_create_agent_from_a_template_seeds_the_template_persona():
    async with _temp_database("abv_template") as sessionmaker:
        user = await _user(sessionmaker)
        async with sessionmaker() as session:
            session.add(
                AgentTemplate(
                    id=uuid.uuid4(),
                    slug="weekly-digest",
                    name="Weekly digest",
                    default_persona="You write the weekly digest.",
                    enabled=True,
                )
            )
            await session.commit()

        await _builder(
            user.id,
            "create_agent",
            {"name": "Weekly builder", "template_slug": "weekly-digest", "create_default_assignments": False},
        )

        agent_id = await _only_agent_id(sessionmaker, user)
        assert await _persona_versions(sessionmaker, agent_id) == [
            (1, "You write the weekly digest.", INITIAL_COMMIT_MESSAGE, True)
        ]


async def _versions_of_new_agent(sessionmaker, agent_id) -> dict:
    return {
        "persona": await _persona_versions(sessionmaker, agent_id),
        "temperature": await _config_versions(sessionmaker, agent_id, "temperature"),
        "max_iterations": await _config_versions(sessionmaker, agent_id, "max_iterations"),
        "guardrails": await _config_versions(sessionmaker, agent_id, "guardrails"),
    }


_SEEDED = {
    "persona": [(1, "You triage alerts.", INITIAL_COMMIT_MESSAGE, True)],
    "temperature": [(1, 0.2, INITIAL_COMMIT_MESSAGE)],
    "max_iterations": [(1, 30, INITIAL_COMMIT_MESSAGE)],
    # Unset on the source: nothing to seed.
    "guardrails": [],
}


@pytest.mark.asyncio
async def test_clone_agent_seeds_the_initial_version():
    async with _temp_database("abv_clone") as sessionmaker:
        user = await _user(sessionmaker)
        source_id = await _agent(
            sessionmaker, user, persona="You triage alerts.", temperature=0.2, max_iterations=30
        )

        content = await _builder(
            user.id, "clone_agent", {"source_agent_id": str(source_id), "name": "Triage copy"}
        )

        async with sessionmaker() as session:
            clone_id = (
                await session.execute(select(Agent.id).where(Agent.name == "Triage copy"))
            ).scalar_one()
        assert "Triage copy" in content
        assert await _versions_of_new_agent(sessionmaker, clone_id) == _SEEDED


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/x", "headers": []})


@pytest.mark.asyncio
async def test_the_rest_duplicate_seeds_the_initial_version():
    from app.api.v1 import agents as agents_api

    async with _temp_database("abv_duplicate") as sessionmaker:
        user = await _user(sessionmaker)
        source_id = await _agent(
            sessionmaker, user, persona="You triage alerts.", temperature=0.2, max_iterations=30
        )

        async with sessionmaker() as session:
            response = await agents_api.duplicate_agent(
                agent_id=str(source_id),
                request=None,
                user=await session.get(User, user.id),
                db=session,
                _agentic_gate=None,
            )

        assert await _versions_of_new_agent(sessionmaker, uuid.UUID(response.id)) == _SEEDED


@pytest.mark.asyncio
async def test_the_rest_import_seeds_the_initial_version():
    from app.api.v1 import agents as agents_api
    from app.api.v1.schemas.agent import ImportAgentRequest

    async with _temp_database("abv_import") as sessionmaker:
        user = await _user(sessionmaker)
        bundle = {
            "version": 3,
            "export_type": "agent",
            "agent": {
                "name": "Imported triage",
                "persona": "You triage alerts.",
                "temperature": 0.2,
                "max_iterations": 30,
            },
        }

        async with sessionmaker() as session:
            await agents_api.import_agent(
                ImportAgentRequest(**bundle), _request(), await session.get(User, user.id), session
            )

        agent_id = await _only_agent_id(sessionmaker, user)
        assert await _versions_of_new_agent(sessionmaker, agent_id) == _SEEDED


# ── The REST update follows the same rule ────────────────────────────


async def _rest_update(sessionmaker, user: User, agent_id: uuid.UUID, **fields) -> dict:
    from app.api.v1 import agents as agents_api
    from app.api.v1.schemas.agent import UpdateAgentRequest

    async with sessionmaker() as session:
        caller = await session.get(User, user.id)
        response = await agents_api.update_agent(
            agent_id=str(agent_id),
            body=UpdateAgentRequest(**fields),
            request=None,
            user=caller,
            db=session,
            _ai_gate=None,
        )
    return response if isinstance(response, dict) else response.model_dump()


@pytest.mark.asyncio
async def test_the_rest_update_records_a_baseline_then_the_change():
    async with _temp_database("abv_rest") as sessionmaker:
        user = await _user(sessionmaker)
        agent_id = await _agent(sessionmaker, user, persona="First prompt.", max_iterations=20)

        await _rest_update(
            sessionmaker, user, agent_id, persona="Second prompt.", commit_message="Tighter scope"
        )
        await _rest_update(sessionmaker, user, agent_id, max_iterations=None)

        assert await _persona_versions(sessionmaker, agent_id) == [
            (1, "First prompt.", BASELINE_COMMIT_MESSAGE, False),
            (2, "Second prompt.", "Tighter scope", True),
        ]
        # An explicit reset to the default is a change, recorded as a null marker.
        assert await _config_versions(sessionmaker, agent_id, "max_iterations") == [
            (1, 20, BASELINE_COMMIT_MESSAGE),
            (2, None, None),
        ]


@pytest.mark.asyncio
async def test_the_rest_update_versions_nothing_it_did_not_change():
    async with _temp_database("abv_rest_noop") as sessionmaker:
        user = await _user(sessionmaker)
        agent_id = await _agent(sessionmaker, user, persona="Same.", max_iterations=20)

        await _rest_update(sessionmaker, user, agent_id, persona="Same.", description="New text")

        assert await _persona_versions(sessionmaker, agent_id) == []
        assert await _config_versions(sessionmaker, agent_id, "max_iterations") == []
