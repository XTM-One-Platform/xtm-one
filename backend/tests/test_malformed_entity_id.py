"""A malformed entity id gets the answer an unknown one gets.

The shared entity lookups in ``api/v1/helpers.py`` used to compare the raw
path string against the UUID primary key, so ``/agents/abc`` reached the
database driver and ended in a 500, while ``get_user_channel_bot()`` and
``get_manageable_entity()`` answered the same value with ``parse_uuid()``'s
422. A value that is not a UUID names no row, exactly like a well-formed id
nobody issued: every helper now parses its id with ``parse_path_id()`` before
any query and raises its own unknown-id 404, and
the routes of the same families that look the id up themselves do the same.

The helpers are driven directly (every helper, admin and non-admin); the
routes over HTTP (a minimal app per router, dependencies overridden) so the
status code and body a client sees is what is pinned.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import agent_skills as agent_skills_api
from app.api.v1 import agents as agents_api
from app.api.v1 import assignments as assignments_api
from app.api.v1 import channel_bots as channel_bots_api
from app.api.v1 import custom_tools as custom_tools_api
from app.api.v1 import flows as flows_api
from app.api.v1 import helpers
from app.api.v1 import integrations as integrations_api
from app.api.v1 import knowledge_bases as knowledge_bases_api
from app.api.v1 import mcp_servers as mcp_servers_api
from app.api.v1 import nova_rules as nova_rules_api
from app.api.v1 import prompts as prompts_api
from app.api.v1 import template_containers as template_containers_api
from app.core.database import get_db
from app.models.custom_tool import CustomTool

UNKNOWN_ID = "00000000-0000-0000-0000-000000000000"
MALFORMED_IDS = ["abc", "not-a-uuid", "12345", "", " " + UNKNOWN_ID, UNKNOWN_ID[:-1] + "g"]


class _Rows:
    def __init__(self, row=None):
        self._row = row

    def scalar_one_or_none(self):
        return self._row

    def scalar(self):
        return self._row

    def first(self):
        return self._row

    def scalars(self):
        rows = [] if self._row is None else list(self._row) if isinstance(self._row, list) else [self._row]
        return SimpleNamespace(all=lambda: rows, first=lambda: rows[0] if rows else None)

    def all(self):
        return self.scalars().all()


class _FakeDB:
    """Answers queries from a script (nothing once it runs out) and records them.

    Group memberships are answered outside the script (the caller belongs to
    no group), so a script only lists the entity rows the route reads.
    """

    def __init__(self, *rows):
        self._rows = list(rows)
        self.statements: list = []

    async def execute(self, statement, *_args, **_kwargs):
        self.statements.append(statement)
        if "group_members" in _tables(statement):
            return _Rows([])
        return _Rows(self._rows.pop(0) if self._rows else None)

    async def get(self, *_args, **_kwargs):
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None


def _user(*, admin: bool):
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="admin@example.com" if admin else "user@example.com",
        role="admin" if admin else "user",
        is_admin=admin,
    )


def _tables(statement) -> set[str]:
    froms = getattr(statement, "get_final_froms", None)
    return {getattr(f, "name", "") for f in froms()} if froms else set()


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def _lookup(name: str):
    async def call(db, user, entity_id):
        return await getattr(helpers, name)(db, user.id, entity_id, is_admin=user.is_admin)

    return call


async def _manageable(db, user, entity_id):
    return await helpers.get_manageable_entity(db, CustomTool, user, entity_id, entity_label="Custom tool")


HELPERS = {
    "get_user_agent": (_lookup("get_user_agent"), "Agent not found"),
    "get_user_assignment": (_lookup("get_user_assignment"), "Assignment not found"),
    "get_user_knowledge_base": (_lookup("get_user_knowledge_base"), "Knowledge base not found"),
    "get_user_custom_tool": (_lookup("get_user_custom_tool"), "Custom tool not found"),
    "get_user_mcp_server": (_lookup("get_user_mcp_server"), "MCP server not found"),
    "get_user_prompt": (_lookup("get_user_prompt"), "Prompt not found"),
    "get_user_integration": (_lookup("get_user_integration"), "Integration not found"),
    "get_user_channel_bot": (_lookup("get_user_channel_bot"), "Channel bot not found"),
    "get_manageable_entity": (_manageable, "Custom tool not found"),
}


@pytest.mark.parametrize("admin", [False, True], ids=["user", "admin"])
@pytest.mark.parametrize("name", list(HELPERS))
@pytest.mark.parametrize("malformed", MALFORMED_IDS)
async def test_every_helper_answers_a_malformed_id_like_an_unknown_one(name, malformed, admin):
    call, detail = HELPERS[name]
    user = _user(admin=admin)
    answers = []
    dbs = []
    for entity_id in (UNKNOWN_ID, malformed):
        db = _FakeDB()
        with pytest.raises(HTTPException) as exc_info:
            await call(db, user, entity_id)
        answers.append((exc_info.value.status_code, exc_info.value.detail))
        dbs.append(db)

    assert answers[0] == answers[1] == (404, detail)
    assert dbs[0].statements, "an unknown id is looked up"
    assert dbs[1].statements == [], "a malformed id names no row: nothing to look up, not even group memberships"


@pytest.mark.parametrize("name", list(HELPERS))
@pytest.mark.parametrize("entity_id", [UNKNOWN_ID, uuid.UUID(UNKNOWN_ID)], ids=["string", "uuid"])
async def test_a_well_formed_id_still_resolves(name, entity_id):
    call, _detail = HELPERS[name]
    row = SimpleNamespace(id=uuid.UUID(UNKNOWN_ID), is_company_managed=False, user_id=None)
    db = _FakeDB(row)

    assert await call(db, _user(admin=True), entity_id) is row


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


def _client(router, db: _FakeDB, user) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/x")

    async def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[deps.get_current_user] = lambda: user
    for gate in ("require_admin", "require_ai_enabled", "require_agentic_ai_licensed", "require_agentic_ai", "require_chat_enabled"):
        dependency = getattr(deps, gate)
        app.dependency_overrides[dependency] = (lambda: user) if gate == "require_admin" else (lambda: None)
    return TestClient(app, raise_server_exceptions=False)


def _kb_row():
    return SimpleNamespace(id=uuid.uuid4(), is_company_managed=False, user_id=None, is_builtin=False)


# name -> (router, method, url template, entity table, db script before the lookup, 404 detail)
ROUTES = {
    "agent": (agents_api.router, "GET", "/{id}", "agents", (), "Agent not found"),
    "agent-persona-version": (
        agents_api.router, "DELETE", "/{id}/persona/versions/1", "agents", (), "Agent not found",
    ),
    "assignment": (assignments_api.router, "GET", "/{id}", "assignments", (), "Assignment not found"),
    "knowledge-base": (knowledge_bases_api.router, "GET", "/{id}", "knowledge_bases", (), "Knowledge base not found"),
    "kb-document-download": (
        knowledge_bases_api.router, "GET", f"/{UNKNOWN_ID}/documents/{{id}}/download",
        "knowledge_documents", (_kb_row(),), "Document not found",
    ),
    "custom-tool": (custom_tools_api.router, "GET", "/{id}", "custom_tools", (), "Custom tool not found"),
    "custom-tool-delete": (custom_tools_api.router, "DELETE", "/{id}", "custom_tools", (), "Custom tool not found"),
    "mcp-server": (mcp_servers_api.router, "GET", "/{id}", "mcp_servers", (), "MCP server not found"),
    "prompt": (prompts_api.router, "GET", "/{id}", "prompts", (), "Prompt not found"),
    "integration": (integrations_api.router, "GET", "/{id}", "integrations", (), "Integration not found"),
    "channel-bot": (channel_bots_api.router, "GET", "/{id}/tools", "channel_bots", (), "Channel bot not found"),
    "skill": (agent_skills_api.router, "GET", "/{id}", "agent_skills", (), "Skill not found"),
    "flow": (flows_api.router, "GET", "/{id}", "flows", (), "Flow not found"),
    "template-container": (
        template_containers_api.router, "GET", "/{id}", "template_containers", (), "Template container not found",
    ),
    "nova-rule": (nova_rules_api.router, "GET", "/{id}", "nova_rules", (), "Nova rule not found"),
}


@pytest.mark.parametrize("admin", [False, True], ids=["user", "admin"])
@pytest.mark.parametrize("route", list(ROUTES))
def test_entity_routes_answer_a_malformed_id_like_an_unknown_one(route, admin):
    router, method, template, table, script, detail = ROUTES[route]
    if route == "agent-persona-version" and not admin:
        pytest.skip("admin-only route: a non-admin is refused before the id is read")
    user = _user(admin=admin)
    answers = []
    for entity_id in (UNKNOWN_ID, "not-a-uuid"):
        db = _FakeDB(*script)
        resp = _client(router, db, user).request(method, "/api/v1/x" + template.format(id=entity_id))
        answers.append((resp.status_code, resp.json(), db))

    (unknown_status, unknown_body, unknown_db), (status, body, malformed_db) = answers
    assert (unknown_status, unknown_body) == (404, {"detail": detail})
    assert (status, body) == (unknown_status, unknown_body)
    assert any(table in _tables(s) for s in unknown_db.statements), "an unknown id is looked up"
    assert not any(table in _tables(s) for s in malformed_db.statements), "a malformed id names no row"


def test_sync_source_routes_answer_a_malformed_id_after_the_same_checks(monkeypatch):
    """The source id is parsed where it is looked up: the KB checks run first for both ids."""

    async def _allowed(*_args, **_kwargs):
        return None

    for check in ("assert_knowledge_base_not_package_origin", "_assert_sync_sources_licensed", "_assert_kb_manageable"):
        monkeypatch.setattr(knowledge_bases_api, check, _allowed)

    user = _user(admin=True)
    answers = []
    for source_id in (UNKNOWN_ID, "not-a-uuid"):
        db = _FakeDB(_kb_row())
        resp = _client(knowledge_bases_api.router, db, user).delete(
            f"/api/v1/x/{UNKNOWN_ID}/sync-sources/{source_id}"
        )
        answers.append((resp.status_code, resp.json(), db))

    (unknown_status, unknown_body, unknown_db), (status, body, malformed_db) = answers
    assert (unknown_status, unknown_body) == (404, {"detail": "Sync source not found"})
    assert (status, body) == (unknown_status, unknown_body)
    assert any("kb_sync_sources" in _tables(s) for s in unknown_db.statements)
    assert not any("kb_sync_sources" in _tables(s) for s in malformed_db.statements)


# ---------------------------------------------------------------------------
# Tripwire
# ---------------------------------------------------------------------------

#: The helpers resolving an entity from a client-supplied id. A new
#: ``get_user_<entity>()`` is picked up by name; ``get_user_group_ids`` takes
#: the caller's own user id, not a client-supplied one.
_NOT_AN_ENTITY_LOOKUP = frozenset({"get_user_group_ids"})


def _entity_lookups(tree: ast.Module):
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if node.name in _NOT_AN_ENTITY_LOOKUP:
            continue
        if node.name.startswith("get_user_") or node.name == "get_manageable_entity":
            params = [a.arg for a in node.args.args if a.arg.endswith("_id") and a.arg != "user_id"]
            yield node, params


def test_every_entity_helper_parses_its_id_before_any_query():
    """A helper comparing the raw string against the UUID column is this bug again."""
    tree = ast.parse(Path(helpers.__file__).read_text(encoding="utf-8"))
    lookups = list(_entity_lookups(tree))
    assert {node.name for node, _ in lookups} >= set(HELPERS), "the tripwire must see every known helper"

    offenders = []
    for node, params in lookups:
        assert len(params) == 1, f"{node.name}: expected one client-supplied id, got {params}"
        (param,) = params
        parse_lines = [
            call.lineno
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and getattr(call.func, "id", None) == "parse_path_id"
            and call.args
            and isinstance(call.args[0], ast.Name)
            and call.args[0].id == param
        ]
        awaits = [sub.lineno for sub in ast.walk(node) if isinstance(sub, ast.Await)]
        raw_uses = [
            sub.lineno
            for sub in ast.walk(node)
            if isinstance(sub, ast.Compare)
            and any(isinstance(side, ast.Name) and side.id == param for side in [sub.left, *sub.comparators])
        ]
        if not parse_lines or (awaits and min(parse_lines) > min(awaits)) or raw_uses:
            offenders.append(node.name)

    assert offenders == [], (
        "entity helpers must parse their id with parse_path_id() before any query and never compare "
        f"the raw value against a column - a malformed id would reach the database driver: {offenders}"
    )
