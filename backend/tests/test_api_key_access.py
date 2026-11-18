"""Personal API keys: one resolution, scopes, expiry and per-key limits.

The tripwires first: a personal key (``fcp-...``) is verified in
``services/api_key_access.py`` only, every surface accepting one resolves it
there and says which surface it is, and every MCP endpoint built on the key
authentication narrows its tools to the key. Then the behaviour, with fakes:
what each scope lets through, expiry, the key's own rate limit and quota, the
expiry notices' thresholds. ``test_api_keys_postgres.py`` drives the routes.
"""

from __future__ import annotations

import ast
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.v1 import mcp_platform, mcp_xtm
from app.core import rate_limit
from app.services import api_key_access as access
from app.services.api_key_access import ApiKeyPrincipal, ApiKeySurface
from app.services.tool_classification import infer_tool_type
from tests import app_sources

# ---------------------------------------------------------------------------
# Tripwires
# ---------------------------------------------------------------------------

#: Every place a personal key is resolved, and the surface it guards. A new
#: surface accepting keys is added here on purpose, with its operation checks.
API_KEY_SURFACE_SITES: set[tuple[str, str]] = {
    ("api/deps.py", "REST"),
    ("api/v1/llm_proxy.py", "LLM_PROXY"),
    ("api/v1/mcp_auth.py", "MCP"),
}

#: Modules that read ``key_hash`` off a personal key row, and why that is not a second resolution.
_KEY_HASH_READERS = {
    "services/api_key_access.py": "the one resolution",
    "main.py": "boot-time rotation of the ADMIN_TOKEN row: compares the configured token, authenticates nothing",
}

#: Modules whose code spells the ``fcp-`` prefix, and why.
_PREFIX_SPELLERS = {
    "services/api_key_access.py": "the prefix itself",
    "core/config.py": "validates the shape of ADMIN_TOKEN at startup",
    "api/v1/a2a_server.py": "refuses personal keys on the A2A surface",
}


def _docstring_nodes(tree: ast.Module) -> set[int]:
    found: set[int] = set()
    for node in app_sources.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(id(first.value))
    return found


def _imports_user_api_key(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom) and any(alias.name == "UserApiKey" for alias in node.names)
        for node in app_sources.walk(tree)
    )


def test_only_the_access_module_reads_a_personal_key_hash():
    readers = {
        path
        for path, tree in app_sources.modules()
        if _imports_user_api_key(tree)
        and any(
            isinstance(node, ast.Attribute) and node.attr == "key_hash" and isinstance(node.ctx, ast.Load)
            for node in app_sources.walk(tree)
        )
    }
    assert readers == set(_KEY_HASH_READERS), (
        "a personal API key is verified in services/api_key_access.py only (resolve_api_key): "
        f"{sorted(readers - set(_KEY_HASH_READERS))} read key_hash themselves"
    )


def test_no_other_module_spells_the_key_prefix():
    spellers = set()
    for path, tree in app_sources.modules():
        docstrings = _docstring_nodes(tree)
        if any(
            isinstance(node, ast.Constant) and node.value == "fcp-" and id(node) not in docstrings
            for node in app_sources.walk(tree)
        ):
            spellers.add(path)
    assert spellers == set(_PREFIX_SPELLERS), (
        "test a token with api_key_access.looks_like_api_key, never against the prefix: "
        f"{sorted(spellers - set(_PREFIX_SPELLERS))}"
    )


def _resolution_sites() -> set[tuple[str, str]]:
    sites: set[tuple[str, str]] = set()
    for path, tree in app_sources.modules():
        if path == "services/api_key_access.py":
            continue
        for node in app_sources.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name != "resolve_api_key":
                continue
            surface = next((kw.value for kw in node.keywords if kw.arg == "surface"), None)
            assert (
                isinstance(surface, ast.Attribute)
                and isinstance(surface.value, ast.Name)
                and surface.value.id == "ApiKeySurface"
            ), f"{path} line {node.lineno}: pass surface=ApiKeySurface.<SURFACE>, the surface this site guards"
            sites.add((path, surface.attr))
    return sites


