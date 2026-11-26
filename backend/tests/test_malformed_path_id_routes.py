"""Every route answers a malformed path id exactly like an unknown one.

The shared entity helpers follow this rule; the route families that parse
their own path id used to answer a malformed value with ``parse_uuid()``'s 422
(groups, datasets, evaluations, runners, traces, variables, presentations, ...)
or, on a few admin routes, with the 500 of a bare ``uuid.UUID()`` (users,
provider models). They now go
through ``parse_path_id()`` where the id is looked up, and a route whose answer
to an unknown id is not a 404 (an empty list, an idempotent delete, a token
bound to another deck) gives that same answer. List and body filters keep their
422.

Driven over HTTP (a minimal app per router, dependencies overridden), plus
tripwires over every route in ``app/api`` so a new route family cannot bring
the old answers back: no ``parse_uuid()`` / bare ``uuid.UUID()`` on a path id,
no primary-key comparison of an unchecked one, and no ``safe_uuid()`` answer
without the test pinning it.
"""

from __future__ import annotations

import ast
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import api_keys as api_keys_api
from app.api.v1 import assignments as assignments_api
from app.api.v1 import browser_profiles as browser_profiles_api
from app.api.v1 import datasets as datasets_api
from app.api.v1 import evaluations as evaluations_api
from app.api.v1 import files as files_api
from app.api.v1 import flow_runs as flow_runs_api
from app.api.v1 import groups as groups_api
from app.api.v1 import intents as intents_api
from app.api.v1 import knowledge_bases as knowledge_bases_api
from app.api.v1 import nova_rules as nova_rules_api
from app.api.v1 import platform as platform_api
from app.api.v1 import presentations as presentations_api
from app.api.v1 import public_catalog as public_catalog_api
from app.api.v1 import reports as reports_api
from app.api.v1 import runners as runners_api
from app.api.v1 import settings as settings_api
from app.api.v1 import traces as traces_api
from app.api.v1 import users as users_api
from app.api.v1 import variables as variables_api
from app.api.v1 import video_studio as video_studio_api
from app.core.database import get_db
from tests import app_sources

UNKNOWN_ID = "00000000-0000-0000-0000-000000000000"
MALFORMED_ID = "not-a-uuid"


class _Rows:
    def __init__(self, row=None):
        self._row = row

    def scalar_one_or_none(self):
        return self._row

    def scalar(self):
        return self._row

    def scalar_one(self):
        return 0 if self._row is None else self._row

    def first(self):
        return self._row

    def scalars(self):
        rows = [] if self._row is None else list(self._row) if isinstance(self._row, list) else [self._row]
        return SimpleNamespace(all=lambda: rows, first=lambda: rows[0] if rows else None)

    def all(self):
        return self.scalars().all()


class _FakeDB:
    """A database holding nothing but the scripted rows; group memberships are empty."""

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

    async def flush(self):
        return None

    async def delete(self, _obj):
        return None


def _tables(statement) -> set[str]:
    froms = getattr(statement, "get_final_froms", None)
    return {getattr(f, "name", "") for f in froms()} if froms else set()


def _user(*, admin: bool):
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="admin@example.com" if admin else "user@example.com",
        role="admin" if admin else "user",
        is_admin=admin,
        is_active=True,
    )


class _EmptyTraceStore:
    """A trace store holding no trace: what an unknown id meets."""

    async def get_trace_detail(self, *_args, **_kwargs):
        raise HTTPException(status_code=404, detail="Trace not found")

    async def get_trace_scores(self, *_args, **_kwargs):
        raise HTTPException(status_code=404, detail="Trace not found")

    async def list_traces(self, *_args, **_kwargs):
        return [], 0

    async def get_spans_for_trace_ids(self, *_args, **_kwargs):
        return {}


def _client(router, db: _FakeDB, user) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/x")

    async def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[deps.get_current_user] = lambda: user
    for gate in ("require_admin", "require_ai_enabled", "require_agentic_ai_licensed", "require_agentic_ai", "require_chat_enabled"):
        app.dependency_overrides[getattr(deps, gate)] = (lambda: user) if gate == "require_admin" else (lambda: None)
    app.dependency_overrides[traces_api.require_trace_read_store] = _EmptyTraceStore
    app.dependency_overrides[variables_api._require_custom_variables_enabled] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _runners_enabled(monkeypatch):
    monkeypatch.setattr(runners_api, "_require_enabled", lambda: None)


