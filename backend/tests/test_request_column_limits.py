"""Typed values that do not fit their column are refused, derived ones fitted.

The unit half of ``test_request_column_limits_postgres.py``. A create or update
route wrote its text fields as sent, so a value longer than its ``String(n)``
column validated and then failed the write with a 500. Every JSON body of the
routers below is walked: a string field that shares its name with a bounded
column of the router's models is refused past the column's length, naming the
field and the limit, or it is listed in ``NOT_CHECKED`` with the reason nothing
reaches the column as sent. A name the platform derives (a skill package's
default tool and knowledge base names, their ``-imported-N`` variants) is
shortened to fit instead. Every value that fits is unchanged.
"""

from __future__ import annotations

import importlib
import json
import logging
import types
import typing
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import APIRouter, HTTPException
from fastapi.routing import APIRoute
from pydantic import BaseModel, ValidationError

from app.api.v1 import chat as chat_api
from app.api.v1.api_keys import CreateApiKeyRequest, UpdateApiKeyRequest
from app.api.v1.custom_tools import LintRequest
from app.api.v1.groups import CreateGroupRequest, UpdateGroupRequest
from app.api.v1.intents import VerticalCreate, VerticalUpdate
from app.api.v1.schemas.agent import CreateAgentRequest, UpdateAgentRequest
from app.api.v1.schemas.channel_bot import CreateChannelBotRequest
from app.api.v1.schemas.flow import FlowUpdate
from app.api.v1.schemas.knowledge_base import CreateSyncSourceRequest, UpdateSyncSourceRequest
from app.api.v1.schemas.mcp_server import (
    MCPConfigPreviewRequest,
    MCPOAuthDiscoveryRequest,
    MCPServerUpdate,
)
from app.api.v1.schemas.nova_rule import NovaRuleCreate, NovaRuleUpdate
from app.api.v1.schemas.presentation import PresentationUpdate
from app.api.v1.schemas.settings import UserPreferencesRequest
from app.api.v1.users import CreateUserRequest, UpdateRoleRequest
from app.api.v1.variables import VariableCreate, VariableUpdate
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.agent_template import AgentTemplate
from app.models.api_key import UserApiKey
from app.models.assignment import Assignment
from app.models.channel_bot import ChannelBot
from app.models.chat import Conversation
from app.models.custom_tool import CustomTool
from app.models.dataset import Dataset
from app.models.evaluation import Evaluation
from app.models.file_attachment import FileAttachment
from app.models.flow import Flow
from app.models.flow_event import FlowEventType
from app.models.group import Group
from app.models.integration import Integration
from app.models.intent import Intent, IntentBinding, Vertical
from app.models.knowledge_base import KBSyncSource, KnowledgeBase, KnowledgeDocument
from app.models.mcp_server import MCPServer
from app.models.nova_rule import NovaRule
from app.models.presentation import Presentation, PresentationComment
from app.models.prompt import Prompt
from app.models.template_container import TemplateContainer
from app.models.user import User
from app.models.variable import Variable

#: The routers walked, and the models their bodies are written to.
ROUTER_MODELS: dict[str, tuple[type, ...]] = {
    "agents": (Agent,),
    "agent_templates": (AgentTemplate,),
    "assignments": (Assignment,),
    "knowledge_bases": (KnowledgeBase, KnowledgeDocument, KBSyncSource),
    "prompts": (Prompt,),
    "agent_skills": (AgentSkill,),
    "custom_tools": (CustomTool,),
    "mcp_servers": (MCPServer,),
    "integrations": (Integration,),
    "channel_bots": (ChannelBot,),
    "flows": (Flow,),
    "flow_event_types": (FlowEventType,),
    "template_containers": (TemplateContainer, FileAttachment),
    "presentations": (Presentation, PresentationComment),
    "groups": (Group,),
    "variables": (Variable,),
    "evaluations": (Evaluation,),
    "datasets": (Dataset,),
    "nova_rules": (NovaRule,),
    "intents": (Intent, IntentBinding),
    "api_keys": (UserApiKey,),
    "users": (User,),
    "settings": (User,),
}

