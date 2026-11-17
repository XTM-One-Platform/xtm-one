"""Admins see and edit everything; only the library lists are three-tier for them.

The library pages and the pickers list own + shared + company-managed for every
caller, admins included. Everything an admin opens takes the bypass:
the detail routes, and the views reached from an entity, which the detail pages
read with ``?scope=all`` - the whole platform for an admin, the library set for
anyone else. These tests pin both halves on the helpers, the list endpoints,
the run feeds and the run actions.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.api import deps
from app.api.v1 import agents as agents_api
from app.api.v1 import assignments as assignments_api
from app.api.v1 import helpers
from app.api.v1.helpers import list_scope_admin_bypass, require_run_access
from app.core.database import get_db

_APP = Path(__file__).resolve().parents[1] / "app"


def _user(*, admin: bool):
    return SimpleNamespace(
        id=uuid.uuid4(),
        is_admin=admin,
        role="admin" if admin else "user",
        email="user@example.com",
        full_name="User",
    )


def _where(statement) -> str:
    clause = statement.whereclause
    if clause is None:
        return ""
    return str(clause.compile(dialect=postgresql.dialect())).lower()


# ---------------------------------------------------------------------------
# The scope helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "admin", "expected"),
    [
        ("all", True, True),
        ("all", False, False),
        ("visible", True, False),
        (None, True, False),
    ],
)
def test_only_an_admin_asking_for_all_takes_the_bypass(scope, admin, expected):
    assert list_scope_admin_bypass(scope, _user(admin=admin)) is expected


# ---------------------------------------------------------------------------
# Every entity list endpoint takes the scope, and reads its bypass from it
# ---------------------------------------------------------------------------

_SCOPED_LISTS = {
    "api/v1/agents.py": "list_agents",
    "api/v1/integrations.py": "list_integrations",
    "api/v1/knowledge_bases.py": "list_knowledge_bases",
    "api/v1/mcp_servers.py": "list_mcp_servers",
    "api/v1/custom_tools.py": "list_custom_tools",
    "api/v1/agent_skills.py": "list_skills",
    "api/v1/prompts.py": "list_prompts",
    "api/v1/channel_bots.py": "list_channel_bots",
    "api/v1/runners.py": "list_runners",
    "api/v1/template_containers.py": "list_template_containers",
    "api/v1/variables.py": "list_variables",
    "api/v1/flows.py": "list_flows",
    "api/v1/datasets.py": "list_datasets",
    "api/v1/evaluations.py": "list_evaluations",
    "api/v1/assignments.py": "list_all_runs",
}


def _function(relative: str, name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse((_APP / relative).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {relative}")


@pytest.mark.parametrize("relative", sorted(_SCOPED_LISTS))
def test_every_entity_list_takes_the_scope(relative):
    """A list endpoint declares ``scope: EntityListScope`` and derives its admin
    bypass from it alone: the default stays the library set for an admin, and
    ``scope=all`` never widens a non-admin's reach."""
    function = _function(relative, _SCOPED_LISTS[relative])
    params = {
        arg.arg: ast.unparse(arg.annotation)
        for arg in function.args.args + function.args.kwonlyargs
        if arg.annotation is not None
    }
    assert params.get("scope") == "EntityListScope", params
    source = ast.unparse(function)
    if relative == "api/v1/assignments.py":
        assert "scope=scope" in source
    else:
        assert "is_admin=list_scope_admin_bypass(scope, user)" in source
        assert source.count("is_admin=") == 1, source


class _AnyDB:
    """Records every statement and answers each with an empty result."""

    def __init__(self):
        self.statements = []

    async def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: []),
            all=lambda: [],
            scalar_one_or_none=lambda: None,
            scalar_one=lambda: 0,
        )


def _client(monkeypatch, router, prefix, user):
    db = _AnyDB()

    async def _groups(_db, _uid):
        return []

    for module in (agents_api, assignments_api):
        monkeypatch.setattr(module, "get_user_group_ids", _groups)

    app = FastAPI()
    app.include_router(router, prefix=prefix)

    async def _get_db():
        yield db

    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _get_db
    return TestClient(app), db


@pytest.mark.parametrize(
    ("admin", "query", "whole_platform"),
    [
        (True, "?scope=all", True),
        (True, "", False),
        (True, "?scope=visible", False),
        (False, "?scope=all", False),
    ],
    ids=["admin-all", "admin-default", "admin-visible", "member-all"],
)
def test_the_agents_list_reads_the_whole_platform_for_an_admin_asking_for_it(
    monkeypatch, admin, query, whole_platform
):
    client, db = _client(monkeypatch, agents_api.router, "/api/v1/agents", _user(admin=admin))
    resp = client.get(f"/api/v1/agents{query}")
    assert resp.status_code == 200, resp.text
    where = _where(db.statements[0])
    assert ("agents.user_id" not in where) is whole_platform
    assert ("agents.shared_user_ids" not in where) is whole_platform