def _evaluation_row():
    return SimpleNamespace(id=uuid.uuid4(), is_company_managed=False, user_id=None)


# name -> (router, method, url template, admin only, db script, expected (status, body))
ROUTES = {
    "group": (groups_api.router, "GET", "/{id}", True, (), (404, {"detail": "Group not found"})),
    "group-member": (
        groups_api.router, "DELETE", f"/{UNKNOWN_ID}/members/{{id}}", True, (), (404, {"detail": "Group membership not found"}),
    ),
    "variable": (variables_api.router, "GET", "/{id}", False, (), (404, {"detail": "Variable not found"})),
    "dataset": (datasets_api.router, "GET", "/{id}", True, (), (404, {"detail": "Dataset not found"})),
    "evaluation-datasets": (datasets_api.router, "GET", "/for-evaluation/{id}", True, (), (200, [])),
    "evaluation": (evaluations_api.router, "GET", "/{id}", True, (), (404, {"detail": "Evaluation not found"})),
    "evaluation-run": (evaluations_api.router, "GET", "/runs/{id}", True, (), (404, {"detail": "Evaluation run not found"})),
    "evaluation-agent-remove": (
        evaluations_api.router, "DELETE", f"/{UNKNOWN_ID}/agents/{{id}}", True, (_evaluation_row(),), (204, None),
    ),
    "assignment-run": (assignments_api.router, "GET", "/runs/{id}", False, (), (404, {"detail": "Run not found"})),
    "flow-run": (flow_runs_api.router, "GET", "/runs/{id}", False, (), (404, {"detail": "Run not found"})),
    "file-delete": (files_api.router, "DELETE", "/{id}", False, (), (404, {"detail": "File not found"})),
    "api-key-delete": (api_keys_api.router, "DELETE", "/{id}", False, (), (404, {"detail": "API key not found"})),
    "report": (reports_api.router, "GET", "/{id}", False, (), (404, {"detail": "Report not found"})),
    "intent": (intents_api.router, "GET", "/intents/{id}", False, (), (404, {"detail": "Intent not found"})),
    "presentation": (presentations_api.router, "GET", "/{id}", False, (), (404, {"detail": "Presentation not found"})),
    "runner": (runners_api.router, "GET", "/{id}", False, (), (404, {"detail": "Runner not found"})),
    "trace": (traces_api.router, "GET", "/{id}", False, (), (404, {"detail": "Trace not found"})),
    "trace-scores": (traces_api.router, "GET", "/{id}/scores", True, (), (404, {"detail": "Trace not found"})),
    "catalog-agent": (
        public_catalog_api.router, "GET", "/catalog/agents/{id}", False, (), (404, {"detail": "Agent not found in catalog"}),
    ),
    "catalog-download": (
        public_catalog_api.router, "GET", "/catalog/agents/{id}/download", False, (), (404, {"detail": "Not found"}),
    ),
    "user": (users_api.router, "GET", "/{id}", True, (), (404, {"detail": "User not found"})),
    "user-agents": (users_api.router, "GET", "/{id}/agents", True, (), (200, [])),
    "user-integrations": (users_api.router, "GET", "/{id}/integrations", True, (), (200, [])),
    "user-mcp-servers": (users_api.router, "GET", "/{id}/mcp-servers", True, (), (200, [])),
    "user-knowledge-bases": (users_api.router, "GET", "/{id}/knowledge-bases", True, (), (200, [])),
    "user-custom-tools": (users_api.router, "GET", "/{id}/custom-tools", True, (), (200, [])),
    "provider-models": (
        settings_api.router, "GET", "/llm-providers/{id}/models", True, (), (404, {"detail": "Provider config not found"}),
    ),
    "nova-source-sync": (
        nova_rules_api.router, "POST", "/sources/{id}/sync", True, (), (404, {"detail": "Source not found"}),
    ),
    "kb-export-download": (
        knowledge_bases_api.router, "GET", "/exports/{id}/download", False, (), (404, {"detail": "Export package not found"}),
    ),
    "platform-registration-purge": (
        platform_api.router, "DELETE", "/registrations/{id}/purge", True, (), (404, {"detail": "Platform registration not found"}),
    ),
    "video-project": (video_studio_api.router, "GET", "/projects/{id}", False, (), (404, {"detail": "Not found"})),
    "video-project-renders": (
        video_studio_api.router, "GET", "/projects/{id}/renders", False, (), (404, {"detail": "Not found"}),
    ),
    "video-project-preview": (
        video_studio_api.router, "POST", "/projects/{id}/preview", False, (), (404, {"detail": "Not found"}),
    ),
    "video-project-duplicate": (
        video_studio_api.router, "POST", "/projects/{id}/duplicate", False, (), (404, {"detail": "Not found"}),
    ),
    "video-render": (video_studio_api.router, "GET", "/renders/{id}", False, (), (404, {"detail": "Not found"})),
    "video-take": (video_studio_api.router, "GET", "/takes/{id}", False, (), (404, {"detail": "Not found"})),
    "video-clip": (video_studio_api.router, "GET", "/clips/{id}", False, (), (404, {"detail": "Not found"})),
    "browser-profile": (
        browser_profiles_api.router, "GET", "/{id}", False, (), (404, {"detail": "Not found"}),
    ),
    "browser-profile-sign-in": (
        browser_profiles_api.router, "POST", "/{id}/sign-in", False, (), (404, {"detail": "Not found"}),
    ),
    "browser-profile-mfa-screenshot": (
        browser_profiles_api.router, "GET", "/{id}/mfa-screenshot", False, (), (404, {"detail": "Not found"}),
    ),
}