def test_every_surface_accepting_a_key_is_a_known_site():
    sites = _resolution_sites()
    assert sites == API_KEY_SURFACE_SITES, (
        "a new surface resolving personal API keys must apply the key's operation checks "
        "(read-only, agents) and be listed in API_KEY_SURFACE_SITES"
    )
    assert {surface for _path, surface in sites} == {member.name for member in ApiKeySurface}


def test_every_mcp_endpoint_on_the_key_authentication_narrows_its_tools():
    narrowing = {"agents_for_key", "mcp_tools_for_key", "require_configuration_surface"}
    for path, tree in app_sources.modules():
        names = {
            node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            for node in app_sources.walk(tree)
            if isinstance(node, ast.Call)
        }
        if "authenticate_user" in names:
            assert names & narrowing, f"{path} authenticates API keys but never narrows its tools to the key"


def test_the_agent_scoped_rest_routes_exist():
    from fastapi.routing import APIRoute

    from app.main import application

    templates = {route.path for route in application.routes if isinstance(route, APIRoute)}
    assert access.AGENT_ROUTE in templates
    assert access.CONVERSATIONS_ROUTE in templates
    assert access.CONVERSATION_ROUTE in templates
    assert any(t.startswith(access.CONVERSATION_ROUTE + "/messages") for t in templates)


# ---------------------------------------------------------------------------
# MCP read lists
# ---------------------------------------------------------------------------

#: Read tools the name classifier cannot read as such, reviewed by hand.
_READ_DESPITE_THE_NAME = {
    "inspect_agent_traces": "reads LLM traces; 'inspect' is not one of the classifier's verbs",
    "load_skill": "returns a skill's instructions; changes nothing",
}


@pytest.mark.parametrize("provider", sorted(mcp_platform.MCP_TOOL_READ_LISTS))
def test_a_platform_read_list_stays_inside_its_allow_list(provider):
    reads = mcp_platform.MCP_TOOL_READ_LISTS[provider]
    assert reads <= mcp_platform.MCP_TOOL_ALLOW_LISTS[provider]
    assert not {name for name in reads if name.startswith("manage_")}, "an action-dispatched tool writes"
    assert {name for name in reads if infer_tool_type(name) != "read"} == set()


def test_the_xtm_read_list_names_tools_the_server_offers_and_reads_only():
    from app.services.builtin_tools import build_agent_builder_tool, build_flow_builder_tool, build_trace_tools
    from app.services.builtin_tools.improvements import build_improvement_tools

    offered = {d.name for d in build_agent_builder_tool(None, "")[0]}
    offered |= {d.name for d in build_flow_builder_tool(None, "00000000-0000-0000-0000-000000000000")[0]}
    offered |= {d.name for d in build_trace_tools(None, "u", None, allow_cross_agent=True)[0]}
    offered |= {"search_knowledge", "update_knowledge_document", "load_skill", "read_skill_resource"}
    # The improvement proposal tools (#8256), offered while CONCIERGE is on.
    offered |= {d.name for d in build_improvement_tools(None, "")[0]}
    from app.services.builtin_tools import build_concierge_diagnosis_tools, build_concierge_experiment_tools

    offered |= {d.name for d in build_concierge_diagnosis_tools("", read_only=True)[0]}
    offered |= {d.name for d in build_concierge_experiment_tools("", read_only=True)[0]}
    from app.services.builtin_tools import build_flow_lab_tools

    offered |= {d.name for d in build_flow_lab_tools("", read_only=True)[0]}
    from app.services.builtin_tools.health_orchestrator import build_concierge_orchestrator_tools

    offered |= {d.name for d in build_concierge_orchestrator_tools("")[0]}
    assert mcp_xtm.XTM_MCP_READ_TOOLS.issubset(offered)
    not_read = {
        name
        for name in mcp_xtm.XTM_MCP_READ_TOOLS
        if infer_tool_type(name) != "read" and name not in _READ_DESPITE_THE_NAME
    }
    assert not_read == set()
    for writer in (
        "create_agent",
        "update_agent",
        "delete_agent",
        "trigger_assignment",
        "update_knowledge_document",
        "create_flow",
        "update_flow_graph",
        "create_flow_event_type",
        "run_flow",
        "activate_flow",
        "deactivate_flow",
    ):
        assert writer not in mcp_xtm.XTM_MCP_READ_TOOLS