def test_an_unknown_scope_is_refused(monkeypatch):
    client, _db = _client(monkeypatch, agents_api.router, "/api/v1/agents", _user(admin=True))
    assert client.get("/api/v1/agents?scope=everything").status_code == 422


# ---------------------------------------------------------------------------
# Runs: the entity Activity tabs and the run actions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("admin", "scope", "run_visible"),
    [
        (True, "all", False),
        (True, "visible", True),
        (True, None, True),
        (False, "all", True),
    ],
    ids=["admin-all", "admin-visible", "admin-default", "member-all"],
)
def test_the_run_feed_lifts_the_all_bucket_for_an_admin_asking_for_it(admin, scope, run_visible):
    user = _user(admin=admin)
    query = assignments_api._filtered_runs_query(user, agent_id=str(uuid.uuid4()), scope=scope)
    where = _where(query)
    assert ("assignment_runs.user_id =" in where) is run_visible
    assert "assignment_runs.agent_id =" in where


def test_the_ownership_buckets_keep_their_meaning_for_an_admin():
    """``scope=all`` only lifts the ``all`` bucket: ``mine`` is still the
    admin's own runs."""
    admin = _user(admin=True)
    where = _where(assignments_api._filtered_runs_query(admin, ownership="mine", scope="all"))
    assert "assignment_runs.user_id =" in where


@pytest.mark.parametrize(
    ("admin", "scope", "run_visible"),
    [
        (True, "all", False),
        (True, None, True),
        (False, "all", True),
        (False, None, True),
    ],
    ids=["admin-all", "admin-default", "member-all", "member-default"],
)
def test_the_agent_ids_filter_narrows_the_feed_and_keeps_its_visibility(admin, scope, run_visible):
    """An entity's Activity tab filters by its bound agents on the server, on top
    of the run visibility: the admin scope still decides who sees what."""
    user = _user(admin=admin)
    ids = [uuid.uuid4(), uuid.uuid4()]
    where = _where(assignments_api._filtered_runs_query(user, agent_ids=ids, scope=scope))
    assert "assignment_runs.agent_id in" in where
    assert ("assignment_runs.user_id =" in where) is run_visible


def test_an_empty_agent_ids_filter_matches_no_run():
    admin = _user(admin=True)
    where = _where(assignments_api._filtered_runs_query(admin, agent_ids=[], scope="all"))
    assert "false" in where
    unfiltered = _where(assignments_api._filtered_runs_query(admin, scope="all"))
    assert "false" not in unfiltered
    assert "assignment_runs.agent_id" not in unfiltered


def test_the_agent_ids_parameter_reads_repeated_and_comma_separated_ids():
    a, b, c = (str(uuid.uuid4()) for _ in range(3))
    parse = assignments_api.parse_run_agent_ids
    assert parse(None) is None
    assert parse([""]) == []
    assert parse([f"{a}, {b}", c, a]) == [uuid.UUID(a), uuid.UUID(b), uuid.UUID(c)]
    with pytest.raises(HTTPException) as bad:
        parse([f"{a},not-a-uuid"])
    assert bad.value.status_code == 422
    too_many = ",".join(str(uuid.uuid4()) for _ in range(assignments_api.MAX_RUN_FILTER_AGENT_IDS + 1))
    with pytest.raises(HTTPException) as over:
        parse([too_many])
    assert over.value.status_code == 422
    assert str(assignments_api.MAX_RUN_FILTER_AGENT_IDS) in over.value.detail


@pytest.mark.parametrize("path", ["/api/v1/assignments/runs/all", "/api/v1/assignments/runs/count"])
def test_the_run_feed_and_its_count_take_the_agent_ids_filter(monkeypatch, path):
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    for admin, scope, scoped in ((True, "all", False), (False, "all", True), (True, "visible", True)):
        client, db = _client(
            monkeypatch, assignments_api.router, "/api/v1/assignments", _user(admin=admin)
        )
        resp = client.get(f"{path}?agent_ids={','.join(ids)}&scope={scope}")
        assert resp.status_code == 200, resp.text
        where = str(db.statements[0].compile(dialect=postgresql.dialect())).lower()
        assert "assignment_runs.agent_id in" in where
        assert ("assignment_runs.user_id =" in where) is scoped

    client, db = _client(monkeypatch, assignments_api.router, "/api/v1/assignments", _user(admin=True))
    resp = client.get(f"{path}?agent_ids=&scope=all")
    assert resp.status_code == 200, resp.text
    assert "false" in str(db.statements[0].compile(dialect=postgresql.dialect())).lower()
    assert client.get(f"{path}?agent_ids=nope").status_code == 422