def _answer(resp):
    return resp.status_code, (resp.json() if resp.content else None)


@pytest.mark.parametrize("admin", [False, True], ids=["user", "admin"])
@pytest.mark.parametrize("route", list(ROUTES))
def test_routes_answer_a_malformed_path_id_like_an_unknown_one(route, admin):
    router, method, template, admin_only, script, expected = ROUTES[route]
    if admin_only and not admin:
        pytest.skip("admin-only route: a non-admin is refused before the id is read")
    user = _user(admin=admin)
    answers = [
        _answer(_client(router, _FakeDB(*script), user).request(method, "/api/v1/x" + template.format(id=entity_id)))
        for entity_id in (UNKNOWN_ID, MALFORMED_ID)
    ]

    assert answers[0] == expected
    assert answers[1] == answers[0]


@pytest.mark.parametrize("kind", ["render", "take", "clip"])
def test_a_video_media_token_for_a_malformed_id_is_refused_like_an_unknown_one(kind):
    """The Video Studio cards and player mint a media URL per asset: a malformed id is the unknown id's 404."""
    client = _client(video_studio_api.router, _FakeDB(), _user(admin=False))
    answers = [
        _answer(client.post("/api/v1/x/media-token", json={"kind": kind, "id": entity_id, "asset": "poster"}))
        for entity_id in (UNKNOWN_ID, MALFORMED_ID)
    ]

    assert answers[0] == answers[1] == (404, {"detail": "Not found"})


def test_a_list_filter_keeps_its_422():
    """A malformed list filter is refused, not read as "no filter" nor a 500."""
    client = _client(knowledge_bases_api.router, _FakeDB(), _user(admin=False))

    assert client.get("/api/v1/x", params={"integration_id": MALFORMED_ID}).status_code == 422


@pytest.mark.parametrize("admin", [False, True], ids=["user", "admin"])
def test_the_session_explorer_answers_a_malformed_id_like_an_unknown_one(admin):
    """The same empty explorer; ``session_id`` echoes the id asked for."""
    answers = []
    for session_id in (UNKNOWN_ID, MALFORMED_ID):
        resp = _client(traces_api.router, _FakeDB(), _user(admin=admin)).get(f"/api/v1/x/sessions/{session_id}/explorer")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.pop("session_id") == session_id
        answers.append(body)

    assert answers[0] == answers[1]
    assert (answers[0]["traces"], answers[0]["total_traces"], answers[0]["total_spans"]) == ([], 0, 0)


def test_a_render_token_for_another_deck_and_a_malformed_deck_id_get_the_same_refusal(monkeypatch):
    deck = uuid.uuid4()
    monkeypatch.setattr(presentations_api, "decode_render_token", lambda _token: SimpleNamespace(presentation_id=deck))
    request = SimpleNamespace(query_params={}, headers={"authorization": "Bearer t"})

    answers = []
    for deck_id in (UNKNOWN_ID, MALFORMED_ID):
        with pytest.raises(HTTPException) as exc_info:
            presentations_api._render_claims(request, deck_id)
        answers.append((exc_info.value.status_code, exc_info.value.detail))

    assert answers[0] == answers[1] == (403, "Access denied")