# ---------------------------------------------------------------------------
# Principals and scopes
# ---------------------------------------------------------------------------


def _row(**overrides):
    base = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "name": "CI",
        "key_prefix": "fcp-" + "0" * 8,
        "key_hash": "unused",
        "is_active": True,
        "surfaces": None,
        "read_only": False,
        "agent_ids": None,
        "expires_at": None,
        "rate_limit_per_minute": None,
        "quota_limit": None,
        "quota_period": None,
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _principal(**overrides) -> ApiKeyPrincipal:
    return ApiKeyPrincipal.from_row(_row(**overrides))


def _keyed_user(principal: ApiKeyPrincipal | None):
    user = SimpleNamespace(id=uuid.uuid4(), is_active=True)
    if principal is not None:
        user.api_key_principal = principal
    return user


def test_a_key_created_before_scopes_keeps_full_access():
    principal = _principal()
    assert all(principal.allows_surface(surface) for surface in access.API_KEY_SURFACES)
    assert not principal.read_only and not principal.agent_restricted
    assert principal.allows_agent(uuid.uuid4())
    assert access.has_full_scope(_row())


def test_a_stored_value_nothing_wrote_reaches_nothing():
    principal = _principal(surfaces="rest", agent_ids="all")
    assert not principal.allows_surface("rest")
    assert principal.agent_restricted and not principal.allows_agent(uuid.uuid4())


def test_agent_ids_compare_canonically():
    agent = uuid.uuid4()
    principal = _principal(agent_ids=[str(agent).upper()])
    assert principal.allows_agent(agent)
    assert principal.allows_agent(str(agent))
    assert not principal.allows_agent("not-a-uuid")


def test_full_scope_reads_the_surfaces_this_deployment_serves(monkeypatch):
    monkeypatch.setattr(access, "available_surfaces", lambda: ("rest", "mcp"))
    assert access.has_full_scope(_row(surfaces=["rest", "mcp"]))
    assert not access.has_full_scope(_row(surfaces=["rest"]))
    assert not access.has_full_scope(_row(read_only=True))
    assert not access.has_full_scope(_row(agent_ids=[str(uuid.uuid4())]))


