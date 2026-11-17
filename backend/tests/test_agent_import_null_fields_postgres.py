"""Explicit ``null`` in an imported bundle, against a real PostgreSQL.

A bundle is untyped JSON authored elsewhere. The import pipeline reads its
entries with ``.get(key, default)``, and an explicit ``null`` bypasses the
default: the NOT NULL column received ``None`` and the INSERT failed with an
``IntegrityError`` - a 500 on ``POST /agents/import`` and
``POST /agents/import-file``. Same scratch-database recipe as
``test_provisioning_postgres.py``.
"""

from __future__ import annotations

import copy
import io
import json
import uuid

import pytest
from fastapi import HTTPException, Request, UploadFile
from sqlalchemy import select

from app.api.v1 import agents as agents_api
from app.api.v1.schemas.agent import ImportAgentRequest
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.assignment import Assignment
from app.models.custom_tool import CustomTool
from app.models.mcp_server import MCPServer
from app.models.prompt import Prompt
from app.models.user import User
from app.models.variable import Variable
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration

_BUNDLE = {
    "version": 3,
    "export_type": "agent",
    "agent": {"name": "Imported digest agent", "persona": "You summarise."},
    "variables": [{"id": str(uuid.uuid4()), "name": "REGION", "value": "eu"}],
    "custom_tools": [{"name": "Lookup", "js_function": "return 1;"}],
    "mcp_servers": [{"name": "Docs server", "transport": "stdio", "command": "docs"}],
    "skills": [{"name": "Summaries", "content": "Be brief."}],
    "prompts": [{"name": "Weekly", "content": "Summarise the week."}],
    "assignments": [
        {
            "name": "Nightly digest",
            "trigger_type": "schedule",
            "trigger_config": {"cron": "0 9 * * 1"},
            "prompt": "Write the digest.",
        }
    ],
}

#: Every NOT NULL column without a default that a bundle entry feeds, with the
#: value the import stores when the key is absent - and now when it is null.
_NOT_NULL_FIELDS = [
    ("assignments", "name", Assignment, "Imported Assignment"),
    ("assignments", "prompt", Assignment, ""),
    ("assignments", "trigger_type", Assignment, "manual"),
    ("custom_tools", "name", CustomTool, "Imported Tool"),
    ("mcp_servers", "name", MCPServer, "Imported Server"),
    ("skills", "name", AgentSkill, "Imported Skill"),
    ("prompts", "name", Prompt, "Imported Prompt"),
    ("variables", "name", Variable, "IMPORTED_VAR"),
]


def test_the_table_covers_every_not_null_column_a_bundle_entry_feeds():
    """A NOT NULL column added without a default must be added here too."""
    models = {Assignment: {"agent_id"}, CustomTool: set(), MCPServer: set(),
              AgentSkill: set(), Prompt: set(), Variable: set()}
    covered = {(model, field) for _key, field, model, _default in _NOT_NULL_FIELDS}
    for model, set_by_the_pipeline in models.items():
        for column in model.__table__.c:
            if column.nullable or column.primary_key or column.default is not None:
                continue
            if column.server_default is not None or column.name in set_by_the_pipeline:
                continue
            assert (model, column.name) in covered, f"{model.__name__}.{column.name}"


async def _user(sessionmaker) -> User:
    async with sessionmaker() as session:
        user = User(
            id=uuid.uuid4(),
            email=f"import-null-{uuid.uuid4().hex[:8]}@test.local",
            full_name="Import Null Test",
            hashed_password="x",
        )
        session.add(user)
        await session.commit()
        return user


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/x", "headers": []})


async def _import_json(sessionmaker, user: User, bundle: dict) -> None:
    async with sessionmaker() as db:
        await agents_api.import_agent(
            ImportAgentRequest(**bundle), _request(), await db.get(User, user.id), db
        )


async def _import_file(sessionmaker, user: User, bundle: dict) -> None:
    upload = UploadFile(file=io.BytesIO(json.dumps(bundle).encode()), filename="bundle.json")
    async with sessionmaker() as db:
        await agents_api.import_agent_file(_request(), upload, await db.get(User, user.id), db)


_ROUTES = {"import": _import_json, "import-file": _import_file}


@pytest.mark.asyncio
@pytest.mark.parametrize("route", sorted(_ROUTES))
@pytest.mark.parametrize(("key", "field", "model", "default"), _NOT_NULL_FIELDS)
async def test_a_null_not_null_field_imports_with_the_absent_key_default(
    route, key, field, model, default
):
    bundle = copy.deepcopy(_BUNDLE)
    bundle[key][0][field] = None
    async with _temp_database("imp_null") as sessionmaker:
        user = await _user(sessionmaker)

        await _ROUTES[route](sessionmaker, user, bundle)

        async with sessionmaker() as db:
            stored = (await db.execute(select(model))).scalars().all()
        assert [getattr(row, field) for row in stored] == [default]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", sorted(_ROUTES))
@pytest.mark.parametrize(("key", "field"), [(k, f) for k, f, _m, _d in _NOT_NULL_FIELDS])
async def test_a_non_string_not_null_field_is_a_400_naming_it(route, key, field):
    bundle = copy.deepcopy(_BUNDLE)
    bundle[key][0][field] = 42
    async with _temp_database("imp_null") as sessionmaker:
        user = await _user(sessionmaker)

        with pytest.raises(HTTPException) as exc_info:
            await _ROUTES[route](sessionmaker, user, bundle)

        assert exc_info.value.status_code == 400
        assert f"'{field}'" in exc_info.value.detail
        async with sessionmaker() as db:
            assert (await db.execute(select(Agent))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("route", sorted(_ROUTES))
async def test_a_valid_bundle_imports_as_before(route):
    async with _temp_database("imp_null") as sessionmaker:
        user = await _user(sessionmaker)

        await _ROUTES[route](sessionmaker, user, copy.deepcopy(_BUNDLE))

        async with sessionmaker() as db:
            assignment = (await db.execute(select(Assignment))).scalar_one()
            names = {
                model.__name__: [row.name for row in (await db.execute(select(model))).scalars()]
                for model in (Agent, CustomTool, MCPServer, AgentSkill, Prompt, Variable)
            }
        assert (assignment.name, assignment.trigger_type, assignment.prompt) == (
            "Nightly digest",
            "schedule",
            "Write the digest.",
        )
        assert names == {
            "Agent": ["Imported digest agent"],
            "CustomTool": ["Lookup"],
            "MCPServer": ["Docs server"],
            "AgentSkill": ["Summaries"],
            "Prompt": ["Weekly"],
            "Variable": ["REGION"],
        }
