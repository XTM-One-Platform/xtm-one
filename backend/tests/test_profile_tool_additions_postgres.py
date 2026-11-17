"""A profile save may only add tools its user could bind to their own agent.

A chat turn loads the profile's tool lists - the agent chat defaults and the
General Assistant's MCP servers, knowledge bases and custom tools - with no
check of its own, and a chat request sending them back is not refused
(``chat_selection``: they are known). So ``PUT /settings/preferences`` checks
what a save ADDS, with the rule of an agent's own bindings (three-tier, admins
bypass): a colleague's private row and an id naming no row are refused alike;
what the profile already holds, and the platform's own General Assistant lists
a user starts from, keep saving, a row unshared since included.

The three-tier predicate reads the per-user sharing columns: PostgreSQL.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.api.v1.schemas.settings import UserPreferencesRequest
from app.api.v1.settings import update_preferences
from app.models.custom_tool import CustomTool
from app.models.general_assistant_config import GeneralAssistantConfig
from app.models.knowledge_base import KnowledgeBase
from app.models.mcp_server import MCPServer
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

#: Profile list -> the kind of row it names.
FIELDS = {
    "default_mcp_server_ids": "server",
    "default_knowledge_base_ids": "kb",
    "ga_mcp_server_ids": "server",
    "ga_knowledge_base_ids": "kb",
    "ga_custom_tool_ids": "tool",
}


def _user(name: str, role: str = "user") -> User:
    return User(
        email=f"{name}-{uuid.uuid4().hex[:8]}@test.local",
        full_name=name.title(),
        hashed_password="x",
        role=role,
    )


def _rows(owner: uuid.UUID, tag: str) -> dict:
    return {
        "server": MCPServer(name=f"{tag}-server", user_id=owner),
        "kb": KnowledgeBase(name=f"{tag}-kb", user_id=owner),
        "tool": CustomTool(
            name=f"{tag}_tool", description="A tool",
            input_schema={"type": "object", "properties": {}},
            language="javascript", js_function="return 1;", user_id=owner,
        ),
    }


async def _seed(sessions) -> dict:
    async with sessions() as session:
        member, colleague, admin = _user("member"), _user("colleague"), _user("admin", "admin")
        session.add_all([member, colleague, admin])
        await session.flush()
        own, foreign, platform = (
            _rows(member.id, "member"), _rows(colleague.id, "colleague"),
            _rows(colleague.id, "platform"),
        )
        session.add_all([*own.values(), *foreign.values(), *platform.values()])
        await session.flush()
        session.add(
            GeneralAssistantConfig(
                mcp_server_ids=[str(platform["server"].id)],
                knowledge_base_ids=[str(platform["kb"].id)],
                custom_tool_ids=[str(platform["tool"].id)],
            )
        )
        await session.commit()
        return {
            "member": member.id,
            "admin": admin.id,
            "own": {k: str(v.id) for k, v in own.items()},
            "foreign": {k: str(v.id) for k, v in foreign.items()},
            "platform": {k: str(v.id) for k, v in platform.items()},
        }


async def _save(session, user_id, **fields):
    user = await session.get(User, user_id)
    return await update_preferences(
        UserPreferencesRequest(**fields), request=None, user=user, db=session
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", list(FIELDS))
async def test_a_save_adding_a_row_its_user_cannot_bind_is_refused(field):
    kind = FIELDS[field]
    async with scratch_database("profile", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        refusals = []
        for extra in (w["foreign"][kind], str(uuid.uuid4())):
            async with db.sessions() as session:
                with pytest.raises(HTTPException) as exc:
                    await _save(session, w["member"], **{field: [w["own"][kind], extra]})
                refusals.append(exc.value)
        async with db.sessions() as session:
            assert await _save(session, w["member"], **{field: [w["own"][kind]]}) == {
                "status": "ok"
            }
            saved = getattr(await session.get(User, w["member"]), field)

    assert [r.status_code for r in refusals] == [403, 403]
    assert saved == [w["own"][kind]]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", list(FIELDS))
async def test_what_the_profile_holds_and_the_platform_defaults_keep_saving(field):
    """A row the profile already names (unshared since) is sent back by every
    Profile toggle; the General Assistant's own lists are where a user's
    first customisation starts from."""
    kind = FIELDS[field]
    async with scratch_database("profile", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        async with db.sessions() as session:
            user = await session.get(User, w["member"])
            setattr(user, field, [w["foreign"][kind]])
            await session.commit()
        requested = [w["foreign"][kind], w["own"][kind]]
        if field.startswith("ga_"):
            requested.append(w["platform"][kind])
        async with db.sessions() as session:
            result = await _save(session, w["member"], **{field: requested})

    assert result == {"status": "ok"}


@pytest.mark.asyncio
async def test_an_admin_may_add_any_row():
    async with scratch_database("profile", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        async with db.sessions() as session:
            result = await _save(
                session,
                w["admin"],
                **{field: [w["foreign"][kind]] for field, kind in FIELDS.items()},
            )

    assert result == {"status": "ok"}
