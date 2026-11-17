"""A write may only ADD channel bots and integrations the writer may use.

Agent ``channel_config`` and assignment ``integration_bindings`` /
``channel_config`` overrides name resources a run later uses with their stored
credentials. Only what a write adds is checked against the writer, with the
rules of the agent's own bindings; what the row already holds is left alone.
The three-tier predicate short-circuits for admins and reads the per-user
sharing columns otherwise, which a mock session cannot show: the real REST
handlers run here against PostgreSQL. Same scratch-database recipe as
``test_assignment_stale_reference_postgres.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.api.v1 import agents as agents_api
from app.api.v1 import assignments as assignments_api
from app.api.v1.schemas.agent import CreateAgentRequest, UpdateAgentRequest
from app.api.v1.schemas.assignment import (
    CreateAssignmentRequest,
    UpdateAssignmentRequest,
)
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.channel_bot import ChannelBot
from app.models.group import Group, GroupMember
from app.models.integration import Integration
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "bindadd",
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


#: A company bot whose whitelist lets personal agents use channel C1.
_WHITELIST = [{"id": "C1", "name": "general"}]


def _company_slack(ids: dict[str, str]) -> dict:
    return {"channel_bot_id": ids["bot_company"], "receive_channels": [{"id": "C1"}]}


async def _seed(sessions) -> _World:
    async with sessions() as session:
        owner, member, admin = _user("owner"), _user("member"), _user("admin", role="admin")
        session.add_all([owner, member, admin])
        await session.flush()
        group = Group(name=f"analysts-{uuid.uuid4().hex[:8]}")
        session.add(group)
        await session.flush()
        session.add(GroupMember(group_id=group.id, user_id=member.id))

        rows = {
            "bot_private": ChannelBot(
                provider="teams_bot", name="Owner Teams", user_id=owner.id, is_company_managed=False
            ),
            "bot_private_2": ChannelBot(
                provider="discord_bot", name="Owner Discord", user_id=owner.id, is_company_managed=False
            ),
            "bot_member": ChannelBot(
                provider="discord_bot", name="Member Discord", user_id=member.id, is_company_managed=False
            ),
            "bot_company": ChannelBot(
                provider="slack_bot",
                name="Company Slack",
                user_id=None,
                is_company_managed=True,
                allowed_receive_channels=_WHITELIST,
            ),
            "int_private": Integration(provider="github", name="Owner GitHub", user_id=owner.id),
            "int_private_2": Integration(provider="github", name="Owner GitHub 2", user_id=owner.id),
            "int_private_3": Integration(provider="github", name="Owner GitHub 3", user_id=owner.id),
            "int_member": Integration(provider="github", name="Member GitHub", user_id=member.id),
            "int_company": Integration(
                provider="github", name="Company GitHub", user_id=None, is_company_managed=True
            ),
        }
        session.add_all(rows.values())
        await session.flush()
        ids = {key: str(row.id) for key, row in rows.items()}

        member_agent = Agent(name="Member agent", user_id=member.id)
        shared = Agent(
            name="Shared analyst",
            user_id=owner.id,
            shared_group_ids=[str(group.id)],
            shared_group_permissions={str(group.id): "config"},
            channel_config={"teams": {"channel_bot_id": ids["bot_private"]}},
            integration_bindings={"github": [ids["int_private"]]},
        )
        session.add_all([member_agent, shared])
        await session.flush()
        shared_wf = Assignment(
            agent_id=shared.id,
            user_id=owner.id,
            name="Owner scoped",
            trigger_type="manual",
            prompt="Work",
            integration_bindings={"github": [ids["int_private_2"]]},
            channel_config={"discord": {"channel_bot_id": ids["bot_private_2"]}},
        )
        session.add(shared_wf)
        await session.commit()
        return _World(
            owner.id, member.id, admin.id, member_agent.id, shared.id, shared_wf.id, ids
        )


async def _create_agent(sessions, user_id, channel_config):
    async with sessions() as session:
        user = await session.get(User, user_id)
        response = await agents_api.create_agent(
            body=CreateAgentRequest(name=f"Agent {uuid.uuid4().hex[:6]}", channel_config=channel_config),
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
    async with sessions() as session:
        return await session.get(Agent, agent_id)


async def _create_assignment(sessions, user_id, agent_id, **fields):
    async with sessions() as session:
        user = await session.get(User, user_id)
        response = await assignments_api.create_assignment(
            body=CreateAssignmentRequest(
                agent_id=str(agent_id), name="Scoped", trigger_type="manual", prompt="Work", **fields
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


@pytest.mark.asyncio
async def test_a_non_admin_cannot_add_a_bot_or_integration_they_cannot_see():
    async with _temp_database() as sessions:
        w = await _seed(sessions)
        ids = w.ids

        hidden_bot = await _refused(
            _create_agent(sessions, w.member, {"teams": {"channel_bot_id": ids["bot_private"]}})
        )
        missing_bot = await _refused(
            _create_agent(sessions, w.member, {"teams": {"channel_bot_id": str(uuid.uuid4())}})
        )
        await _refused(
            _update_agent(
                sessions,
                w.member,
                w.member_agent,
                channel_config={"teams": {"channel_bot_id": ids["bot_private"]}},
            )
        )
        hidden_integration = await _refused(
            _create_assignment(
                sessions, w.member, w.member_agent, integration_bindings={"github": [ids["int_private"]]}
            )
        )
        missing_integration = await _refused(
            _create_assignment(
                sessions, w.member, w.member_agent, integration_bindings={"github": [str(uuid.uuid4())]}
            )
        )
        await _refused(
            _create_assignment(
                sessions,
                w.member,
                w.member_agent,
                channel_config={"teams": {"channel_bot_id": ids["bot_private"]}},
            )
        )

    # Hidden answers exactly like missing, and names nothing.
    assert hidden_bot.detail == missing_bot.detail
    assert hidden_integration.detail == missing_integration.detail
    for detail in (hidden_bot.detail, hidden_integration.detail):
        assert "Owner" not in detail
        assert not any(value in detail for value in ids.values())


@pytest.mark.asyncio
async def test_an_override_naming_a_company_integration_restricted_to_others_is_refused():
    """A company-managed integration narrowed with ``access_restricted``
    is one the editor may only bind when a group of theirs is granted it."""
    async with _temp_database() as sessions:
        w = await _seed(sessions)
        async with sessions() as session:
            others = Group(name=f"others-{uuid.uuid4().hex[:8]}")
            session.add(others)
            await session.flush()
            member_groups = [
                str(g) for g in (await session.execute(
                    text("SELECT group_id FROM group_members WHERE user_id = :u"),
                    {"u": w.member},
                )).scalars().all()
            ]
            restricted = Integration(
                provider="confluence", name="Restricted", user_id=None,
                is_company_managed=True, access_restricted=True,
                shared_group_ids=[str(others.id)],
            )
            granted = Integration(
                provider="jira", name="Granted", user_id=None,
                is_company_managed=True, access_restricted=True,
                shared_group_ids=member_groups,
            )
            session.add_all([restricted, granted])
            await session.commit()
            restricted_id, granted_id = str(restricted.id), str(granted.id)

        refused = await _refused(
            _create_assignment(
                sessions, w.member, w.member_agent,
                integration_bindings={"confluence": [restricted_id]},
            )
        )
        await _refused(
            _update_assignment(
                sessions, w.member, w.shared_assignment,
                integration_bindings={
                    "github": [w.ids["int_private_2"]],
                    "confluence": [restricted_id],
                },
            )
        )
        accepted = await _create_assignment(
            sessions, w.member, w.member_agent,
            integration_bindings={"jira": [granted_id]},
        )

    assert restricted_id not in refused.detail
    assert accepted.integration_bindings == {"jira": [granted_id]}


@pytest.mark.asyncio
async def test_own_and_company_managed_bindings_are_accepted():
    async with _temp_database() as sessions:
        w = await _seed(sessions)
        ids = w.ids
        channels = {
            "discord": {"channel_bot_id": ids["bot_member"]},
            "slack": _company_slack(ids),
        }

        created = await _create_agent(sessions, w.member, channels)
        assert set(created.channel_config) == {"discord", "slack"}
        updated = await _update_agent(sessions, w.member, w.member_agent, channel_config=channels)
        assert set(updated.channel_config) == {"discord", "slack"}

        wf = await _create_assignment(
            sessions,
            w.member,
            w.member_agent,
            integration_bindings={"github": [ids["int_member"], ids["int_company"]]},
            channel_config={"discord": {"channel_bot_id": ids["bot_member"]}},
        )
        assert wf.integration_bindings == {"github": [ids["int_member"], ids["int_company"]]}
        # The agent's own binding may scope its assignment, whoever writes it.
        on_shared = await _create_assignment(
            sessions, w.member, w.shared_agent, integration_bindings={"github": [ids["int_private"]]}
        )
        assert on_shared.integration_bindings == {"github": [ids["int_private"]]}


@pytest.mark.asyncio
async def test_a_member_resaving_a_shared_row_keeps_what_was_there():
    async with _temp_database() as sessions:
        w = await _seed(sessions)
        ids = w.ids

        # The shared agent's stored bot stays, a visible one is added alongside.
        agent = await _update_agent(
            sessions,
            w.member,
            w.shared_agent,
            channel_config={
                "teams": {"channel_bot_id": ids["bot_private"]},
                "slack": _company_slack(ids),
            },
        )
        assert agent.channel_config["teams"] == {"channel_bot_id": ids["bot_private"]}

        wf = await _update_assignment(
            sessions,
            w.member,
            w.shared_assignment,
            name="Renamed",
            integration_bindings={"github": [ids["int_private_2"], ids["int_company"]]},
            channel_config={"discord": {"channel_bot_id": ids["bot_private_2"]}},
        )
        assert wf.integration_bindings == {"github": [ids["int_private_2"], ids["int_company"]]}
        assert wf.channel_config == {"discord": {"channel_bot_id": ids["bot_private_2"]}}

        # A new hidden resource (neither stored nor the agent's own) is still
        # refused on the same rows.
        await _refused(
            _update_assignment(
                sessions,
                w.member,
                w.shared_assignment,
                integration_bindings={"github": [ids["int_private_2"], ids["int_private_3"]]},
            )
        )
        await _refused(
            _update_agent(
                sessions,
                w.member,
                w.shared_agent,
                channel_config={
                    "teams": {"channel_bot_id": ids["bot_private"]},
                    "discord": {"channel_bot_id": ids["bot_private_2"]},
                },
            )
        )


@pytest.mark.asyncio
async def test_owner_and_admin_writes_are_unchanged():
    async with _temp_database() as sessions:
        w = await _seed(sessions)
        ids = w.ids

        # The owner binds their own private bot; an admin binds anyone's.
        own = await _create_agent(sessions, w.owner, {"teams": {"channel_bot_id": ids["bot_private"]}})
        assert own.channel_config == {"teams": {"channel_bot_id": ids["bot_private"]}}
        by_admin = await _create_agent(
            sessions, w.admin, {"teams": {"channel_bot_id": ids["bot_private"]}}
        )
        assert by_admin.channel_config == {"teams": {"channel_bot_id": ids["bot_private"]}}

        wf = await _create_assignment(
            sessions,
            w.admin,
            uuid.UUID(by_admin.id),
            integration_bindings={"github": [ids["int_private_2"]]},
            channel_config={"discord": {"channel_bot_id": ids["bot_private_2"]}},
        )
        assert wf.integration_bindings == {"github": [ids["int_private_2"]]}

        owner_wf = await _update_assignment(
            sessions,
            w.owner,
            w.shared_assignment,
            integration_bindings={"github": [ids["int_private_2"], ids["int_private"]]},
        )
        assert owner_wf.integration_bindings == {"github": [ids["int_private_2"], ids["int_private"]]}


async def _forbidden(call) -> HTTPException:
    with pytest.raises(HTTPException) as exc:
        await call
    assert exc.value.status_code == 403
    return exc.value


@pytest.mark.asyncio
async def test_only_an_admin_turns_on_the_whitelist_bypass_of_an_override():
    """``bypass_whitelist`` lets a run publish past the bot's whitelist and is
    admin-only. An override entry naming a bot the row or the agent already
    names binds nothing new, so it skips the channel check - the flag must be
    refused on its own there too, while a bypass the row or the agent's own
    entry already carries is kept on a re-save or a copy."""
    async with _temp_database() as sessions:
        w = await _seed(sessions)
        ids = w.ids
        agents_bot = {"teams": {"channel_bot_id": ids["bot_private"], "bypass_whitelist": True}}
        stored_bot = {
            "discord": {"channel_bot_id": ids["bot_private_2"], "bypass_whitelist": True}
        }

        # The member turns the flag on for the agent's bot (create) and for the
        # row's bot (update): refused, and the row is left as it was.
        on_create = await _forbidden(
            _create_assignment(sessions, w.member, w.shared_agent, channel_config=agents_bot)
        )
        on_update = await _forbidden(
            _update_assignment(sessions, w.member, w.shared_assignment, channel_config=stored_bot)
        )
        assert on_create.detail == on_update.detail == (
            "Only admins can enable bypass_whitelist on a channel."
        )
        async with sessions() as session:
            row = await session.get(Assignment, w.shared_assignment)
            assert row.channel_config == {"discord": {"channel_bot_id": ids["bot_private_2"]}}

        # An admin turns it on, on the agent and on the row.
        async with sessions() as session:
            agent = await session.get(Agent, w.shared_agent)
            agent.channel_config = dict(agents_bot)
            await session.commit()
        admin_wf = await _update_assignment(
            sessions, w.admin, w.shared_assignment, channel_config=stored_bot
        )
        assert admin_wf.channel_config == stored_bot

        # The member keeps what is already there: the agent's entry copied into
        # a new override, the row's entry re-saved with another field changed.
        copied = await _create_assignment(
            sessions, w.member, w.shared_agent, channel_config=agents_bot
        )
        assert copied.channel_config == agents_bot
        resaved = {"discord": {**stored_bot["discord"], "publish_enabled": True}}
        kept = await _update_assignment(
            sessions, w.member, w.shared_assignment, channel_config=resaved
        )
        assert kept.channel_config == resaved