#: A body written to another model than the router's own.
SCHEMA_MODELS: dict[type, tuple[type, ...]] = {
    VerticalCreate: (Vertical,),
    VerticalUpdate: (Vertical,),
}

#: Fields that share a name with a bounded column but never reach it as sent.
NOT_CHECKED: dict[tuple[type, str], str] = {
    (CreateAgentRequest, "slug"): "validate_slug bounds it: a 400 after stripping it",
    (UpdateAgentRequest, "slug"): "validate_slug bounds it: a 400 after stripping it",
    (CreateAgentRequest, "template_slug"): "only looks a template up: a 404 for a slug no template has",
    (CreateSyncSourceRequest, "source_type"): "allow-listed by the route",
    (CreateSyncSourceRequest, "sync_interval"): "allow-listed by the route",
    (UpdateSyncSourceRequest, "sync_interval"): "allow-listed by the route",
    (CreateChannelBotRequest, "provider"): "allow-listed by the route",
    (NovaRuleCreate, "action"): "allow-listed by the route",
    (NovaRuleUpdate, "action"): "allow-listed by the route",
    (CreateUserRequest, "role"): "allow-listed by the route",
    (UpdateRoleRequest, "role"): "allow-listed by the route",
    (FlowUpdate, "status"): "restricted to FLOW_STATUSES by its pattern",
    (PresentationUpdate, "theme"): "dark or light, checked by validate_spec",
    (UserPreferencesRequest, "theme"): "normalized to a known value by the route",
    (UserPreferencesRequest, "language"): "normalized to a known value by the route",
    (UserPreferencesRequest, "ctem_vertical"): "normalized to a known value by the route",
    (LintRequest, "language"): "lints the code, writes nothing",
    (MCPConfigPreviewRequest, "url"): "a preview, writes nothing",
    (MCPOAuthDiscoveryRequest, "url"): "discovery, writes nothing",
    (MCPOAuthDiscoveryRequest, "authorize_url"): "discovery, writes nothing",
    (MCPOAuthDiscoveryRequest, "token_url"): "discovery, writes nothing",
}

#: Fields the route strips before writing them.
STRIPPED: set[tuple[type, str]] = {
    (CreateGroupRequest, "name"),
    (UpdateGroupRequest, "name"),
    (VariableCreate, "name"),
    (VariableUpdate, "name"),
    (CreateApiKeyRequest, "name"),
    (UpdateApiKeyRequest, "name"),
    (MCPServerUpdate, "authorize_url"),
    (MCPServerUpdate, "token_url"),
    (MCPServerUpdate, "registration_url"),
}


def _is_str_field(annotation: object) -> bool:
    if annotation is str:
        return True
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType, typing.Annotated):
        return any(_is_str_field(arg) for arg in typing.get_args(annotation))
    return False


def _body_schemas() -> dict[type[BaseModel], tuple[set[str], set[type]]]:
    """Every JSON body of a POST / PUT / PATCH route: its routers and its models."""
    schemas: dict[type[BaseModel], tuple[set[str], set[type]]] = {}
    for module_name, models in ROUTER_MODELS.items():
        module = importlib.import_module(f"app.api.v1.{module_name}")
        routers = [value for value in vars(module).values() if isinstance(value, APIRouter)]
        assert routers, module_name
        for router in routers:
            for route in router.routes:
                if not isinstance(route, APIRoute) or not route.methods & {"POST", "PUT", "PATCH"}:
                    continue
                if route.body_field is None:
                    continue
                schema = route.body_field.field_info.annotation
                if typing.get_origin(schema) in (typing.Union, types.UnionType):
                    [schema] = [arg for arg in typing.get_args(schema) if arg is not type(None)]
                # Form uploads: FastAPI builds their body model, nothing declares it.
                if not isinstance(schema, type) or not issubclass(schema, BaseModel):
                    continue
                if schema.__module__.startswith("fastapi."):
                    continue
                modules, targets = schemas.setdefault(schema, (set(), set()))
                modules.add(module_name)
                targets.update(SCHEMA_MODELS.get(schema, models))
    return schemas