def _request(method: str, route_path: str = "/api/v1/agents", path_params: dict | None = None, body: bytes = b""):
    scope = {
        "type": "http",
        "method": method,
        "path": route_path,
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
        "route": SimpleNamespace(path=route_path),
        "path_params": path_params or {},
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


class _ConversationLookup:
    def __init__(self, agent_id):
        self.agent_id = agent_id

    async def execute(self, _stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self.agent_id)


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
async def test_a_read_only_key_makes_safe_rest_requests(method):
    await access.enforce_rest_scope(_principal(read_only=True), _request(method), None)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_a_read_only_key_cannot_write_through_rest(method):
    with pytest.raises(HTTPException) as exc:
        await access.enforce_rest_scope(_principal(read_only=True), _request(method), None)
    assert exc.value.status_code == 403
    assert "read-only" in exc.value.detail


async def test_a_search_posted_by_a_read_only_key_is_refused_too():
    # Conservative on purpose: the method, never the route, decides.
    request = _request("POST", "/api/v1/knowledge-bases/{kb_id}/search", {"kb_id": str(uuid.uuid4())})
    with pytest.raises(HTTPException) as exc:
        await access.enforce_rest_scope(_principal(read_only=True), request, None)
    assert exc.value.status_code == 403


async def test_an_agent_restricted_key_reaches_its_agent_routes_only():
    agent, other = uuid.uuid4(), uuid.uuid4()
    principal = _principal(agent_ids=[str(agent)])
    await access.enforce_rest_scope(principal, _request("GET", access.AGENT_ROUTE, {"agent_id": str(agent)}), None)
    await access.enforce_rest_scope(
        principal, _request("GET", access.AGENT_ROUTE + "/persona/versions", {"agent_id": str(agent)}), None
    )
    for request in (
        _request("GET", access.AGENT_ROUTE, {"agent_id": str(other)}),
        _request("GET", "/api/v1/agents"),
        _request("GET", "/api/v1/integrations"),
        _request("GET", "/api/v1/settings/security-options"),
    ):
        with pytest.raises(HTTPException) as exc:
            await access.enforce_rest_scope(principal, request, None)
        assert exc.value.status_code == 403
        assert exc.value.detail == access.AGENT_RESTRICTED_DETAIL


async def test_an_agent_restricted_key_starts_conversations_with_its_agents_only():
    agent = uuid.uuid4()
    principal = _principal(agent_ids=[str(agent)])
    allowed = _request("POST", access.CONVERSATIONS_ROUTE, body=f'{{"agent_id": "{agent}"}}'.encode())
    await access.enforce_rest_scope(principal, allowed, None)
    for body in (f'{{"agent_id": "{uuid.uuid4()}"}}'.encode(), b"{}", b"not json"):
        with pytest.raises(HTTPException):
            await access.enforce_rest_scope(principal, _request("POST", access.CONVERSATIONS_ROUTE, body=body), None)


async def test_an_agent_restricted_key_reaches_the_conversations_of_its_agents_only():
    agent = uuid.uuid4()
    principal = _principal(agent_ids=[str(agent)])
    route = access.CONVERSATION_ROUTE + "/messages"
    params = {"conversation_id": str(uuid.uuid4())}
    await access.enforce_rest_scope(principal, _request("POST", route, params), _ConversationLookup(agent))
    for owner_agent in (uuid.uuid4(), None):
        with pytest.raises(HTTPException):
            await access.enforce_rest_scope(principal, _request("POST", route, params), _ConversationLookup(owner_agent))
    with pytest.raises(HTTPException):
        await access.enforce_rest_scope(
            principal, _request("GET", route, {"conversation_id": "nope"}), _ConversationLookup(agent)
        )


async def test_an_agent_restricted_key_cannot_mention_another_agent_in_its_conversation():
    agent = uuid.uuid4()
    principal = _principal(agent_ids=[str(agent)])
    route = access.CONVERSATION_ROUTE + "/messages"
    params = {"conversation_id": str(uuid.uuid4())}
    named = _request("POST", route, params, body=f'{{"content": "hi", "mentioned_agent_ids": ["{agent}"]}}'.encode())
    await access.enforce_rest_scope(principal, named, _ConversationLookup(agent))
    other = _request(
        "POST", route, params, body=f'{{"content": "hi", "mentioned_agent_ids": ["{uuid.uuid4()}"]}}'.encode()
    )
    with pytest.raises(HTTPException) as exc:
        await access.enforce_rest_scope(principal, other, _ConversationLookup(agent))
    assert exc.value.detail == access.AGENT_RESTRICTED_DETAIL


async def test_an_agent_restricted_key_cannot_mention_another_agent():
    agent, other = uuid.uuid4(), uuid.uuid4()
    principal = _principal(agent_ids=[str(agent)])
    route = access.CONVERSATION_ROUTE + "/messages"
    params = {"conversation_id": str(uuid.uuid4())}
    allowed = f'{{"content": "hi", "mentioned_agent_ids": ["{agent}"]}}'.encode()
    await access.enforce_rest_scope(principal, _request("POST", route, params, allowed), _ConversationLookup(agent))
    mention = f'{{"content": "hi", "mentioned_agent_ids": ["{other}"]}}'.encode()
    with pytest.raises(HTTPException) as exc:
        await access.enforce_rest_scope(principal, _request("POST", route, params, mention), _ConversationLookup(agent))
    assert exc.value.detail == access.AGENT_RESTRICTED_DETAIL


async def test_without_a_request_only_an_unrestricted_key_passes():
    await access.enforce_rest_scope(_principal(), None, None)
    with pytest.raises(HTTPException):
        await access.enforce_rest_scope(_principal(read_only=True), None, None)


def test_running_an_agent_needs_a_read_write_key_that_names_it():
    agent = uuid.uuid4()
    access.require_agent_run(_keyed_user(None), agent)
    access.require_agent_run(_keyed_user(_principal(agent_ids=[str(agent)])), agent)
    with pytest.raises(HTTPException) as exc:
        access.require_agent_run(_keyed_user(_principal(read_only=True)), agent)
    assert "read-only" in exc.value.detail
    with pytest.raises(HTTPException) as exc:
        access.require_agent_run(_keyed_user(_principal(agent_ids=[str(uuid.uuid4())])), agent)
    assert exc.value.detail == access.AGENT_RESTRICTED_DETAIL


def test_a_key_restricted_to_agents_reaches_no_raw_model_and_no_configuration_surface():
    restricted = _keyed_user(_principal(agent_ids=[str(uuid.uuid4())]))
    for check in (access.require_model_access, access.require_configuration_surface):
        with pytest.raises(HTTPException):
            check(restricted)
        check(_keyed_user(_principal(read_only=True)))
        check(_keyed_user(None))
    assert not access.lists_raw_models(restricted)


def test_the_agents_and_tools_a_key_keeps():
    agents = [SimpleNamespace(id=uuid.uuid4()) for _ in range(3)]
    assert access.agents_for_key(_keyed_user(None), agents) == agents
    assert access.agents_for_key(_keyed_user(_principal(read_only=True)), agents) == []
    assert access.agents_for_key(_keyed_user(_principal(agent_ids=[str(agents[1].id)])), agents) == [agents[1]]
    tools = [SimpleNamespace(name="get_x"), SimpleNamespace(name="delete_x")]
    assert access.mcp_tools_for_key(_keyed_user(None), tools, frozenset({"get_x"})) == tools
    assert access.mcp_tools_for_key(_keyed_user(_principal()), tools, frozenset({"get_x"})) == tools
    assert access.mcp_tools_for_key(_keyed_user(_principal(read_only=True)), tools, frozenset({"get_x"})) == [tools[0]]


_UNLIMITED = {"rate_limit_per_minute": None, "quota_limit": None, "quota_period": None}


def test_a_key_never_makes_a_wider_key():
    agent = str(uuid.uuid4())
    soon = datetime.now(UTC) + timedelta(days=3)
    caller = _keyed_user(_principal(surfaces=["mcp"], agent_ids=[agent], expires_at=soon))
    same = {"surfaces": ["mcp"], "read_only": False, "agent_ids": [agent], "expires_at": soon, **_UNLIMITED}
    access.require_within_caller_key(caller, **same, creates=True)
    for widened in (
        {"surfaces": ["mcp", "rest"]},
        {"surfaces": None},
        {"agent_ids": None},
        {"agent_ids": [agent, str(uuid.uuid4())]},
        {"expires_at": None},
        {"expires_at": soon + timedelta(days=1)},
    ):
        for creates in (True, False):
            with pytest.raises(HTTPException):
                access.require_within_caller_key(caller, **{**same, **widened}, creates=creates)
    # A session (no key) creates whatever it likes; a full-scope key too.
    everything = {"surfaces": None, "read_only": False, "agent_ids": None, "expires_at": None, **_UNLIMITED}
    access.require_within_caller_key(_keyed_user(None), **everything, creates=True)
    access.require_within_caller_key(_keyed_user(_principal()), **everything, creates=True)


def test_a_limited_key_makes_no_key_and_never_loosens_its_limits():
    caller = _keyed_user(_principal(rate_limit_per_minute=10, quota_limit=100, quota_period="daily"))
    scopes = {"surfaces": None, "read_only": False, "agent_ids": None, "expires_at": None}
    limits = {"rate_limit_per_minute": 10, "quota_limit": 100, "quota_period": "daily"}
    # Every key brings a budget of its own: a limited key would multiply its own by minting.
    with pytest.raises(HTTPException) as refused:
        access.require_within_caller_key(caller, **scopes, **limits, creates=True)
    assert "cannot create" in refused.value.detail
    # Editing (itself or a sibling) within its limits is fine, tighter too.
    access.require_within_caller_key(caller, **scopes, **limits, creates=False)
    access.require_within_caller_key(
        caller, **scopes, rate_limit_per_minute=5, quota_limit=50, quota_period="daily", creates=False
    )
    for loosened in (
        {"rate_limit_per_minute": None},
        {"rate_limit_per_minute": 11},
        {"quota_limit": None, "quota_period": None},
        {"quota_limit": 101},
        {"quota_period": "monthly"},
    ):
        with pytest.raises(HTTPException):
            access.require_within_caller_key(caller, **scopes, **{**limits, **loosened}, creates=False)
    # A rate limit alone bounds the rate, not the quota, and still forbids minting.
    rate_only = _keyed_user(_principal(rate_limit_per_minute=10))
    access.require_within_caller_key(
        rate_only, **scopes, rate_limit_per_minute=10, quota_limit=None, quota_period=None, creates=False
    )
    with pytest.raises(HTTPException):
        access.require_within_caller_key(rate_only, **scopes, **_UNLIMITED, creates=False)
    with pytest.raises(HTTPException):
        access.require_within_caller_key(
            rate_only, **scopes, rate_limit_per_minute=10, quota_limit=None, quota_period=None, creates=True
        )


# ---------------------------------------------------------------------------
# The resolution
# ---------------------------------------------------------------------------


class _Rows:
    """``resolve_api_key``'s two reads: the candidate keys, then the owner."""

    def __init__(self, row, owner):
        self._results = [
            SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [row])),
            SimpleNamespace(scalar_one_or_none=lambda: owner),
        ]

    async def execute(self, _stmt):
        return self._results.pop(0)


