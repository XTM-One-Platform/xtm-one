"""Every writer of an assignment trigger checks the integration it names, on
PostgreSQL; a duplicate and agent writes check the work-item owners.

The real REST handlers, the agent-builder tools, the import pipeline, the
duplicate and template instantiation run here against real rows, so the
three-tier predicate - its admin short-circuit (the agent tools' under
``AGENT_TOOLS_STRICT_ADMIN_ACCESS`` too), the per-user and group grants and
``access_restricted`` - is PostgreSQL's own answer. Every writer is a
NON-admin unless named otherwise.
Same scratch-database recipe as ``test_binding_additions_postgres.py``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.api.v1 import agent_import
from app.api.v1 import agents as agents_api
from app.api.v1 import assignments as assignments_api
from app.api.v1.schemas.agent import CreateAgentRequest, UpdateAgentRequest
from app.api.v1.schemas.assignment import (
    CreateAssignmentRequest,
    UpdateAssignmentRequest,
)
from app.core.config import settings
from app.models.agent import Agent
from app.models.agent_template import AgentTemplate
from app.models.assignment import EVENT_MATCHING_EXACT, EVENT_MATCHING_KEY, Assignment
from app.models.group import Group, GroupMember
from app.models.integration import Integration
from app.models.user import User
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

#: ``binding_access.TRIGGER_INTEGRATION_REFUSED_DETAIL`` /
#: ``WORK_ITEM_OWNERS_REFUSED_DETAIL``, spelled out so this module also runs
#: against a tree without the rule (``test_trigger_integration_writes.py``
#: pins the constants to these sentences).
TRIGGER_INTEGRATION_REFUSED_DETAIL = (
    "The integration in trigger_config was not found or is not accessible."
)
WORK_ITEM_OWNERS_REFUSED_DETAIL = (
    "One or more work-item owner agents were not found or are not accessible."
)


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "trigsave",
        tables=ALL_TABLES,
        extensions=("vector",),
        bind_app_sessions=False,
    ) as db:
        yield db.sessions


@dataclass
class _World:
    owner: uuid.UUID
    member: uuid.UUID
    admin: uuid.UUID
    member_agent: uuid.UUID
    shared_agent: uuid.UUID
    shared_assignment: uuid.UUID
    ids: dict[str, str]


def _user(name: str, *, role: str = "user") -> User:
    return User(
        email=f"{name}-{uuid.uuid4().hex[:8]}@test.local",
        full_name=name.title(),
        hashed_password="x",
        role=role,
    )


def _trigger(integration_id: str | None = None, **extra) -> dict:
    config = {"provider": "microsoft", "event": "new_email", **extra}
    if integration_id is not None:
        config["integration_id"] = integration_id
    return config


#: Integrations the member can see ("worked on main -> still works").
VISIBLE = ("int_member", "int_group", "int_group_restricted", "int_company")
#: Integrations the member cannot see.
HIDDEN = ("int_owner", "int_hidden", "int_restricted_company")


async def _seed(sessions) -> _World:
    async with sessions() as session:
        owner, member, colleague, admin = (
            _user("owner"),
            _user("member"),
            _user("colleague"),
            _user("admin", role="admin"),
        )
        session.add_all([owner, member, colleague, admin])
        await session.flush()
        analysts = Group(name=f"analysts-{uuid.uuid4().hex[:8]}")
        others = Group(name=f"others-{uuid.uuid4().hex[:8]}")
        session.add_all([analysts, others])
        await session.flush()
        session.add_all(
            [
                GroupMember(group_id=analysts.id, user_id=member.id),
                GroupMember(group_id=others.id, user_id=colleague.id),
            ]
        )

        def _integration(name, **kw):
            return Integration(provider="microsoft", name=name, **kw)

        rows = {
            "int_owner": _integration("Owner mailbox", user_id=owner.id),
            "int_member": _integration("Member mailbox", user_id=member.id),
            "int_group": _integration(
                "Team mailbox", user_id=colleague.id, shared_group_ids=[str(analysts.id)]
            ),
            "int_group_restricted": _integration(
                "Restricted team mailbox",
                user_id=colleague.id,
                shared_group_ids=[str(analysts.id)],
                access_restricted=True,
            ),
            "int_company": _integration("Company mailbox", user_id=None, is_company_managed=True),
            "int_hidden": _integration("Colleague mailbox", user_id=colleague.id),
            "int_restricted_company": _integration(
                "Board mailbox", user_id=None, is_company_managed=True, access_restricted=True
            ),
            "peer_owner": Agent(name="Owner peer", user_id=owner.id),
            "peer_member": Agent(name="Member peer", user_id=member.id),
            "peer_company": Agent(name="Company peer", user_id=None, is_company_managed=True),
            "peer_colleague": Agent(name="Colleague peer", user_id=colleague.id),
            "peer_colleague_2": Agent(name="Colleague peer 2", user_id=colleague.id),
        }
        session.add_all(rows.values())
        await session.flush()
        ids = {key: str(row.id) for key, row in rows.items()}

        member_agent = Agent(name="Member agent", user_id=member.id)
        shared = Agent(
            name="Shared mail desk",
            user_id=owner.id,
            shared_group_ids=[str(analysts.id)],
            shared_group_permissions={str(analysts.id): "config"},
            builtin_tool_ids=["work_items"],
            work_item_owner_agent_ids=[
                ids["peer_owner"],
                ids["peer_member"],
                ids["peer_company"],
                ids["peer_colleague"],
            ],
        )
        session.add_all([member_agent, shared])
        await session.flush()
        shared_wf = Assignment(
            agent_id=shared.id,
            user_id=owner.id,
            name="Owner mailbox triage",
            trigger_type="integration_event",
            trigger_config=_trigger(ids["int_owner"]),
            prompt="Triage",
        )
        session.add(shared_wf)
        await session.commit()
        return _World(owner.id, member.id, admin.id, member_agent.id, shared.id, shared_wf.id, ids)


async def _create_assignment(sessions, user_id, agent_id, trigger_config):
    async with sessions() as session:
        user = await session.get(User, user_id)
        response = await assignments_api.create_assignment(
            body=CreateAssignmentRequest(
                agent_id=str(agent_id),
                name="Mail triage",
                trigger_type="integration_event",
                trigger_config=trigger_config,
                prompt="Triage",
            ),
            request=None,
            user=user,
            db=session,
            _agentic_gate=None,
        )
        await session.commit()
        return response


async def _update_assignment(sessions, user_id, assignment_id, **fields):
    async with sessions() as session:
        user = await session.get(User, user_id)
        return await assignments_api.update_assignment(
            assignment_id=str(assignment_id),
            body=UpdateAssignmentRequest(**fields),
            request=None,
            user=user,
            db=session,
            _ai_gate=None,
        )


async def _refused(call) -> HTTPException:
    with pytest.raises(HTTPException) as exc:
        await call
    assert exc.value.status_code == 400
    return exc.value


# ---------------------------------------------------------------------------
# REST create / update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_non_admin_cannot_name_an_integration_they_cannot_see():
    async with _temp_database() as sessions:
        w = await _seed(sessions)
        refusals = [
            await _refused(
                _create_assignment(sessions, w.member, w.member_agent, _trigger(w.ids[key]))
            )
            for key in HIDDEN
        ]
        refusals.append(
            await _refused(
                _create_assignment(sessions, w.member, w.member_agent, _trigger(str(uuid.uuid4())))
            )
        )
        refusals.append(
            await _refused(
                _create_assignment(sessions, w.member, w.member_agent, _trigger("not-a-uuid"))
            )
        )
        changed = await _refused(
            _update_assignment(
                sessions,
                w.member,
                w.shared_assignment,
                trigger_config=_trigger(w.ids["int_hidden"]),
            )
        )
        async with sessions() as session:
            stored = await session.get(Assignment, w.shared_assignment)

    # Hidden answers exactly like missing, and names nothing.
    for exc in [*refusals, changed]:
        assert exc.detail == TRIGGER_INTEGRATION_REFUSED_DETAIL
        assert not any(value in exc.detail for value in w.ids.values())
    assert stored.trigger_config == _trigger(w.ids["int_owner"])


@pytest.mark.asyncio
async def test_worked_on_main_every_integration_the_writer_sees_saves():
    async with _temp_database() as sessions:
        w = await _seed(sessions)
        created = {
            key: await _create_assignment(sessions, w.member, w.member_agent, _trigger(w.ids[key]))
            for key in VISIBLE
        }
        unpinned = await _create_assignment(sessions, w.member, w.member_agent, _trigger())
        admin_hidden = await _create_assignment(
            sessions, w.admin, w.member_agent, _trigger(w.ids["int_hidden"])
        )

    for key, response in created.items():
        assert response.trigger_config["integration_id"] == w.ids[key]
    assert "integration_id" not in unpinned.trigger_config
    # Admins may name any integration, as they may bind any.
    assert admin_hidden.trigger_config["integration_id"] == w.ids["int_hidden"]


@pytest.mark.asyncio
async def test_worked_on_main_a_member_edit_keeps_the_stored_trigger_integration():
    """The member cannot see the owner's mailbox the shared assignment reads:
    renaming it, re-sending its trigger (in any spelling) or adding a filter
    still saves; switching to one they can see or removing it saves too."""
    async with _temp_database() as sessions:
        w = await _seed(sessions)
        stored = w.ids["int_owner"]
        renamed = await _update_assignment(sessions, w.member, w.shared_assignment, name="Renamed")
        resent = await _update_assignment(
            sessions,
            w.member,
            w.shared_assignment,
            trigger_config=_trigger(stored.upper(), filter={"is_flagged": True}),
        )
        switched = await _update_assignment(
            sessions, w.member, w.shared_assignment, trigger_config=_trigger(w.ids["int_group"])
        )
        removed = await _update_assignment(
            sessions, w.member, w.shared_assignment, trigger_config=_trigger()
        )

    assert renamed.name == "Renamed"
    assert renamed.trigger_config["integration_id"] == stored
    assert resent.trigger_config["integration_id"] == stored.upper()
    assert switched.trigger_config["integration_id"] == w.ids["int_group"]
    assert "integration_id" not in removed.trigger_config


# ---------------------------------------------------------------------------
# The agent-builder tools (real gates, real session)
# ---------------------------------------------------------------------------


async def _builder_call(monkeypatch, sessions, user_id, tool, **arguments):
    import app.core.database as database
    import app.services.builtin_tools as builtin_tools_module

    monkeypatch.setattr(database, "async_session", sessions)
    _, executor = builtin_tools_module.build_agent_builder_tool(SimpleNamespace(), str(user_id))
    return await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name=tool, arguments=arguments)
    )


@pytest.mark.asyncio
async def test_the_builder_tools_apply_the_rest_rule(monkeypatch):
    async with _temp_database() as sessions:
        w = await _seed(sessions)
        refused = await _builder_call(
            monkeypatch,
            sessions,
            w.member,
            "create_assignment",
            agent_id=str(w.member_agent),
            name="Hidden",
            prompt="Triage",
            trigger_type="integration_event",
            trigger_config=json.dumps(_trigger(w.ids["int_hidden"])),
        )
        accepted = await _builder_call(
            monkeypatch,
            sessions,
            w.member,
            "create_assignment",
            agent_id=str(w.member_agent),
            name="Team",
            prompt="Triage",
            trigger_type="integration_event",
            trigger_config=json.dumps(_trigger(w.ids["int_group"])),
        )
        kept = await _builder_call(
            monkeypatch,
            sessions,
            w.member,
            "update_assignment",
            assignment_id=str(w.shared_assignment),
            trigger_config=json.dumps(_trigger(w.ids["int_owner"], filter={"x": 1})),
        )
        changed = await _builder_call(
            monkeypatch,
            sessions,
            w.member,
            "update_assignment",
            assignment_id=str(w.shared_assignment),
            trigger_config=json.dumps(_trigger(w.ids["int_restricted_company"])),
        )
        async with sessions() as session:
            names = set(
                (
                    await session.execute(
                        select(Assignment.name).where(Assignment.agent_id == w.member_agent)
                    )
                ).scalars()
            )
            stored = await session.get(Assignment, w.shared_assignment)

    assert TRIGGER_INTEGRATION_REFUSED_DETAIL in str(refused.content)
    assert not accepted.is_error, accepted.content
    assert names == {"Team"}
    assert "Nothing was saved" not in str(kept.content), kept.content
    assert TRIGGER_INTEGRATION_REFUSED_DETAIL in str(changed.content)
    assert stored.trigger_config == _trigger(w.ids["int_owner"], filter={"x": 1})


async def _seed_admin_agent(sessions, w: _World) -> tuple[uuid.UUID, uuid.UUID]:
    async with sessions() as session:
        agent = Agent(name="Admin desk", user_id=w.admin)
        session.add(agent)
        await session.flush()
        wf = Assignment(
            agent_id=agent.id,
            user_id=w.admin,
            name="Company mailbox triage",
            trigger_type="integration_event",
            trigger_config=_trigger(w.ids["int_company"]),
            prompt="Triage",
        )
        session.add(wf)
        await session.commit()
        return agent.id, wf.id


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True], ids=["default", "strict"])
async def test_an_admins_builder_writes_take_the_agent_tools_admin_bypass(monkeypatch, strict):
    """``AGENT_TOOLS_STRICT_ADMIN_ACCESS`` holds an admin's agent to the
    admin's three tiers; a REST admin write names any integration in
    both modes."""
    monkeypatch.setattr(settings, "agent_tools_strict_admin_access", strict)
    async with _temp_database() as sessions:
        w = await _seed(sessions)
        admin_agent, admin_wf = await _seed_admin_agent(sessions, w)
        hidden = _trigger(w.ids["int_hidden"])
        created = await _builder_call(
            monkeypatch,
            sessions,
            w.admin,
            "create_assignment",
            agent_id=str(admin_agent),
            name="Colleague mailbox",
            prompt="Triage",
            trigger_type="integration_event",
            trigger_config=json.dumps(hidden),
        )
        updated = await _builder_call(
            monkeypatch,
            sessions,
            w.admin,
            "update_assignment",
            assignment_id=str(admin_wf),
            trigger_config=json.dumps(hidden),
        )
        rest = await _create_assignment(sessions, w.admin, admin_agent, hidden)
        async with sessions() as session:
            names = set(
                (
                    await session.execute(
                        select(Assignment.name).where(Assignment.agent_id == admin_agent)
                    )
                ).scalars()
            )
            stored = await session.get(Assignment, admin_wf)

    assert rest.trigger_config["integration_id"] == w.ids["int_hidden"]
    if strict:
        assert TRIGGER_INTEGRATION_REFUSED_DETAIL in str(created.content)
        assert TRIGGER_INTEGRATION_REFUSED_DETAIL in str(updated.content)
        assert names == {"Company mailbox triage", "Mail triage"}
        assert stored.trigger_config == _trigger(w.ids["int_company"])
    else:
        assert not created.is_error, created.content
        assert "Nothing was saved" not in str(updated.content), updated.content
        assert names == {"Company mailbox triage", "Colleague mailbox", "Mail triage"}
        assert stored.trigger_config == hidden


# ---------------------------------------------------------------------------
# Template instantiation: a template trigger keeps only what its user sees
# ---------------------------------------------------------------------------


#: The template's default assignments naming an integration of the world.
TEMPLATE_TRIGGERS = {
    "Own": "int_member",
    "Team": "int_group",
    "Company": "int_company",
    "Hidden": "int_hidden",
}
UNPINNED = {"provider": "microsoft", "event": "new_email"}


def _created(config: dict) -> dict:
    return {**config, EVENT_MATCHING_KEY: EVENT_MATCHING_EXACT}


def _assert_template_triggers(triggers: dict[str, dict], ids: dict[str, str], kept: set[str]):
    for name, key in TEMPLATE_TRIGGERS.items():
        if name in kept:
            assert triggers[name] == _created(_trigger(ids[key])), name
        else:
            assert triggers[name] == _created(UNPINNED), name
    # An id naming no integration is left out for everyone; no id is untouched.
    assert triggers["Elsewhere"] == _created(UNPINNED)
    assert triggers["Unpinned"] == _created(UNPINNED)


async def _seed_template(sessions, ids: dict[str, str]) -> str:
    def _wf(name, integration_id=None):
        return {
            "name": name,
            "prompt": "Triage",
            "trigger_type": "integration_event",
            "trigger_config": _trigger(integration_id),
        }

    slug = f"desk-{uuid.uuid4().hex[:8]}"
    async with sessions() as session:
        session.add(
            AgentTemplate(
                slug=slug,
                name="Mail desk",
                default_assignments=[
                    _wf(name, ids[key]) for name, key in TEMPLATE_TRIGGERS.items()
                ]
                + [_wf("Elsewhere", str(uuid.uuid4())), _wf("Unpinned")],
            )
        )
        await session.commit()
    return slug


async def _template_triggers(sessions, agent_id) -> dict[str, dict]:
    async with sessions() as session:
        rows = await session.execute(select(Assignment).where(Assignment.agent_id == agent_id))
        return {wf.name: wf.trigger_config for wf in rows.scalars().all()}


async def _rest_create_from_template(sessions, user_id, slug) -> dict[str, dict]:
    async with sessions() as session:
        user = await session.get(User, user_id)
        response = await agents_api.create_agent(
            body=CreateAgentRequest(name=f"Desk {uuid.uuid4().hex[:6]}", template_slug=slug),
            request=None,
            user=user,
            db=session,
            _agentic_gate=None,
        )
        await session.commit()
    return await _template_triggers(sessions, uuid.UUID(response.id))


@pytest.mark.asyncio
async def test_a_rest_agent_from_a_template_keeps_only_the_trigger_integrations_its_user_sees():
    async with _temp_database() as sessions:
        w = await _seed(sessions)
        slug = await _seed_template(sessions, w.ids)
        member = await _rest_create_from_template(sessions, w.member, slug)
        admin = await _rest_create_from_template(sessions, w.admin, slug)

    _assert_template_triggers(member, w.ids, {"Own", "Team", "Company"})
    # An admin keeps every integration that exists, as on a REST trigger write.
    _assert_template_triggers(admin, w.ids, {"Own", "Team", "Company", "Hidden"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("who", "strict", "kept"),
    [
        ("member", False, {"Own", "Team", "Company"}),
        ("admin", False, {"Own", "Team", "Company", "Hidden"}),
        ("admin", True, {"Company"}),
    ],
    ids=["member", "admin-default", "admin-strict"],
)
async def test_a_builder_agent_from_a_template_keeps_only_what_its_caller_sees(
    monkeypatch, who, strict, kept
):
    monkeypatch.setattr(settings, "agent_tools_strict_admin_access", strict)
    async with _temp_database() as sessions:
        w = await _seed(sessions)
        slug = await _seed_template(sessions, w.ids)
        result = await _builder_call(
            monkeypatch,
            sessions,
            getattr(w, who),
            "create_agent",
            name=f"Desk {uuid.uuid4().hex[:6]}",
            template_slug=slug,
        )
        async with sessions() as session:
            agent_id = (
                await session.execute(
                    select(Agent.id).where(Agent.template_slug == slug)
                )
            ).scalar_one()
        triggers = await _template_triggers(sessions, agent_id)

    assert not result.is_error, result.content
    _assert_template_triggers(triggers, w.ids, kept)
    left_out = len(TEMPLATE_TRIGGERS) - len(kept) + 1
    assert f"{left_out} assignment trigger integration" in str(result.content)
    assert not any(value in str(result.content) for value in w.ids.values())


# ---------------------------------------------------------------------------
# Import: the integration is left out of the trigger, with a warning
# ---------------------------------------------------------------------------


def _bundle(ids: dict[str, str]) -> dict:
    def _wf(name, integration_id=None):
        return {
            "name": name,
            "prompt": "Triage",
            "trigger_type": "integration_event",
            "trigger_config": _trigger(integration_id),
        }

    return {
        "version": 3,
        "export_type": "agent",
        "agent": {"name": "Imported desk"},
        "assignments": [
            _wf("Own", ids["int_member"]),
            _wf("Team", ids["int_group"]),
            _wf("Company", ids["int_company"]),
            _wf("Hidden", ids["int_hidden"]),
            _wf("Restricted", ids["int_restricted_company"]),
            _wf("Elsewhere", str(uuid.uuid4())),
            _wf("Unpinned"),
        ],
    }


async def _import_as(sessions, user_id, bundle):
    async with sessions() as session:
        user = await session.get(User, user_id)
        imported = await agent_import.create_agent_from_bundle(session, user=user, bundle=bundle)
        await session.commit()
    async with sessions() as session:
        rows = await session.execute(
            select(Assignment).where(Assignment.agent_id == imported.agent.id)
        )
        triggers = {wf.name: wf.trigger_config for wf in rows.scalars().all()}
    return imported, triggers


@pytest.mark.asyncio
async def test_an_import_keeps_only_the_trigger_integrations_the_importer_sees():
    async with _temp_database() as sessions:
        w = await _seed(sessions)
        imported, triggers = await _import_as(sessions, w.member, _bundle(w.ids))
        admin_import, admin_triggers = await _import_as(sessions, w.admin, _bundle(w.ids))

    kept = {"Own": "int_member", "Team": "int_group", "Company": "int_company"}
    for name, key in kept.items():
        assert triggers[name]["integration_id"] == w.ids[key]
    for name in ("Hidden", "Restricted", "Elsewhere", "Unpinned"):
        assert "integration_id" not in triggers[name]
        assert triggers[name]["provider"] == "microsoft"
    warned = [warning for warning in imported.warnings if "trigger integration" in warning]
    assert len(warned) == 3
    for name in ("Hidden", "Restricted", "Elsewhere"):
        assert any(f"'{name}'" in warning for warning in warned)
    # An admin keeps every integration that exists here, as a trigger write
    # and the MCP servers of the same file do; an id naming nothing is
    # still dropped.
    admin_kept = {
        "Own": "int_member",
        "Team": "int_group",
        "Company": "int_company",
        "Hidden": "int_hidden",
        "Restricted": "int_restricted_company",
    }
    for name, key in admin_kept.items():
        assert admin_triggers[name]["integration_id"] == w.ids[key]
    assert "integration_id" not in admin_triggers["Elsewhere"]
    admin_warned = [w_ for w_ in admin_import.warnings if "trigger integration" in w_]
    assert len(admin_warned) == 1 and "'Elsewhere'" in admin_warned[0]


# ---------------------------------------------------------------------------
# Duplicate: trigger integration and work-item owners
# ---------------------------------------------------------------------------


async def _duplicate(sessions, user_id, agent_id):
    async with sessions() as session:
        user = await session.get(User, user_id)
        response = await agents_api.duplicate_agent(
            agent_id=str(agent_id), request=None, user=user, db=session, _agentic_gate=None
        )
    async with sessions() as session:
        copy = await session.get(Agent, uuid.UUID(response.id))
        [wf] = (
            await session.execute(select(Assignment).where(Assignment.agent_id == copy.id))
        ).scalars().all()
    return response, copy, wf


@pytest.mark.asyncio
async def test_a_member_copy_leaves_out_the_trigger_integration_and_owners_they_cannot_see():
    async with _temp_database() as sessions:
        w = await _seed(sessions)
        response, copy, wf = await _duplicate(sessions, w.member, w.shared_agent)

    assert copy.work_item_owner_agent_ids == [w.ids["peer_member"], w.ids["peer_company"]]
    assert wf.trigger_config == {"provider": "microsoft", "event": "new_email"}
    assert (
        "2 work-item owner agents of the original agent were not copied: you cannot use them."
        in response.warnings
    )
    assert (
        "1 assignment trigger integration of the original agent was not copied: you cannot use it."
        in response.warnings
    )
    assert not any(value in " ".join(response.warnings) for value in w.ids.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("copier", ["owner", "admin"])
async def test_worked_on_main_the_owner_and_admins_copy_as_before(copier):
    async with _temp_database() as sessions:
        w = await _seed(sessions)
        response, copy, wf = await _duplicate(sessions, getattr(w, copier), w.shared_agent)
        async with sessions() as session:
            source = await session.get(Agent, w.shared_agent)

    assert copy.work_item_owner_agent_ids == source.work_item_owner_agent_ids
    assert wf.trigger_config == _trigger(w.ids["int_owner"])
    assert response.warnings == []


# ---------------------------------------------------------------------------
# Agent create / update: the work-item owner roster (the same rule, extended)
# ---------------------------------------------------------------------------


async def _create_agent(sessions, user_id, owners):
    async with sessions() as session:
        user = await session.get(User, user_id)
        response = await agents_api.create_agent(
            body=CreateAgentRequest(
                name=f"Agent {uuid.uuid4().hex[:6]}", work_item_owner_agent_ids=owners
            ),
            request=None,
            user=user,
            db=session,
            _agentic_gate=None,
        )
        await session.commit()
        return response


async def _update_agent(sessions, user_id, agent_id, **fields):
    async with sessions() as session:
        user = await session.get(User, user_id)
        await agents_api.update_agent(
            agent_id=str(agent_id),
            body=UpdateAgentRequest(**fields),
            request=None,
            user=user,
            db=session,
            _ai_gate=None,
        )
        await session.commit()
    async with sessions() as session:
        return await session.get(Agent, agent_id)


@pytest.mark.asyncio
async def test_agent_writes_add_only_work_item_owners_the_writer_sees():
    async with _temp_database() as sessions:
        w = await _seed(sessions)
        ids = w.ids
        hidden_create = await _refused(_create_agent(sessions, w.member, [ids["peer_colleague"]]))
        missing_create = await _refused(_create_agent(sessions, w.member, [str(uuid.uuid4())]))
        created = await _create_agent(sessions, w.member, [ids["peer_member"], ids["peer_company"]])
        admin_created = await _create_agent(sessions, w.admin, [ids["peer_colleague"]])
        stored = [ids["peer_owner"], ids["peer_member"], ids["peer_company"], ids["peer_colleague"]]
        resaved = await _update_agent(
            sessions,
            w.member,
            w.shared_agent,
            work_item_owner_agent_ids=[s.upper() for s in stored],
        )
        hidden_update = await _refused(
            _update_agent(
                sessions,
                w.member,
                w.shared_agent,
                work_item_owner_agent_ids=[*stored, ids["peer_colleague_2"]],
            )
        )

    for exc in (hidden_create, missing_create, hidden_update):
        assert exc.detail == WORK_ITEM_OWNERS_REFUSED_DETAIL
    assert created.work_item_owner_agent_ids == [ids["peer_member"], ids["peer_company"]]
    assert admin_created.work_item_owner_agent_ids == [ids["peer_colleague"]]
    # Re-saving the stored roster (entries the member cannot see included) saves.
    assert resaved.work_item_owner_agent_ids == [s.upper() for s in stored]