SCHEMAS = _body_schemas()


def _bounded_fields() -> list[tuple[type[BaseModel], str, int]]:
    """``(schema, field, limit)`` for every string field written to a bounded column."""
    found = []
    for schema, (_, models) in SCHEMAS.items():
        for field, info in schema.model_fields.items():
            if not _is_str_field(info.annotation):
                continue
            limits = [
                model.__table__.c[field].type.length
                for model in models
                if field in model.__table__.c and getattr(model.__table__.c[field].type, "length", None)
            ]
            if limits:
                found.append((schema, field, min(limits)))
    return sorted(found, key=lambda entry: (entry[0].__qualname__, entry[1]))


BOUNDED = _bounded_fields()
CHECKED = [entry for entry in BOUNDED if (entry[0], entry[1]) not in NOT_CHECKED]


def _id(entry: tuple[type, str, int]) -> str:
    return f"{entry[0].__qualname__}.{entry[1]}"


def _length_errors(schema: type[BaseModel], field: str, value: str) -> list[dict]:
    """The length errors *field* gets for *value* (required siblings stay missing)."""
    try:
        schema.model_validate({field: value})
    except ValidationError as exc:
        return [
            error
            for error in exc.errors()
            if error["loc"] == (field,)
            and (error["type"] == "string_too_long" or "characters (got" in error["msg"])
        ]
    return []


# ---------------------------------------------------------------------------
# The request schemas
# ---------------------------------------------------------------------------


def test_the_walk_reaches_a_bounded_field_in_every_router():
    """An empty walk would pass every test below."""
    reached = set()
    for schema, _, _ in CHECKED:
        reached |= SCHEMAS[schema][0]
    assert reached == set(ROUTER_MODELS)


@pytest.mark.parametrize("entry", CHECKED, ids=_id)
def test_a_value_longer_than_its_column_is_refused(entry):
    schema, field, limit = entry
    [error] = _length_errors(schema, field, "n" * (limit + 1))
    if error["type"] == "string_too_long":
        # A limit typed in the schema: it is the column's own.
        assert error["ctx"]["max_length"] == limit
    else:
        assert error["msg"] == (
            f"Value error, {field} must be at most {limit} characters (got {limit + 1})."
        )


@pytest.mark.parametrize("entry", CHECKED, ids=_id)
def test_a_value_at_the_limit_is_accepted(entry):
    schema, field, limit = entry
    assert _length_errors(schema, field, "n" * limit) == []
    if (schema, field) in STRIPPED:
        # The route strips it: padding around a value that fits keeps working.
        assert _length_errors(schema, field, f"  {'n' * limit}\t") == []


def test_every_exception_is_a_field_the_walk_finds():
    """A stale entry would hide a field the next change starts writing."""
    found = {(schema, field) for schema, field, _ in BOUNDED}
    assert found >= set(NOT_CHECKED)
    assert found - set(NOT_CHECKED) >= STRIPPED


def test_a_refusal_names_the_field_and_the_limit():
    limit = Agent.__table__.c.name.type.length
    with pytest.raises(ValidationError) as exc_info:
        CreateAgentRequest(name="n" * (limit + 1))
    [error] = exc_info.value.errors()
    assert error["loc"] == ("name",)
    assert error["msg"] == f"Value error, name must be at most {limit} characters (got {limit + 1})."


def test_values_that_fit_are_unchanged():
    limit = Agent.__table__.c.name.type.length
    request = UpdateAgentRequest(name="n" * limit, icon="Bot", llm_model=None)
    assert (request.name, request.icon, request.llm_model) == ("n" * limit, "Bot", None)
    assert UpdateAgentRequest().model_fields_set == set()
    padded = f"  {'g' * Group.__table__.c.name.type.length}  "
    assert CreateGroupRequest(name=padded).name == padded