class _FakeRedis:
    """Counters, hashes and the two Lua scripts the resolution runs."""

    def __init__(self):
        self.values: dict[str, int] = {}
        self.hashes: dict[str, dict] = {}

    async def eval(self, script, _numkeys, key, *args):
        if script == rate_limit.INCR_EXPIRE_LUA:
            self.values[key] = self.values.get(key, 0) + 1
            return self.values[key]
        limit = int(args[0])
        if self.values.get(key, 0) >= limit:
            return -1
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def ttl(self, _key):
        return 30

    async def get(self, key):
        return self.values.get(key)

    def pipeline(self):
        redis = self
        calls: list = []

        class _Pipe:
            def hincrby(self, key, field, amount):
                calls.append(lambda: redis.hashes.setdefault(key, {}).update(
                    {field: redis.hashes.get(key, {}).get(field, 0) + amount}
                ))

            def hset(self, key, field, value):
                calls.append(lambda: redis.hashes.setdefault(key, {}).update({field: value}))

            def expire(self, *_args):
                calls.append(lambda: None)

            def get(self, key):
                calls.append(lambda: redis.values.get(key))

            async def execute(self):
                return [call() for call in calls]

        return _Pipe()


@pytest.fixture
def fake_redis(monkeypatch):
    redis = _FakeRedis()

    async def _client():
        return redis

    monkeypatch.setattr(access, "redis_client", _client)
    monkeypatch.setattr(rate_limit, "redis_client", _client)
    return redis