def test_a_render_token_for_another_report_and_a_malformed_report_id_get_the_same_refusal(monkeypatch):
    report = uuid.uuid4()
    monkeypatch.setattr(reports_api, "decode_report_render_token", lambda _token: SimpleNamespace(report_id=report))
    request = SimpleNamespace(query_params={}, headers={"authorization": "Bearer t"})

    answers = []
    for report_id in (UNKNOWN_ID, MALFORMED_ID):
        with pytest.raises(HTTPException) as exc_info:
            reports_api._render_claims(request, report_id)
        answers.append((exc_info.value.status_code, exc_info.value.detail))

    assert answers[0] == answers[1] == (403, "Access denied")


# ---------------------------------------------------------------------------
# Tripwire
# ---------------------------------------------------------------------------

_ROUTE_METHODS = {"get", "post", "put", "patch", "delete", "api_route", "websocket"}
_PATH_PARAM = re.compile(r"{([a-zA-Z_]\w*)(?::\w+)?}")
_OLD_PARSERS = {"parse_uuid", "UUID"}


def _callee(node: ast.Call) -> str | None:
    return node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)


def _path_params(func) -> set[str]:
    for dec in func.decorator_list:
        if (
            isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Attribute)
            and dec.func.attr in _ROUTE_METHODS
            and dec.args
            and isinstance(dec.args[0], ast.Constant)
            and isinstance(dec.args[0].value, str)
        ):
            return set(_PATH_PARAM.findall(dec.args[0].value))
    return set()