def test_a_value_that_is_not_a_string_is_left_to_its_type():
    with pytest.raises(ValidationError) as exc_info:
        CreateAgentRequest(name=12)
    [error] = exc_info.value.errors()
    assert error["type"] == "string_type"


def test_the_slug_rule_keeps_to_the_agent_slug_column():
    from app.core.utils import validate_slug

    limit = Agent.__table__.c.slug.type.length
    assert validate_slug(" " + "a" * limit + " ") == "a" * limit
    with pytest.raises(ValueError, match=f"cannot exceed {limit} characters"):
        validate_slug("a" * (limit + 1))


def test_the_conversation_title_limit_is_the_column():
    assert Conversation.__table__.c.title.type.length == chat_api._CONVERSATION_TITLE_MAX_LENGTH


# ---------------------------------------------------------------------------
# Skill packages: derived names fitted, typed names refused
# ---------------------------------------------------------------------------


TOOL_LIMIT = CustomTool.__table__.c.name.type.length
KB_LIMIT = KnowledgeBase.__table__.c.name.type.length
SKILL_LIMIT = AgentSkill.__table__.c.name.type.length


def _package(*paths: str, manifest: str | None = None):
    from app.services.skill_package_manager import SkillPackageResource

    resources = [SkillPackageResource(path=path, content=b"print(1)\n") for path in paths]
    if manifest is not None:
        resources.append(
            SkillPackageResource(path="assets/xtm-resource-map.json", content=manifest.encode())
        )
    return resources


def test_a_default_tool_name_is_fitted_to_its_column():
    from app.api.v1.agent_skills import _default_tool_name

    assert _default_tool_name("demo", "scripts/fetch_data.py") == "demo-fetch-data"
    name = _default_tool_name("demo", f"scripts/{'x' * 300}.py")
    assert name == f"demo-{'x' * (TOOL_LIMIT - 5)}"


def test_the_default_names_of_a_long_skill_fit_their_columns():
    from app.api.v1.agent_skills import _normalize_resource_mappings

    skill_name = "s" * SKILL_LIMIT
    manifest, _ = _normalize_resource_mappings(
        skill_name=skill_name,
        resources=_package(f"scripts/{'x' * 250}.py", "references/guide.md"),
    )

    [script] = manifest["scripts"]
    [reference] = manifest["references"]
    assert script["tool"]["name"] == f"{skill_name}-{'x' * 250}"[:TOOL_LIMIT]
    assert reference["knowledge_base"]["name"] == f"{'s' * (KB_LIMIT - 11)}-references"


def test_short_default_names_are_unchanged():
    from app.api.v1.agent_skills import _normalize_resource_mappings

    manifest, _ = _normalize_resource_mappings(
        skill_name="demo", resources=_package("scripts/run.py", "references/guide.md")
    )

    assert manifest["scripts"][0]["tool"]["name"] == "demo-run"
    assert manifest["references"][0]["knowledge_base"]["name"] == "demo-references"


def _manifest(kind: str, path: str, key: str, name: str) -> str:
    return json.dumps({"schema_version": 1, kind: [{"path": path, key: {"name": name}}]})