@pytest.mark.parametrize("admin", [True, False], ids=["admin", "member"])
def test_the_assignment_activity_tab_counts_the_runs_it_lists(monkeypatch, admin):
    """The assignment Activity tab lists ``GET /assignments/{id}/runs`` and
    counts ``GET /assignments/runs/count`` with that assignment and
    ``scope=all``: one WHERE, so a search matching the agent or the assignment
    name (the documented predicate) never counts runs the list leaves out."""
    user = _user(admin=admin)
    assignment_id = str(uuid.uuid4())

    async def _visible(*_args, **_kwargs):
        return SimpleNamespace(id=uuid.UUID(assignment_id))

    monkeypatch.setattr(assignments_api, "get_user_assignment", _visible)
    wheres = []
    for path, counted in (
        (f"/api/v1/assignments/{assignment_id}/runs?search=digest", False),
        (f"/api/v1/assignments/runs/count?search=digest&assignment_id={assignment_id}&scope=all", True),
    ):
        client, db = _client(monkeypatch, assignments_api.router, "/api/v1/assignments", user)
        resp = client.get(path)
        assert resp.status_code == 200, resp.text
        [statement] = db.statements
        rows = statement.get_final_froms()[0].element if counted else statement
        compiled = rows.whereclause.compile(dialect=postgresql.dialect())
        wheres.append((str(compiled).lower(), compiled.params))
    assert wheres[0] == wheres[1]
    where = wheres[0][0]
    for column in ("agents.name", "assignments.name", "assignment_runs.result_summary"):
        assert f"{column} ilike" in where, where
    assert "assignment_runs.parent_run_id is null" in where
    assert ("assignment_runs.user_id =" in where) is (not admin)


def test_the_frontend_mirrors_the_agent_ids_cap():
    source = (_APP.parent.parent / "frontend" / "src" / "lib" / "run-agent-filter.ts").read_text(
        encoding="utf-8"
    )
    assert f"MAX_RUN_FILTER_AGENT_IDS = {assignments_api.MAX_RUN_FILTER_AGENT_IDS};" in source


def _run(owner, *, company=False):
    return SimpleNamespace(user_id=owner, is_company_managed=company)


def test_an_admin_acts_on_every_run():
    owner, admin, member = uuid.uuid4(), _user(admin=True), _user(admin=False)
    require_run_access(_run(owner), admin)
    require_run_access(_run(owner, company=True), admin)
    require_run_access(_run(member.id), member)
    with pytest.raises(HTTPException) as other:
        require_run_access(_run(owner), member)
    assert other.value.status_code == 404
    with pytest.raises(HTTPException) as company:
        require_run_access(_run(member.id, company=True), member)
    assert company.value.status_code == 403


def test_an_admin_sees_the_live_status_of_every_agent(monkeypatch):
    for admin, scoped in ((True, False), (False, True)):
        client, db = _client(
            monkeypatch, assignments_api.router, "/api/v1/assignments", _user(admin=admin)
        )
        resp = client.get("/api/v1/assignments/runs/active-agents")
        assert resp.status_code == 200, resp.text
        assert ("assignment_runs.user_id =" in _where(db.statements[0])) is scoped


def test_an_admin_bulk_deletes_the_runs_they_selected(monkeypatch):
    for admin, scoped in ((True, False), (False, True)):
        client, db = _client(
            monkeypatch, assignments_api.router, "/api/v1/assignments", _user(admin=admin)
        )
        resp = client.post(
            "/api/v1/assignments/runs/bulk-delete", json={"run_ids": [str(uuid.uuid4())]}
        )
        assert resp.status_code == 200, resp.text
        assert ("assignment_runs.user_id =" in _where(db.statements[0])) is scoped


# ---------------------------------------------------------------------------
# Detail routes that used to 404 for an admin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_admin_opens_a_colleague_s_variable(monkeypatch):
    from app.api.v1 import variables as variables_api

    async def _groups(_db, _uid):
        return []

    monkeypatch.setattr(variables_api, "get_user_group_ids", _groups)
    for admin, scoped in ((True, False), (False, True)):
        db = _AnyDB()
        with pytest.raises(HTTPException) as missing:
            await variables_api._get_user_variable(db, _user(admin=admin), str(uuid.uuid4()))
        assert missing.value.status_code == 404
        assert ("variables.user_id" in _where(db.statements[0])) is scoped


def test_the_skill_export_takes_the_admin_bypass():
    source = ast.unparse(_function("api/v1/agent_skills.py", "export_skill"))
    assert "is_admin=user.is_admin" in source


def test_the_assignment_lists_across_agents_take_the_admin_bypass():
    """The Schedule tab and the list across agents read the whole platform for
    an admin, as at the baseline."""
    for name in ("list_assignments", "list_assignment_schedule"):
        source = ast.unparse(_function("api/v1/assignments.py", name))
        assert "is_admin=" in source, name
        assert "user.is_admin" in source, name


def test_helpers_keep_one_scope_vocabulary():
    assert helpers.EntityListScope.__args__ == ("visible", "all")