@pytest.fixture
def any_key_matches(monkeypatch):
    async def _match(*_args, **_kwargs):
        return True

    monkeypatch.setattr(access, "bcrypt_checkpw_async", _match)


async def _resolve(row, owner=None, surface=ApiKeySurface.REST):
    owner = owner or SimpleNamespace(id=row.user_id, is_active=True)
    return await access.resolve_api_key("fcp-1234567890abcdef", _Rows(row, owner), surface=surface)


async def test_a_resolved_key_tags_its_owner(fake_redis, any_key_matches):
    row = _row()
    user = await _resolve(row)
    assert user.auth_credential_kind == "api_key"
    assert access.principal_of(user).key_id == row.id
    assert fake_redis.hashes[f"{access.APIKEY_STATS_PREFIX}{row.id}"]["count"] == 1


@pytest.mark.parametrize("surface", list(ApiKeySurface))
async def test_an_expired_key_is_refused_on_every_surface(fake_redis, any_key_matches, surface):
    row = _row(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(HTTPException) as exc:
        await _resolve(row, surface=surface)
    assert (exc.value.status_code, exc.value.detail) == (401, access.EXPIRED_KEY_DETAIL)


async def test_an_expiry_without_a_zone_reads_as_utc(fake_redis, any_key_matches):
    with pytest.raises(HTTPException):
        await _resolve(_row(expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)))
    await _resolve(_row(expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)))


async def test_a_key_of_a_deactivated_owner_is_refused(fake_redis, any_key_matches):
    row = _row()
    with pytest.raises(HTTPException) as exc:
        await _resolve(row, owner=SimpleNamespace(id=row.user_id, is_active=False))
    assert exc.value.status_code == 401