@pytest.mark.parametrize(
    ("kind", "path", "key", "limit"),
    [
        ("scripts", "scripts/run.py", "tool", TOOL_LIMIT),
        ("references", "references/guide.md", "knowledge_base", KB_LIMIT),
    ],
)
def test_a_manifest_name_longer_than_its_column_is_refused(kind, path, key, limit):
    from app.api.v1.agent_skills import _normalize_resource_mappings

    with pytest.raises(HTTPException) as exc_info:
        _normalize_resource_mappings(
            skill_name="demo",
            resources=_package(path, manifest=_manifest(kind, path, key, "n" * (limit + 1))),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == (
        f"Invalid assets/xtm-resource-map.json: `{key}.name` must be at most {limit} "
        f"characters (got {limit + 1}) for {path}."
    )
    manifest, _ = _normalize_resource_mappings(
        skill_name="demo", resources=_package(path, manifest=_manifest(kind, path, key, "n" * limit))
    )
    assert manifest[kind][0][key]["name"] == "n" * limit


class _TakenNames:
    """A session on which the first *taken* candidate names already exist."""

    def __init__(self, taken: int) -> None:
        self.taken = taken
        self.candidates: list[str] = []

    async def execute(self, statement):
        self.candidates.append(statement.compile().params["name_1"])
        exists = len(self.candidates) <= self.taken
        result = MagicMock()
        result.scalar_one_or_none.return_value = "id" if exists else None
        return result


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [CustomTool, KnowledgeBase])
async def test_a_unique_name_keeps_its_counter_inside_the_column(model):
    from app.api.v1.agent_skills import _resolve_unique_name

    limit = model.__table__.c.name.type.length
    db = _TakenNames(taken=2)
    name = await _resolve_unique_name(db, model_cls=model, name="n" * limit)

    assert db.candidates == [
        "n" * limit,
        f"{'n' * (limit - 11)}-imported-2",
        f"{'n' * (limit - 11)}-imported-3",
    ]
    assert name == db.candidates[-1]
    short = _TakenNames(taken=1)
    assert await _resolve_unique_name(short, model_cls=model, name=" Lookup ") == "Lookup-imported-2"


@pytest.mark.asyncio
async def test_an_imported_skill_name_keeps_its_suffix_inside_the_column():
    from app.api.v1.agent_skills import _resolve_import_name

    db = _TakenNames(taken=1)
    name = await _resolve_import_name(db, preferred_name="n" * SKILL_LIMIT)

    assert db.candidates == [
        f"{'n' * (SKILL_LIMIT - 9)}-imported",
        f"{'n' * (SKILL_LIMIT - 11)}-imported-2",
    ]
    assert name == db.candidates[-1]
    assert await _resolve_import_name(_TakenNames(taken=0), preferred_name="demo") == "demo-imported"


def test_a_frontmatter_name_longer_than_the_column_is_refused():
    from app.api.v1.agent_skills import _frontmatter_name

    with pytest.raises(HTTPException) as exc_info:
        _frontmatter_name({"name": "n" * (SKILL_LIMIT + 1)})

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == (
        f"The SKILL.md frontmatter name must be at most {SKILL_LIMIT} characters "
        f"(got {SKILL_LIMIT + 1})."
    )
    assert _frontmatter_name({"name": "n" * SKILL_LIMIT}) == "n" * SKILL_LIMIT
    assert _frontmatter_name({}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("length", [SKILL_LIMIT, SKILL_LIMIT + 1])
async def test_the_scheduled_refresh_keeps_a_name_its_column_cannot_hold(caplog, length):
    """One skill's name must not fail the flush of every skill refreshed with it."""
    from app.services.autonomous import _refresh_stale_skills
    from tests.test_skill_autorefresh_url_redaction import _FakeDB, _skill

    skill = _skill("https://example.com/SKILL.md", name="docs")
    response = MagicMock(status_code=200, text=f"---\nname: {'n' * length}\n---\n# Docs")
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=response)

    with (
        patch("app.core.database.release_db_connection_before_loop", new_callable=AsyncMock),
        patch.object(httpx, "AsyncClient", return_value=client),
        caplog.at_level(logging.WARNING, logger="app.services.autonomous"),
    ):
        await _refresh_stale_skills(_FakeDB([skill]))

    assert skill.content.endswith("# Docs")
    if length <= SKILL_LIMIT:
        assert skill.name == "n" * length
        assert caplog.messages == []
    else:
        assert skill.name == "docs"
        assert caplog.messages == [
            "Skill 'docs' refreshed from https://example.com/SKILL.md keeps its name: "
            f"frontmatter name must be at most {SKILL_LIMIT} characters (got {length})."
        ]