def _is_name(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _helper_scopes(func, name: str, by_name: dict) -> list:
    """Each module helper *func* passes *name* to, with the parameter it lands in."""
    scopes = []
    for call in ast.walk(func):
        if not isinstance(call, ast.Call) or _callee(call) not in by_name:
            continue
        helper = by_name[_callee(call)]
        params = [a.arg for a in helper.args.posonlyargs + helper.args.args]
        scopes += [(helper, params[i]) for i, arg in enumerate(call.args) if i < len(params) and _is_name(arg, name)]
        scopes += [(helper, kw.arg) for kw in call.keywords if kw.arg and _is_name(kw.value, name)]
    return scopes


def _route_path_ids():
    """``(where, route, param, scopes)`` for every path param of every route in ``app/api``.

    *scopes* is the route with the param, then every module helper the route passes it to with the parameter it
    lands in (``_get_user_dataset(db, user, dataset_id)``), the shape every route family uses to look its entity up.
    """
    api_dir = Path(traces_api.__file__).resolve().parent.parent
    for path in app_sources.python_files(api_dir):
        tree = app_sources.tree(path)
        functions = [n for n in app_sources.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        by_name = {f.name: f for f in functions}
        where = path.relative_to(api_dir.parent).as_posix()
        for func in functions:
            for param in sorted(_path_params(func)):
                yield where, func, param, [(func, param), *_helper_scopes(func, param, by_name)]


def _passes_to(func, name: str, accepts) -> bool:
    """Whether *func* hands *name* to a call whose callee *accepts*."""
    return any(
        isinstance(node, ast.Call)
        and accepts(_callee(node) or "")
        and any(_is_name(arg, name) for arg in (*node.args, *(kw.value for kw in node.keywords)))
        for node in ast.walk(func)
    )


def _old_parses(func, name: str) -> list[int]:
    """Lines where *name* goes straight into ``parse_uuid()`` / ``uuid.UUID()``."""
    return [
        node.lineno
        for node in ast.walk(func)
        if isinstance(node, ast.Call) and _callee(node) in _OLD_PARSERS and node.args and _is_name(node.args[0], name)
    ]


def test_no_route_parses_a_path_id_with_parse_uuid_or_a_bare_uuid_call():
    """A path id answers like an unknown one: ``parse_path_id()``, or ``safe_uuid()`` for a route's own answer."""
    offenders = [
        f"{where}:{line}"
        for where, _route, _param, scopes in _route_path_ids()
        for scope, name in scopes
        for line in _old_parses(scope, name)
    ]

    assert sorted(set(offenders)) == [], (
        "a path id parsed with parse_uuid() (422) or a bare uuid.UUID() (500) - answer a malformed id like an "
        f"unknown one with parse_path_id(), or safe_uuid() where the route's own answer is not a 404: {offenders}"
    )


#: What checks a path id before anything compares it: the parsers, the guards of a route with its own answer, and
#: the shared lookups that parse the id themselves (every ``get_user_*()`` helper too).
_ID_CHECKS = frozenset(
    {
        "parse_path_id",
        "parse_conversation_id",
        "safe_uuid",
        "is_valid_uuid",
        "get_manageable_entity",
        "load_readable_file",
        "load_readable_chat_image",
        "resolve_conversation_for_read",
    }
)


def _typed_uuid(func, name: str) -> bool:
    """A ``uuid.UUID``-typed param: FastAPI validates it before the route runs (its 422 is kept)."""
    return any(
        arg.arg == name and arg.annotation is not None and "UUID" in ast.unparse(arg.annotation)
        for arg in (*func.args.args, *func.args.kwonlyargs)
    )


def _raw_primary_key_compares(func, name: str) -> list[int]:
    """Lines comparing *name* itself with a primary key (``Model.id == name``)."""
    return [
        node.lineno
        for node in ast.walk(func)
        if isinstance(node, ast.Compare)
        and any(_is_name(side, name) for side in (node.left, *node.comparators))
        and any(isinstance(side, ast.Attribute) and side.attr == "id" for side in (node.left, *node.comparators))
    ]


def test_no_route_compares_an_unchecked_path_id_with_a_primary_key():
    """``Model.id == <path id>`` hands a malformed id to the database driver: a 500 where an unknown id gets 404.

    The shape the shared lookups and the sync-source routes must avoid. An id the route checks
    first, or that FastAPI validates, may be compared.
    """
    offenders = [
        f"{where}:{line}"
        for where, route, param, scopes in _route_path_ids()
        if not _typed_uuid(route, param)
        and not any(_passes_to(s, n, lambda c: c in _ID_CHECKS or c.startswith("get_user_")) for s, n in scopes)
        for scope, name in scopes
        for line in _raw_primary_key_compares(scope, name)
    ]

    assert offenders == [], (
        "a path id compared with a primary key before anything checked it - a malformed id reaches the database "
        f"driver (500) instead of getting the unknown-id answer: parse it with parse_path_id() first: {offenders}"
    )


#: The routes answering a malformed path id themselves (``safe_uuid()`` / ``is_valid_uuid()``) because their answer
#: to an id naming nothing is not a 404, each with the test pinning that a malformed id gets that very answer: a
#: ``ROUTES`` key or a test function name.
_OWN_ANSWERS = {
    "api/v1/datasets.py:list_evaluation_datasets": "evaluation-datasets",
    "api/v1/datasets.py:remove_entry_file": "test_dataset_entry_file_delete_answers_a_malformed_file_id_like_an_unknown_one",
    "api/v1/evaluations.py:remove_agent": "evaluation-agent-remove",
    "api/v1/presentations.py:render_asset": "test_a_render_token_for_another_deck_and_a_malformed_deck_id_get_the_same_refusal",
    "api/v1/presentations.py:render_polls": "test_a_render_token_for_another_deck_and_a_malformed_deck_id_get_the_same_refusal",
    "api/v1/presentations.py:render_spec": "test_a_render_token_for_another_deck_and_a_malformed_deck_id_get_the_same_refusal",
    "api/v1/public_chat.py:send_public_message_stream": "test_widget_stream_answers_a_malformed_session_id_with_the_unknown_id_event",
    "api/v1/reports.py:render_spec": "test_a_render_token_for_another_report_and_a_malformed_report_id_get_the_same_refusal",
    "api/v1/traces.py:get_session_explorer": "test_the_session_explorer_answers_a_malformed_id_like_an_unknown_one",
    "api/v1/users.py:list_user_agents": "user-agents",
    "api/v1/users.py:list_user_custom_tools": "user-custom-tools",
    "api/v1/users.py:list_user_integrations": "user-integrations",
    "api/v1/users.py:list_user_knowledge_bases": "user-knowledge-bases",
    "api/v1/users.py:list_user_mcp_servers": "user-mcp-servers",
}
#: Webhook receivers acknowledge every id naming nothing with a 200 ``ignored`` whose ``reason`` says what was wrong
#: for the sender's delivery log ("invalid agent id", "agent not found or disabled"). A malformed id can never name
#: a row, so saying it is malformed reveals nothing about which ids exist.
_WEBHOOK_RECEIVERS = frozenset(
    {
        "api/v1/webhooks.py:receive_assignment_webhook",
        "api/v1/webhooks.py:receive_direct_assignment_webhook",
        "api/v1/webhooks.py:receive_flow_webhook",
        "api/v1/webhooks.py:receive_integration_webhook",
        "api/v1/webhooks.py:receive_signal_source_webhook",
        "api/v1/webhooks.py:receive_webhook",
    }
)


def test_every_route_answering_a_malformed_path_id_itself_names_the_test_pinning_that_answer():
    """A ``safe_uuid()`` fallback that differs from the unknown-id answer goes unnoticed without a paired test.

    That is how ``GET /knowledge-bases/exports/{id}/download`` kept a 400 for a malformed id next to the 404 of an
    unknown one. A new route checking its path id this way fails here until it is listed with its test.
    """
    found = {
        f"{where}:{route.name}"
        for where, route, _param, scopes in _route_path_ids()
        if any(_passes_to(s, n, lambda c: c in {"safe_uuid", "is_valid_uuid"}) for s, n in scopes)
    }
    tests_dir = Path(__file__).resolve().parent
    defined = {
        match.group(1)
        for path in tests_dir.glob("test_*.py")
        for match in re.finditer(r"^(?:async )?def (test_\w+)\(", path.read_text(encoding="utf-8"), re.MULTILINE)
    }

    assert sorted(found - set(_OWN_ANSWERS) - _WEBHOOK_RECEIVERS) == [], (
        "a route answering a malformed path id with safe_uuid() / is_valid_uuid() - use parse_path_id(), or list it "
        "in _OWN_ANSWERS with the test proving its answer is the unknown id's"
    )
    assert sorted((set(_OWN_ANSWERS) | _WEBHOOK_RECEIVERS) - found) == [], "a listed route no longer answers itself"
    assert [test for test in _OWN_ANSWERS.values() if test not in ROUTES and test not in defined] == []


def _required_keyword_only(helpers: ast.Module) -> dict[str, set[str]]:
    """Each public function of ``helpers.py`` -> its keyword-only parameters that have no default."""
    return {
        node.name: required
        for node in helpers.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and (required := {a.arg for a, d in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True) if d is None})
    }


def _helper_names_imported(module: ast.Module, helpers: dict[str, set[str]]) -> dict[str, str]:
    """The local name -> the helper it names, for every helper *module* imports from ``helpers.py``, inline too."""
    return {
        alias.asname or alias.name: alias.name
        for node in app_sources.walk(module)
        if isinstance(node, ast.ImportFrom) and node.module == "app.api.v1.helpers"
        for alias in node.names
        if alias.name in helpers
    }


def test_every_call_to_a_shared_helper_passes_its_required_keyword_arguments():
    """``parse_path_id(render_id)`` is a ``TypeError`` (a 500) on every request, which no linter reports.

    The whole Video Studio API shipped that way: every detail route, the media token behind each
    card's poster and the player answered 500. Its required ``not_found_detail`` is the route's unknown-id
    wording, so a call that leaves it out never worked.
    """
    helpers_path = Path(traces_api.__file__).resolve().parent / "helpers.py"
    required = _required_keyword_only(app_sources.tree(helpers_path))
    offenders = []
    for where, module in app_sources.modules():
        local = (
            {name: name for name in required}
            if where == "api/v1/helpers.py"
            else _helper_names_imported(module, required)
        )
        for node in app_sources.walk(module):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                local.get(func.id)
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute) and _is_name(func.value, "helpers") and func.attr in required
                else None
            )
            if name is None or any(kw.arg is None for kw in node.keywords):
                continue
            missing = required[name] - {kw.arg for kw in node.keywords}
            if missing:
                offenders.append(f"{where}:{node.lineno} {name}() without {sorted(missing)}")

    assert offenders == [], f"a call to a shared helper misses a required keyword argument: {offenders}"