async def test_a_proxy_only_key_cannot_call_rest_or_mcp(fake_redis, any_key_matches):
    row = _row(surfaces=["llm_proxy"])
    for surface in (ApiKeySurface.REST, ApiKeySurface.MCP):
        with pytest.raises(HTTPException) as exc:
            await _resolve(row, surface=surface)
        assert exc.value.status_code == 403
    await _resolve(row, surface=ApiKeySurface.LLM_PROXY)


async def test_a_refused_surface_counts_against_nothing(fake_redis, any_key_matches):
    row = _row(surfaces=["mcp"], quota_limit=1, quota_period="daily")
    with pytest.raises(HTTPException):
        await _resolve(row, surface=ApiKeySurface.REST)
    await _resolve(row, surface=ApiKeySurface.MCP)


async def test_a_custom_unauthorized_answer_is_used(fake_redis, any_key_matches):
    class _Mcp401Error(Exception):
        pass

    def _refuse(detail):
        raise _Mcp401Error(detail)

    row = _row(expires_at=datetime.now(UTC) - timedelta(days=1))
    with pytest.raises(_Mcp401Error):
        await access.resolve_api_key(
            "fcp-1234567890abcdef", _Rows(row, None), surface=ApiKeySurface.MCP, unauthorized=_refuse
        )


async def test_a_key_over_its_rate_limit_gets_a_429_while_its_sibling_works(fake_redis, any_key_matches):
    owner = uuid.uuid4()
    limited = _row(user_id=owner, rate_limit_per_minute=2)
    sibling = _row(user_id=owner)
    await _resolve(limited)
    await _resolve(limited)
    with pytest.raises(HTTPException) as exc:
        await _resolve(limited)
    assert exc.value.status_code == 429
    assert "rate limit" in exc.value.detail
    assert exc.value.headers["Retry-After"]
    for _ in range(5):
        await _resolve(sibling)


async def test_a_key_over_its_quota_gets_a_429_while_its_sibling_works(fake_redis, any_key_matches):
    owner = uuid.uuid4()
    limited = _row(user_id=owner, quota_limit=2, quota_period="monthly")
    sibling = _row(user_id=owner, quota_limit=5, quota_period="monthly")
    await _resolve(limited)
    await _resolve(limited)
    for _ in range(3):
        with pytest.raises(HTTPException) as exc:
            await _resolve(limited)
        assert exc.value.status_code == 429
        assert "quota" in exc.value.detail
    # A refusal is never counted: the period's counter stays at the limit.
    assert fake_redis.values[access.quota_counter_key(limited.id, "monthly")] == 2
    await _resolve(sibling)
    assert await access.read_quota_usage([limited, sibling]) == {str(limited.id): 2, str(sibling.id): 1}


@pytest.mark.parametrize(("closed", "status"), [(True, 503), (False, None)])
async def test_a_quota_the_store_cannot_count_follows_the_owner_quotas_fail_mode(
    monkeypatch, any_key_matches, closed, status
):
    from app.services import quota

    async def _down():
        raise ConnectionError("redis down")

    monkeypatch.setattr(access, "redis_client", _down)
    monkeypatch.setattr(quota, "quota_fail_closed", lambda: closed)
    row = _row(quota_limit=10, quota_period="daily")
    if status is None:
        await _resolve(row)
        return
    with pytest.raises(HTTPException) as exc:
        await _resolve(row)
    assert exc.value.status_code == status


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


def test_the_notice_a_key_is_inside():
    now = datetime(2026, 10, 1, tzinfo=UTC)
    assert access.due_expiry_notice(None, now) is None
    assert access.due_expiry_notice(now + timedelta(days=30), now) is None
    assert access.due_expiry_notice(now + timedelta(days=7), now) == 7
    assert access.due_expiry_notice(now + timedelta(days=3), now) == 7
    assert access.due_expiry_notice(now + timedelta(hours=20), now) == 1
    assert access.due_expiry_notice(now - timedelta(hours=1), now) is None


def test_the_maximum_lifetime_bounds_new_expiries_and_reports_old_ones(monkeypatch):
    now = datetime(2026, 10, 1, tzinfo=UTC)
    monkeypatch.setattr(access, "max_lifetime_days", lambda: 0)
    assert access.latest_allowed_expiry(now) is None
    assert not access.beyond_max_lifetime(None, now)
    monkeypatch.setattr(access, "max_lifetime_days", lambda: 90)
    assert access.latest_allowed_expiry(now) == now + timedelta(days=90)
    assert access.beyond_max_lifetime(None, now)
    assert access.beyond_max_lifetime(now + timedelta(days=91), now)
    assert not access.beyond_max_lifetime(now + timedelta(days=90), now)
    # Measured from now, like a re-dated expiry: a key created long ago and given
    # an expiry the bound allows today is no longer reported.
    later = now + timedelta(days=400)
    assert not access.beyond_max_lifetime(later + timedelta(days=30), later)


def test_a_daily_period_ends_at_the_next_utc_midnight_on_every_day_of_the_month():
    from app.services.quota import seconds_until_period_reset

    for last_hour in (
        datetime(2026, 1, 15, 23, tzinfo=UTC),
        datetime(2026, 1, 28, 23, tzinfo=UTC),
        datetime(2026, 1, 31, 23, tzinfo=UTC),
        datetime(2026, 2, 28, 23, tzinfo=UTC),
        datetime(2026, 12, 31, 23, tzinfo=UTC),
    ):
        assert seconds_until_period_reset("daily", last_hour) == 3600
    assert seconds_until_period_reset("daily", datetime(2026, 1, 30, 12, tzinfo=UTC)) == 12 * 3600
    assert seconds_until_period_reset("daily", datetime(2026, 1, 30, 23, 59, 30, tzinfo=UTC)) == 60


async def test_a_key_is_not_deleted_while_impersonating():
    from app.api.v1 import api_keys

    impersonated = SimpleNamespace(id=uuid.uuid4(), impersonated_by=uuid.uuid4())
    with pytest.raises(HTTPException) as refused:
        await api_keys.delete_api_key(str(uuid.uuid4()), request=None, user=impersonated, db=None)
    assert refused.value.status_code == 403


def test_the_maximum_lifetime_is_a_security_option(monkeypatch):
    from app.core import security_options
    from app.core.security_controls import get_control

    option = security_options.SECURITY_OPTIONS["api_key_max_lifetime_days"]
    assert (option.minimum, option.maximum, option.default()) == (0, 3650, 0)
    control = get_control("authentication.api_key_max_lifetime")
    assert control.setting == "api_key_max_lifetime_days"


async def test_a_read_only_key_runs_no_agent_from_a_batch_on_the_user_endpoint(monkeypatch):
    """The read-only refusal of ``tools/call`` holds for every message of a batch."""
    import json
    from unittest.mock import AsyncMock

    from starlette.datastructures import Headers

    from app.api.v1 import mcp_endpoint

    dispatched: list[str] = []

    async def _dispatch(body, agents, db, user_id):
        dispatched.append(body.method)
        return {"jsonrpc": "2.0", "id": body.id, "result": {}}

    async def _allowed(*_args, **_kwargs):
        return True

    async def _keyed(_request, _db, _resource_path):
        return _keyed_user(_principal(read_only=True))

    async def _no_agents(*_args, **_kwargs):
        return []

    class _BatchRequest:
        headers = Headers({"mcp-protocol-version": "2025-03-26"})

        async def json(self):
            return [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "any-agent"}},
            ]

    monkeypatch.setattr("app.services.ai_state.is_ai_enabled", _allowed)
    monkeypatch.setattr("app.api.v1.platform.is_chat_licensed", _allowed)
    monkeypatch.setattr(mcp_endpoint, "require_valid_origin", lambda _request: None)
    monkeypatch.setattr(mcp_endpoint, "authenticate_user", _keyed)
    monkeypatch.setattr(mcp_endpoint, "_get_user_agents", _no_agents)
    monkeypatch.setattr(mcp_endpoint, "_dispatch_mcp", _dispatch)

    response = await mcp_endpoint.mcp_user_endpoint(_BatchRequest(), db=SimpleNamespace(commit=AsyncMock()))

    answers = json.loads(response.body)
    assert dispatched == ["tools/list"]
    assert [answer["id"] for answer in answers] == [1, 2]
    assert answers[1]["error"]["code"] == -32602
    assert "read-only" in answers[1]["error"]["message"]
