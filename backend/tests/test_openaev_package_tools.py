"""Tests for the OpenAEV provider package and its consolidated management tools.

Covers the package refactor (build_tools still exposes the legacy tools) and
the new domain tools: presence, valid JSON schemas, every tool has a handler,
and executor dispatch smoke (early-return paths, no network).
"""

import json

import pytest

from app.integrations.providers.openaev import build_tools
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.openaev.io||test-token"

EXPECTED_NEW_TOOLS = {
    "manage_openaev_simulation",
    "manage_openaev_scenario",
    "manage_openaev_inject",
    "manage_openaev_expectation",
    "manage_openaev_atomic_testing",
    "manage_openaev_payload",
    "manage_openaev_threat_arsenal",
    "manage_openaev_phishing",
    "manage_openaev_asset",
    "manage_openaev_asset_group",
    "manage_openaev_security_platform",
    "manage_openaev_team",
    "manage_openaev_player",
    "manage_openaev_organization",
    "manage_openaev_user",
    "manage_openaev_group",
    "manage_openaev_role",
    "manage_openaev_profile",
    "manage_openaev_document",
    "manage_openaev_channel",
    "manage_openaev_article",
    "manage_openaev_challenge",
    "manage_openaev_lessons",
    "manage_openaev_reporting",
    "manage_openaev_dashboard",
    "query_openaev_analytics",
    "manage_openaev_notification",
    "query_openaev_attack_path",
    "query_openaev_chaining",
    "manage_openaev_tag",
    "manage_openaev_kill_chain_phase",
    "manage_openaev_attack_pattern",
    "manage_openaev_variable",
    "manage_openaev_finding",
    "manage_openaev_vulnerability",
    "query_openaev_connectors",
    "query_openaev_catalog",
    "manage_openaev_connector_instance",
    "manage_openaev_settings",
    "export_openaev_scenario",
    "import_openaev_scenario",
}

LEGACY_SAMPLE = {
    "search_openaev_assets",
    "create_openaev_scenario",
    "create_openaev_atomic_testing",
    "search_openaev_exercises",
    "full_text_search_openaev",
}


def _build():
    executor = CallableToolExecutor()
    tools = build_tools(TOKEN, executor)
    return executor, tools


def test_new_tools_present_and_legacy_preserved():
    _, tools = _build()
    names = {t.name for t in tools}
    missing_new = EXPECTED_NEW_TOOLS - names
    assert not missing_new, f"missing new tools: {missing_new}"
    missing_legacy = LEGACY_SAMPLE - names
    assert not missing_legacy, f"refactor dropped legacy tools: {missing_legacy}"


def test_no_duplicate_tool_names():
    _, tools = _build()
    names = [t.name for t in tools]
    assert len(names) == len(set(names)), "duplicate tool names registered"


def test_all_new_tool_schemas_are_valid():
    import json

    _, tools = _build()
    by_name = {t.name: t for t in tools}
    for name in EXPECTED_NEW_TOOLS:
        tool = by_name[name]
        assert tool.source == "integration:openaev"
        schema = tool.input_schema
        assert schema.get("type") == "object", name
        assert isinstance(schema.get("properties"), dict), name
        json.dumps(schema)


def test_every_tool_has_a_handler():
    executor, tools = _build()
    for tool in tools:
        assert tool.name in executor._handlers, f"no handler for {tool.name}"


def test_empty_base_url_returns_no_tools():
    executor = CallableToolExecutor()
    tools = build_tools("||token-only", executor)
    assert tools == []


# ── Dispatch smoke (early-return validation paths only — no network) ──────

SMOKE_CALLS = [
    ("manage_openaev_simulation", {"action": "__smoke__"}),
    ("manage_openaev_scenario", {"action": "__smoke__"}),
    ("manage_openaev_inject", {"action": "__smoke__", "parent_id": "x"}),
    ("manage_openaev_expectation", {"action": "__smoke__"}),
    ("manage_openaev_atomic_testing", {"action": "__smoke__"}),
    ("manage_openaev_payload", {"action": "__smoke__"}),
    ("manage_openaev_threat_arsenal", {"action": "__smoke__"}),
    ("manage_openaev_phishing", {"action": "__smoke__"}),
    ("manage_openaev_asset", {"action": "__smoke__"}),
    ("manage_openaev_asset_group", {"action": "__smoke__"}),
    ("manage_openaev_security_platform", {"action": "__smoke__"}),
    ("manage_openaev_team", {"action": "__smoke__"}),
    ("manage_openaev_player", {"action": "__smoke__"}),
    ("manage_openaev_organization", {"action": "__smoke__"}),
    ("manage_openaev_user", {"action": "__smoke__"}),
    ("manage_openaev_group", {"action": "__smoke__"}),
    ("manage_openaev_role", {"action": "__smoke__"}),
    ("manage_openaev_profile", {"action": "__smoke__"}),
    ("manage_openaev_document", {"action": "__smoke__"}),
    ("manage_openaev_channel", {"action": "__smoke__"}),
    ("manage_openaev_article", {"action": "__smoke__"}),
    ("manage_openaev_challenge", {"action": "__smoke__"}),
    ("manage_openaev_lessons", {"action": "__smoke__"}),
    ("manage_openaev_reporting", {"action": "__smoke__"}),
    ("manage_openaev_dashboard", {"action": "__smoke__"}),
    ("query_openaev_analytics", {"kind": "__smoke__"}),
    ("manage_openaev_notification", {"action": "__smoke__"}),
    ("manage_openaev_tag", {"action": "__smoke__"}),
    ("manage_openaev_kill_chain_phase", {"action": "__smoke__"}),
    ("manage_openaev_attack_pattern", {"action": "__smoke__"}),
    ("manage_openaev_variable", {"action": "__smoke__"}),
    ("manage_openaev_finding", {"action": "__smoke__"}),
    ("manage_openaev_vulnerability", {"action": "__smoke__"}),
    ("query_openaev_connectors", {"kind": "bogus"}),
    ("query_openaev_catalog", {"action": "__smoke__"}),
    ("manage_openaev_connector_instance", {"action": "__smoke__"}),
    ("manage_openaev_settings", {"action": "__smoke__"}),
    ("export_openaev_scenario", {}),  # scenario_id missing -> early return
    ("import_openaev_scenario", {}),  # file_id missing -> early return
]


async def test_consolidated_dispatch_smoke():
    executor, _ = _build()
    for idx, (name, args) in enumerate(SMOKE_CALLS):
        result = await executor.execute_tool(
            ToolCall(id=f"call-{idx}", name=name, arguments=args)
        )
        assert not result.is_error, f"{name} raised: {result.content}"
        assert isinstance(result.content, str) and result.content, name


# ── Regression tests: partial-update safety + config dict validation ──────


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _RecordingApi:
    """Stub for `_api` that records calls and returns a canned response.

    Domain modules call `_api(base_url, headers, method, path, action, json=,
    params=)` through their module-level import, so patching e.g. `access._api`
    reroutes every REST call in that module through this stub (no network).
    """

    def __init__(self, data=None):
        self._data = {} if data is None else data
        self.calls: list[dict] = []

    async def __call__(
        self, base_url, headers, method, path, action, *, json=None, params=None, **kw
    ):
        self.calls.append({"method": method, "path": path, "json": json})
        return _FakeResp(self._data), None

    def last_json(self) -> dict:
        # The last call that sent a body: a write may be followed by a
        # read-back GET (atomic tests read their stored targets, #660).
        sent = [c["json"] for c in self.calls if c["json"] is not None]
        return sent[-1] if sent else {}


async def _run(executor, name, arguments):
    return await executor.execute_tool(
        ToolCall(id=f"call-{name}", name=name, arguments=arguments)
    )


async def test_user_update_omits_admin_when_not_provided(monkeypatch):
    from app.integrations.providers.openaev import access

    executor, _ = _build()
    fake = _RecordingApi({"user_id": "u1", "user_email": "a@x.io"})
    monkeypatch.setattr(access, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_user",
        {"action": "update", "user_id": "u1", "firstname": "Al"},
    )
    assert not result.is_error, result.content
    body = fake.last_json()
    assert "user_admin" not in body  # not revoked when omitted
    assert body.get("user_firstname") == "Al"


async def test_user_update_includes_admin_when_provided(monkeypatch):
    from app.integrations.providers.openaev import access

    executor, _ = _build()
    fake = _RecordingApi({"user_id": "u1"})
    monkeypatch.setattr(access, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_user",
        {"action": "update", "user_id": "u1", "admin": True},
    )
    assert not result.is_error, result.content
    assert fake.last_json().get("user_admin") is True


async def test_group_update_omits_default_user_assign(monkeypatch):
    from app.integrations.providers.openaev import access

    executor, _ = _build()
    fake = _RecordingApi({"group_id": "g1"})
    monkeypatch.setattr(access, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_group",
        {"action": "update", "group_id": "g1", "name": "Blue"},
    )
    assert not result.is_error, result.content
    assert "group_default_user_assign" not in fake.last_json()


async def test_team_update_omits_contextual(monkeypatch):
    from app.integrations.providers.openaev import teams

    executor, _ = _build()
    fake = _RecordingApi({"team_id": "t1"})
    monkeypatch.setattr(teams, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_team",
        {"action": "update", "team_id": "t1", "name": "Red"},
    )
    assert not result.is_error, result.content
    assert "team_contextual" not in fake.last_json()


async def test_manage_player_orchestrator_cannot_update_delete_or_upsert(monkeypatch):
    # manage_openaev_player is a full CRUD tool. The autonomous orchestrator is
    # granted it ONLY to MINT a person (create - idempotent mint-or-resolve) for
    # ensure_openaev_target_team; it must never update, delete, or upsert arbitrary
    # OpenAEV users. upsert is refused alongside update/delete because
    # /api/players/upsert rewrites an existing user matched by email (profile,
    # tags, team memberships). The guard is keyed to the orchestrator-run marker,
    # so chat / other agents keep the full CRUD contract.
    from app.integrations.providers.openaev import teams
    from app.services.builtin_tools.filtering import (
        reset_orchestrator_arsenal_writes,
        set_orchestrator_arsenal_writes,
    )

    executor, _ = _build()
    fake = _RecordingApi({"user_id": "p1", "user_email": "victim@corp.tld"})
    monkeypatch.setattr(teams, "_api", fake)
    token = set_orchestrator_arsenal_writes(True)
    try:
        for action, extra in (
            ("update", {"player_id": "p1", "firstname": "X"}),
            ("delete", {"player_id": "p1"}),
            # upsert against an existing email is the mutation path Copilot flagged:
            # it would rewrite victim@corp.tld's profile and team memberships.
            ("upsert", {"email": "victim@corp.tld", "firstname": "X"}),
        ):
            result = await _run(
                executor, "manage_openaev_player", {"action": action, **extra}
            )
            assert result.is_error, result.content
            assert "cannot" in result.content.lower()
            assert "GAP" in result.content
        # All three mutating actions were refused BEFORE any REST call left.
        assert not fake.calls
        # Minting via create (idempotent mint-or-resolve) stays allowed, and it
        # must hit the plain create endpoint, never the mutating upsert path.
        result = await _run(
            executor,
            "manage_openaev_player",
            {"action": "create", "email": "newhire@corp.tld"},
        )
        assert not result.is_error, result.content
        assert fake.calls, "create must reach the API for the orchestrator"
        assert fake.calls[-1]["path"] == "/api/players"
    finally:
        reset_orchestrator_arsenal_writes(token)


async def test_manage_player_orchestrator_cannot_bind_teams_on_create(monkeypatch):
    # Deeper least-privilege gap: even with create allowed to
    # MINT a person, team_ids is forwarded as user_teams to /api/players and would
    # let the orchestrator hand-stitch membership in arbitrary teams, bypassing the
    # run-scoped ensure_openaev_target_team gate (the ONLY sanctioned way for it to
    # bind a person to a delivery team). team_ids must be refused BEFORE any REST
    # call leaves; a bare create (no team_ids) still mints via /api/players.
    from app.integrations.providers.openaev import teams
    from app.services.builtin_tools.filtering import (
        reset_orchestrator_arsenal_writes,
        set_orchestrator_arsenal_writes,
    )

    executor, _ = _build()
    fake = _RecordingApi({"user_id": "p1", "user_email": "newhire@corp.tld"})
    monkeypatch.setattr(teams, "_api", fake)
    token = set_orchestrator_arsenal_writes(True)
    try:
        result = await _run(
            executor,
            "manage_openaev_player",
            {
                "action": "create",
                "email": "newhire@corp.tld",
                "team_ids": ["team-abc"],
            },
        )
        assert result.is_error, result.content
        assert "cannot" in result.content.lower()
        assert "ensure_openaev_target_team" in result.content
        # Refused before any REST call left - no membership was hand-stitched.
        assert not fake.calls
        # A bare mint (no team_ids) still reaches the plain create endpoint, and no
        # user_teams field is ever sent.
        result = await _run(
            executor,
            "manage_openaev_player",
            {"action": "create", "email": "newhire@corp.tld"},
        )
        assert not result.is_error, result.content
        assert fake.calls, "bare create must reach the API for the orchestrator"
        assert fake.calls[-1]["path"] == "/api/players"
        assert "user_teams" not in (fake.calls[-1]["json"] or {})
    finally:
        reset_orchestrator_arsenal_writes(token)


async def test_manage_player_allows_update_delete_outside_orchestrator_run(monkeypatch):
    # Without the orchestrator-run marker (normal chat / other agents), the full
    # CRUD contract is preserved - the guard must be a strict no-op by default,
    # including forwarding team_ids as user_teams to /api/players.
    from app.integrations.providers.openaev import teams

    executor, _ = _build()
    fake = _RecordingApi({"user_id": "p1"})
    monkeypatch.setattr(teams, "_api", fake)
    result = await _run(
        executor, "manage_openaev_player", {"action": "delete", "player_id": "p1"}
    )
    assert not result.is_error, result.content
    assert fake.calls, "delete must reach the API outside an orchestrator run"
    assert fake.calls[-1]["method"] == "delete"
    # team_ids must still flow for normal callers (create with membership).
    result = await _run(
        executor,
        "manage_openaev_player",
        {"action": "create", "email": "hire@corp.tld", "team_ids": ["team-abc"]},
    )
    assert not result.is_error, result.content
    assert fake.calls[-1]["path"] == "/api/players"
    assert (fake.calls[-1]["json"] or {}).get("user_teams") == ["team-abc"]


async def test_inject_update_keeps_the_stored_fields(monkeypatch):
    # OpenAEV's PUT replaces the whole inject (InjectInput): the tool reads it
    # and sends every stored field back, only the asked ones changed.
    from app.integrations.providers.openaev import injects

    executor, _ = _build()
    stored = {
        "inject_id": "i1",
        "inject_title": "t",
        "inject_enabled": False,
        "inject_all_teams": True,
        "inject_depends_duration": 60,
        "inject_injector_contract": {"injector_contract_id": "c1"},
        "inject_injector": "inj1",
        "inject_teams": ["t1"],
        "inject_assets": [{"asset_id": "a1"}],
        "inject_asset_groups": [],
        "inject_tags": ["tag1"],
        "inject_depends_on": [
            {
                "dependency_relationship": {"inject_parent_id": "p1", "inject_children_id": "i1"},
                "dependency_condition": {"mode": "and", "conditions": []},
            }
        ],
    }
    fake = _RecordingApi(stored)
    monkeypatch.setattr(injects, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_inject",
        {
            "action": "update",
            "scope": "scenario",
            "parent_id": "s1",
            "inject_id": "i1",
            "description": "d",
        },
    )
    assert not result.is_error, result.content
    assert [c["method"] for c in fake.calls] == ["get", "put"]
    body = fake.last_json()
    assert body["inject_description"] == "d"
    assert body["inject_title"] == "t"
    assert body["inject_enabled"] is False
    assert body["inject_all_teams"] is True
    assert body["inject_depends_duration"] == 60
    assert body["inject_injector_contract"] == "c1"
    assert body["inject_injector"] == "inj1"
    assert body["inject_teams"] == ["t1"]
    assert body["inject_assets"] == ["a1"]
    assert body["inject_tags"] == ["tag1"]
    assert body["inject_depends_on"] == [
        {
            "dependency_relationship": {"inject_parent_id": "p1"},
            "dependency_condition": {"mode": "and", "conditions": []},
        }
    ]


async def test_inject_update_of_an_unreadable_inject_writes_nothing(monkeypatch):
    from app.integrations.providers.openaev import injects

    executor, _ = _build()
    fake = _RecordingApi({"inject_id": "i1"})
    monkeypatch.setattr(injects, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_inject",
        {"action": "update", "scope": "scenario", "parent_id": "s1", "inject_id": "i1", "description": "d"},
    )
    assert "Nothing was written" in result.content
    assert [c["method"] for c in fake.calls] == ["get"]


async def test_inject_create_sends_defaults(monkeypatch):
    from app.integrations.providers.openaev import injects

    executor, _ = _build()
    fake = _RecordingApi({"inject_id": "i1", "inject_title": "T"})
    monkeypatch.setattr(injects, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_inject",
        {
            "action": "create",
            "scope": "scenario",
            "parent_id": "s1",
            "title": "T",
            "injector_contract_id": "c1",
        },
    )
    assert not result.is_error, result.content
    body = fake.last_json()
    assert body.get("inject_title") == "T"
    assert body.get("inject_enabled") is True
    assert body.get("inject_all_teams") is False


async def test_inject_create_chains_parent_into_attack_path(monkeypatch):
    from app.integrations.providers.openaev import injects

    executor, _ = _build()
    fake = _RecordingApi({"inject_id": "i2", "inject_title": "Exploit"})
    monkeypatch.setattr(injects, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_inject",
        {
            "action": "create",
            "scope": "simulation",
            "parent_id": "e1",
            "title": "Exploit",
            "injector_contract_id": "c1",
            "parent_inject_id": "recon-1",
        },
    )
    assert not result.is_error, result.content
    deps = fake.last_json().get("inject_depends_on")
    assert isinstance(deps, list) and len(deps) == 1
    dep = deps[0]
    assert dep["dependency_relationship"]["inject_parent_id"] == "recon-1"
    condition = dep["dependency_condition"]["conditions"][0]
    assert condition["key"] == "Execution"
    assert condition["value"] is True


async def test_inject_create_parent_condition_failure(monkeypatch):
    from app.integrations.providers.openaev import injects

    executor, _ = _build()
    fake = _RecordingApi({"inject_id": "i3", "inject_title": "Fallback"})
    monkeypatch.setattr(injects, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_inject",
        {
            "action": "create",
            "scope": "simulation",
            "parent_id": "e1",
            "title": "Fallback",
            "injector_contract_id": "c1",
            "parent_inject_id": "recon-1",
            "parent_condition": "failure",
        },
    )
    assert not result.is_error, result.content
    deps = fake.last_json().get("inject_depends_on")
    assert deps[0]["dependency_condition"]["conditions"][0]["value"] is False


async def test_inject_create_without_parent_omits_depends_on(monkeypatch):
    from app.integrations.providers.openaev import injects

    executor, _ = _build()
    fake = _RecordingApi({"inject_id": "i4", "inject_title": "Root"})
    monkeypatch.setattr(injects, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_inject",
        {
            "action": "create",
            "scope": "simulation",
            "parent_id": "e1",
            "title": "Root",
            "injector_contract_id": "c1",
        },
    )
    assert not result.is_error, result.content
    assert "inject_depends_on" not in fake.last_json()


async def test_inject_activation_requires_enabled():
    executor, _ = _build()
    result = await _run(
        executor,
        "manage_openaev_inject",
        {
            "action": "activation",
            "scope": "scenario",
            "parent_id": "s1",
            "inject_id": "i1",
        },
    )
    assert not result.is_error, result.content
    assert "enabled" in result.content


async def test_article_update_omits_published(monkeypatch):
    from app.integrations.providers.openaev import content

    executor, _ = _build()
    fake = _RecordingApi({"article_id": "a1"})
    monkeypatch.setattr(content, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_article",
        {
            "action": "update",
            "scope": "simulation",
            "parent_id": "e1",
            "article_id": "a1",
            "name": "X",
        },
    )
    assert not result.is_error, result.content
    assert "article_published" not in fake.last_json()


async def test_atomic_update_keeps_stored_targeting_when_not_provided(monkeypatch):
    from app.integrations.providers.openaev import testing

    executor, _ = _build()
    fake = _RecordingApi(
        {
            "inject_id": "i1",
            "inject_title": "Old",
            "inject_injector_contract": {"injector_contract_id": "c1"},
            "inject_assets": ["a1"],
            "inject_asset_groups": [],
            "inject_teams": [],
            "inject_tags": [],
        }
    )
    monkeypatch.setattr(testing, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_atomic_testing",
        {"action": "update", "inject_id": "i1", "title": "New"},
    )
    assert not result.is_error, result.content
    assert fake.calls[0]["method"] == "get"
    body = fake.last_json()
    assert body["inject_assets"] == ["a1"]
    assert body["inject_injector_contract"] == "c1"
    assert body.get("inject_title") == "New"


async def test_atomic_create_sends_full_shape(monkeypatch):
    from app.integrations.providers.openaev import testing

    executor, _ = _build()
    fake = _RecordingApi({"inject_id": "i1", "inject_title": "T"})
    monkeypatch.setattr(testing, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_atomic_testing",
        {"action": "create", "title": "T", "injector_contract_id": "c1"},
    )
    assert not result.is_error, result.content
    body = fake.last_json()
    assert body.get("inject_teams") == []
    assert body.get("inject_all_teams") is False
    assert body.get("inject_assets") == []


async def test_settings_rejects_non_object_config():
    executor, _ = _build()
    result = await _run(
        executor,
        "manage_openaev_settings",
        {"action": "update_policies", "config": "[1, 2, 3]"},
    )
    assert not result.is_error, result.content
    assert "JSON object" in result.content


async def test_team_upsert_message_uses_upserted(monkeypatch):
    from app.integrations.providers.openaev import teams

    executor, _ = _build()
    monkeypatch.setattr(
        teams, "_api", _RecordingApi({"team_id": "t1", "team_name": "Blue"})
    )
    result = await _run(
        executor, "manage_openaev_team", {"action": "upsert", "name": "Blue"}
    )
    assert not result.is_error, result.content
    assert "Upserted" in result.content
    assert "Upsertd" not in result.content


async def test_simulation_add_teams_allows_empty_list(monkeypatch):
    from app.integrations.providers.openaev import simulations

    executor, _ = _build()
    fake = _RecordingApi({})
    monkeypatch.setattr(simulations, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_simulation",
        {"action": "add_teams", "exercise_id": "e1", "team_ids": []},
    )
    assert not result.is_error, result.content
    # The explicit empty list reaches the /teams/replace endpoint (clears membership).
    assert fake.last_json() == {"exercise_teams": []}


async def test_inject_status_requires_global_scope():
    executor, _ = _build()
    result = await _run(
        executor,
        "manage_openaev_inject",
        {"action": "status", "scope": "scenario", "inject_id": "i1"},
    )
    assert not result.is_error, result.content
    assert "global" in result.content


async def test_inject_update_requires_a_field(monkeypatch):
    from app.integrations.providers.openaev import injects

    executor, _ = _build()
    fake = _RecordingApi({})
    monkeypatch.setattr(injects, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_inject",
        {
            "action": "update",
            "scope": "scenario",
            "parent_id": "s1",
            "inject_id": "i1",
        },
    )
    assert not result.is_error, result.content
    assert "at least one field" in result.content
    assert fake.calls == []  # no empty-body PUT was sent


async def test_token_whitespace_is_stripped(monkeypatch):
    # build_tools strips the api_token so `base_url|| token` cannot produce a
    # malformed `Authorization: Bearer  <token>` header.
    captured: dict = {}

    def fake_headers(token, refresh_fn=None):
        from app.integrations.http import RefreshableHeaders

        captured["token"] = token
        # build_tools hangs the tenant resolver on the headers object.
        return RefreshableHeaders(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )

    monkeypatch.setattr(
        "app.integrations.providers.openaev._headers", fake_headers
    )
    build_tools("https://demo.openaev.io||  spaced-token  ", CallableToolExecutor())
    assert captured["token"] == "spaced-token"


async def test_security_platform_output_uses_security_platform_id_label(monkeypatch):
    # A SecurityPlatform is an Asset, so the API returns `asset_id`; the tool
    # surfaces it under the `security_platform_id` label callers pass back.
    from app.integrations.providers.openaev import assets

    executor, _ = _build()
    monkeypatch.setattr(
        assets,
        "_api",
        _RecordingApi(
            {"content": [{"asset_id": "sp1", "asset_name": "Splunk", "security_platform_type": "SIEM"}]}
        ),
    )
    listed = await _run(
        executor, "manage_openaev_security_platform", {"action": "list"}
    )
    assert not listed.is_error, listed.content
    assert "security_platform_id: sp1" in listed.content
    assert "(id: sp1)" not in listed.content

    monkeypatch.setattr(
        assets, "_api", _RecordingApi({"asset_id": "sp1", "asset_name": "Splunk"})
    )
    created = await _run(
        executor,
        "manage_openaev_security_platform",
        {"action": "create", "name": "Splunk", "platform_type": "SIEM"},
    )
    assert not created.is_error, created.content
    assert "security_platform_id: sp1" in created.content


async def test_asset_update_keeps_the_fields_it_does_not_change(monkeypatch):
    # OpenAEV's PUT /api/endpoints/{id} replaces the whole endpoint: the tool
    # reads it first and sends every field back, only the asked ones changed.
    from app.integrations.providers.openaev import assets

    executor, _ = _build()
    current = {
        "asset_id": "e1",
        "asset_name": "shop.example.com",
        "asset_external_reference": "dns-enum:shop.example.com",
        "asset_hostname": "shop.example.com",
        "asset_ips": ["10.0.0.5"],
        "asset_tags": ["t1", {"tag_id": "t2"}],
        "asset_category": "HOST",
        "endpoint_platform": "Linux",
        "asset_type": "Endpoint",
    }
    fake = _RecordingApi(current)
    monkeypatch.setattr(assets, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_asset",
        {
            "action": "update",
            "asset_id": "e1",
            "category": "web_application",
            "subcategory": "website",
            "url": "https://shop.example.com",
        },
    )
    assert not result.is_error, result.content
    assert [c["method"] for c in fake.calls] == ["get", "put"]
    sent = fake.last_json()
    assert sent["asset_category"] == "WEB_APPLICATION"
    assert sent["asset_subcategory"] == "WEBSITE"
    assert sent["asset_url"] == "https://shop.example.com"
    assert sent["asset_external_reference"] == "dns-enum:shop.example.com"
    assert sent["asset_ips"] == ["10.0.0.5"]
    assert sent["asset_hostname"] == "shop.example.com"
    assert sent["asset_tags"] == ["t1", "t2"]
    assert sent["endpoint_platform"] == "Linux"
    assert "asset_type" not in sent and "asset_id" not in sent


async def test_asset_update_to_source_code_falls_back_on_an_older_openaev(monkeypatch):
    # xtm-one #1233: an OpenAEV that predates SOURCE_CODE refuses it; the tool
    # sends GENERIC_ASSET without a subcategory and says so.
    from app.integrations.providers.openaev import assets

    executor, _ = _build()
    puts: list[dict] = []

    async def fake(base_url, headers, method, path, action, *, json=None, params=None, **kw):
        if method == "get":
            return _FakeResp({"asset_id": "e1", "asset_name": "repo", "asset_category": "GENERIC_ASSET"}), None
        puts.append(dict(json))
        if json.get("asset_category") == "SOURCE_CODE":
            return None, (
                "Error: OpenAEV rejected the request to update endpoint — bad request. "
                'Cannot deserialize value of type `AssetCategory` from String "SOURCE_CODE"'
            )
        return _FakeResp({}), None

    monkeypatch.setattr(assets, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_asset",
        {"action": "update", "asset_id": "e1", "category": "source_code", "subcategory": "code_repository"},
    )
    assert not result.is_error, result.content
    assert [p["asset_category"] for p in puts] == ["SOURCE_CODE", "GENERIC_ASSET"]
    assert puts[0]["asset_subcategory"] == "CODE_REPOSITORY"
    assert "asset_subcategory" not in puts[1]
    assert "GENERIC_ASSET there" in result.content


@pytest.mark.parametrize(
    ("stored", "args", "error"),
    [
        # A server error on a category change: answered as it is.
        (
            "GENERIC_ASSET",
            {"category": "source_code"},
            "Error: OpenAEV server error (503) while trying to update endpoint. "
            "The service may be temporarily unavailable.",
        ),
        # Another field refused on a category change: answered as it is.
        (
            "GENERIC_ASSET",
            {"category": "source_code", "url": "not a url"},
            "Error: OpenAEV rejected the request to update endpoint — validation error. asset_url: must be a URL",
        ),
        # An endpoint already SOURCE_CODE, an unrelated change refused: never
        # downgraded (review 6013276149 on #1238).
        (
            "SOURCE_CODE",
            {"description": "new"},
            "Error: OpenAEV rejected the request to update endpoint — bad request. asset_category",
        ),
        # The same endpoint and a transient error.
        (
            "SOURCE_CODE",
            {"description": "new"},
            "Error: OpenAEV server error (502) while trying to update endpoint.",
        ),
    ],
)
async def test_asset_update_never_downgrades_on_another_error(monkeypatch, stored, args, error):
    # xtm-one #1233: only a rejection of the requested newer category falls back.
    from app.integrations.providers.openaev import assets

    executor, _ = _build()
    puts: list[dict] = []

    async def fake(base_url, headers, method, path, action, *, json=None, params=None, **kw):
        if method == "get":
            return _FakeResp({"asset_id": "e1", "asset_name": "repo", "asset_category": stored}), None
        puts.append(dict(json))
        return None, error

    monkeypatch.setattr(assets, "_api", fake)
    result = await _run(executor, "manage_openaev_asset", {"action": "update", "asset_id": "e1", **args})
    assert len(puts) == 1
    assert "GENERIC_ASSET" not in result.content
    assert error in result.content


async def test_asset_update_clears_the_named_fields(monkeypatch):
    # An explicit clear unsets a field; an empty string alone is still no change.
    from app.integrations.providers.openaev import assets

    executor, _ = _build()
    current = {
        "asset_id": "e1",
        "asset_name": "shop",
        "asset_url": "https://old.example",
        "asset_subcategory": "WEBSITE",
        "asset_internet_facing": True,
        "asset_ips": ["10.0.0.5"],
    }
    fake = _RecordingApi(current)
    monkeypatch.setattr(assets, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_asset",
        {"action": "update", "asset_id": "e1", "clear": ["url", "subcategory", "internet_facing", "ips"]},
    )
    assert not result.is_error, result.content
    sent = fake.last_json()
    assert sent["asset_url"] is None
    assert sent["asset_subcategory"] is None
    assert sent["asset_internet_facing"] is None
    assert sent["asset_ips"] == []
    assert sent["asset_name"] == "shop"


async def test_asset_update_refuses_an_unknown_clear_before_any_call(monkeypatch):
    from app.integrations.providers.openaev import assets

    executor, _ = _build()
    fake = _RecordingApi({})
    monkeypatch.setattr(assets, "_api", fake)
    result = await _run(executor, "manage_openaev_asset", {"action": "update", "asset_id": "e1", "clear": ["name"]})
    assert "unknown field(s): name" in result.content
    assert fake.calls == []


async def test_asset_bulk_update_clears_the_url(monkeypatch):
    from app.integrations.providers.openaev import assets

    executor, _ = _build()
    fake = _RecordingApi(["a1"])
    monkeypatch.setattr(assets, "_api", fake)
    result = await _run(
        executor, "manage_openaev_asset", {"action": "bulk_update", "asset_ids": ["a1"], "clear": ["url"]}
    )
    assert not result.is_error, result.content
    assert fake.last_json() == {"asset_ids_to_process": ["a1"], "asset_url": ""}


async def test_asset_group_update_keeps_the_stored_fields(monkeypatch):
    # OpenAEV's PUT replaces the whole group (AssetGroupInput): a dynamic filter
    # alone keeps the stored name, description and tags.
    from app.integrations.providers.openaev import assets

    executor, _ = _build()
    stored = {
        "asset_group_id": "ag1",
        "asset_group_name": "Web",
        "asset_group_description": "front",
        "asset_group_tags": ["t1", {"tag_id": "t2"}],
        "asset_group_dynamic_filter": {"mode": "and", "filters": []},
    }
    fake = _RecordingApi(stored)
    monkeypatch.setattr(assets, "_api", fake)
    group = {"mode": "and", "filters": [{"key": "asset_tags", "operator": "contains", "values": ["t1"], "mode": "or"}]}
    result = await _run(
        executor,
        "manage_openaev_asset_group",
        {"action": "update", "asset_group_id": "ag1", "dynamic_filter": json.dumps(group)},
    )
    assert not result.is_error, result.content
    assert [c["method"] for c in fake.calls] == ["get", "put"]
    assert fake.last_json() == {
        "asset_group_name": "Web",
        "asset_group_description": "front",
        "asset_group_tags": ["t1", "t2"],
        "asset_group_dynamic_filter": group,
    }


def test_fmt_asset_prints_the_fields_the_write_tool_sets():
    from app.integrations.providers.openaev._common import _fmt_asset

    out = _fmt_asset(
        {
            "asset_id": "e1",
            "asset_name": "shop",
            "asset_category": "WEB_APPLICATION",
            "asset_subcategory": "WEBSITE",
            "asset_criticality": "HIGH",
            "asset_internet_facing": False,
            "asset_url": "https://shop.example",
            "asset_external_reference": "dns:shop",
            "asset_tags": [{"tag_id": "t1"}],
        }
    )
    for line in (
        "Category: WEB_APPLICATION",
        "Subcategory: WEBSITE",
        "Criticality: HIGH",
        "Internet-facing: False",
        "URL: https://shop.example",
        "External reference: dns:shop",
        "Tags: t1",
    ):
        assert line in out


async def test_asset_update_of_a_missing_endpoint_writes_nothing(monkeypatch):
    from app.integrations.providers.openaev import assets

    executor, _ = _build()
    fake = _RecordingApi({})
    monkeypatch.setattr(assets, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_asset",
        {"action": "update", "asset_id": "e1", "name": "x"},
    )
    assert "nothing updated" in result.content
    assert [c["method"] for c in fake.calls] == ["get"]


async def test_asset_bulk_update_by_filter_sends_one_put(monkeypatch):
    # xtm-one #1288: OpenAEV #9654 PUT /api/assets/bulk, selection by filter.
    from app.integrations.providers.openaev import assets

    executor, _ = _build()
    fake = _RecordingApi(["a1", "a2", "a3"])
    monkeypatch.setattr(assets, "_api", fake)
    group = {
        "mode": "and",
        "filters": [{"key": "asset_tags", "operator": "contains", "values": ["t1"], "mode": "or"}],
    }
    result = await _run(
        executor,
        "manage_openaev_asset",
        {
            "action": "bulk_update",
            "asset_filter": json.dumps(group),
            "category": "web_application",
            "subcategory": "website",
            "tags": ["t9"],
        },
    )
    assert not result.is_error, result.content
    assert [(c["method"], c["path"]) for c in fake.calls] == [("put", "/api/assets/bulk")]
    sent = fake.last_json()
    assert sent == {
        "search_pagination_input": {"filterGroup": group},
        "asset_category": "WEB_APPLICATION",
        "asset_subcategory": "WEBSITE",
        "asset_tags_to_add": ["t9"],
    }
    assert "Updated 3 asset(s)" in result.content


async def test_asset_bulk_update_by_ids(monkeypatch):
    from app.integrations.providers.openaev import assets

    executor, _ = _build()
    fake = _RecordingApi(["a1"])
    monkeypatch.setattr(assets, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_asset",
        {"action": "bulk_update", "asset_ids": ["a1"], "criticality": "high", "url": "https://x.example"},
    )
    assert not result.is_error, result.content
    assert fake.last_json() == {
        "asset_ids_to_process": ["a1"],
        "asset_criticality": "HIGH",
        "asset_url": "https://x.example",
    }


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"category": "host"}, "exactly one selection"),
        ({"asset_ids": ["a1"], "asset_filter": "{}", "category": "host"}, "exactly one selection"),
        ({"asset_ids": ["a1"]}, "at least one of"),
        ({"asset_ids": ["a1"], "subcategory": "server"}, "needs its category"),
        ({"asset_filter": "not json", "category": "host"}, "valid JSON"),
    ],
)
async def test_asset_bulk_update_refuses_before_any_call(monkeypatch, args, message):
    from app.integrations.providers.openaev import assets

    executor, _ = _build()
    fake = _RecordingApi([])
    monkeypatch.setattr(assets, "_api", fake)
    result = await _run(executor, "manage_openaev_asset", {"action": "bulk_update", **args})
    assert message in result.content
    assert fake.calls == []


async def test_asset_bulk_update_on_an_openaev_without_the_route(monkeypatch):
    from app.integrations.providers.openaev import assets

    executor, _ = _build()

    async def fake(base_url, headers, method, path, action, *, json=None, params=None, **kw):
        return None, "Error: OpenAEV resource not found (404) while trying to bulk update assets."

    monkeypatch.setattr(assets, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_asset",
        {"action": "bulk_update", "asset_ids": ["a1"], "category": "host"},
    )
    assert "one by one" in result.content


async def test_empty_update_bodies_are_rejected():
    # Representative coverage of the empty-update guard across the domain
    # modules: an update with only the entity id must return a friendly message
    # (and never issue an empty-body PUT).
    executor, _ = _build()
    cases = [
        ("manage_openaev_user", {"action": "update", "user_id": "u1"}),
        ("manage_openaev_organization", {"action": "update", "organization_id": "o1"}),
        ("manage_openaev_group", {"action": "update", "group_id": "g1"}),
        ("manage_openaev_role", {"action": "update", "role_id": "r1"}),
        ("manage_openaev_channel", {"action": "update", "channel_id": "c1"}),
        (
            "manage_openaev_article",
            {"action": "update", "parent_id": "e1", "article_id": "a1"},
        ),
        ("manage_openaev_challenge", {"action": "update", "challenge_id": "c1"}),
        ("manage_openaev_vulnerability", {"action": "update", "vulnerability_id": "v1"}),
        ("manage_openaev_finding", {"action": "update", "finding_id": "f1"}),
        (
            "manage_openaev_kill_chain_phase",
            {"action": "update", "phase_id": "p1"},
        ),
        (
            "manage_openaev_attack_pattern",
            {"action": "update", "attack_pattern_id": "ap1"},
        ),
        (
            "manage_openaev_variable",
            {"action": "update", "parent_id": "s1", "variable_id": "v1"},
        ),
        (
            "manage_openaev_asset_group",
            {"action": "update", "asset_group_id": "ag1"},
        ),
        (
            "manage_openaev_security_platform",
            {"action": "update", "security_platform_id": "sp1"},
        ),
        (
            "manage_openaev_atomic_testing",
            {"action": "update", "inject_id": "i1"},
        ),
        (
            "manage_openaev_reporting",
            {"action": "update", "reporting_id": "r1"},
        ),
        (
            "manage_openaev_reporting",
            {"action": "update_schedule", "reporting_id": "r1", "schedule_id": "s1"},
        ),
    ]
    for name, args in cases:
        result = await _run(executor, name, args)
        assert not result.is_error, f"{name}: {result.content}"
        assert "at least one field" in result.content, f"{name}: {result.content}"


# ── Connector catalog browsing + connector-instance deployment ───────────


class _PathApi:
    """Stub for `_api` answering each path with its own canned body."""

    def __init__(self, bodies):
        self._bodies = bodies
        self.paths: list[str] = []

    async def __call__(self, base_url, headers, method, path, action, *, json=None, params=None):
        self.paths.append(path)
        body = self._bodies.get(path)
        if isinstance(body, str):
            return None, body
        return _FakeResp(body), None


async def test_query_connectors_shows_the_reported_composer_version_and_image(monkeypatch):
    # The XTM Composer version and image come from what the Composer reports to
    # OpenAEV (GET /api/settings, OpenAEV #9612), never from the platform version.
    from app.integrations.providers.openaev import platform

    executor, _ = _build()
    fake = _PathApi(
        {
            "/api/collectors": [{"collector_id": "c1", "collector_name": "Mitre"}],
            "/api/settings": {
                "platform_version": "3.261118.0",
                "xtm_composer_version": "3.261121.0",
                "xtm_composer_image": "filigran/xtm-composer:3.261121.0",
                "xtm_composer_last_connectivity_check": "2026-11-21T14:00:00Z",
            },
        }
    )
    monkeypatch.setattr(platform, "_api", fake)
    result = await _run(executor, "query_openaev_connectors", {"kind": "collectors"})
    assert not result.is_error, result.content
    assert "Mitre" in result.content
    assert "  version: 3.261121.0" in result.content
    assert "  image: filigran/xtm-composer:3.261121.0" in result.content
    assert "  last connectivity check: 2026-11-21T14:00:00Z" in result.content
    assert "3.261118.0" not in result.content


async def test_query_connectors_names_an_openaev_without_composer_fields(monkeypatch):
    # An older OpenAEV (no xtm_composer_* in its settings) is named as such, and a
    # failed settings read keeps the connector list.
    from app.integrations.providers.openaev import platform

    executor, _ = _build()
    monkeypatch.setattr(
        platform,
        "_api",
        _PathApi(
            {
                "/api/injectors": [{"injector_id": "i1", "injector_name": "HTTP"}],
                "/api/settings": {"platform_version": "3.261118.0"},
            }
        ),
    )
    result = await _run(executor, "query_openaev_connectors", {"kind": "injectors"})
    assert "XTM Composer: not reported by this OpenAEV" in result.content
    assert "3.261118.0" not in result.content

    monkeypatch.setattr(
        platform,
        "_api",
        _PathApi(
            {
                "/api/injectors": [{"injector_id": "i1", "injector_name": "HTTP"}],
                "/api/settings": "OpenAEV answered 403",
            }
        ),
    )
    result = await _run(executor, "query_openaev_connectors", {"kind": "injectors"})
    assert "HTTP" in result.content
    assert "XTM Composer: could not be read (OpenAEV answered 403)" in result.content


async def test_catalog_get_surfaces_required_and_secret_params(monkeypatch):
    # query_openaev_catalog get fetches the connector + its configuration schema
    # and flags required / secret parameters so the agent knows what to ask for.
    from app.integrations.providers.openaev import platform

    executor, _ = _build()

    class _SeqApi:
        """Returns the connector detail, then its configurations, in order."""

        def __init__(self):
            self._responses = [
                _FakeResp(
                    {
                        "catalog_connector_id": "cc1",
                        "catalog_connector_title": "AlienVault",
                        "catalog_connector_type": "INJECTOR",
                        "catalog_connector_manager_supported": True,
                    }
                ),
                _FakeResp(
                    [
                        {
                            "connector_configuration_key": "ALIENVAULT_API_KEY",
                            "connector_configuration_type": "STRING",
                            "connector_configuration_format": "PASSWORD",
                            "connector_configuration_required": True,
                            "connector_configuration_description": "Your OTX key",
                        }
                    ]
                ),
            ]
            self.calls: list[dict] = []

        async def __call__(self, base_url, headers, method, path, action, *, json=None, params=None):
            self.calls.append({"method": method, "path": path})
            return self._responses[len(self.calls) - 1], None

    monkeypatch.setattr(platform, "_api", _SeqApi())
    result = await _run(
        executor,
        "query_openaev_catalog",
        {"action": "get", "catalog_connector_id": "cc1"},
    )
    assert not result.is_error, result.content
    assert "ALIENVAULT_API_KEY" in result.content
    assert "REQUIRED" in result.content
    assert "secret" in result.content


async def test_connector_instance_deploy_builds_configuration_payload(monkeypatch):
    # The flat config object becomes the API's connector_instance_configurations
    # list with configuration_key / configuration_value pairs.
    from app.integrations.providers.openaev import platform

    executor, _ = _build()
    fake = _RecordingApi({"connector_instance_id": "ci1"})
    monkeypatch.setattr(platform, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_connector_instance",
        {
            "action": "deploy",
            "catalog_connector_id": "cc1",
            "config": '{"ALIENVAULT_API_KEY": "secret-key"}',
        },
    )
    assert not result.is_error, result.content
    assert "Deployed connector instance" in result.content
    body = fake.last_json()
    assert body["catalog_connector_id"] == "cc1"
    assert body["connector_instance_configurations"] == [
        {"configuration_key": "ALIENVAULT_API_KEY", "configuration_value": "secret-key"}
    ]
    assert fake.calls[-1]["method"] == "post"


async def test_connector_instance_config_values_are_stringified(monkeypatch):
    # The LLM can emit JSON null / non-string values; the payload must coerce
    # every configuration_value to a string (None → "") so the API never
    # receives configuration_value: null, matching the OpenCTI path.
    from app.integrations.providers.openaev import platform

    executor, _ = _build()
    fake = _RecordingApi({"connector_instance_id": "ci1"})
    monkeypatch.setattr(platform, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_connector_instance",
        {
            "action": "deploy",
            "catalog_connector_id": "cc1",
            "config": '{"NULL_VAL": null, "INT_VAL": 7, "STR_VAL": "x"}',
        },
    )
    assert not result.is_error, result.content
    body = fake.last_json()
    by_key = {
        c["configuration_key"]: c["configuration_value"]
        for c in body["connector_instance_configurations"]
    }
    assert by_key == {"NULL_VAL": "", "INT_VAL": "7", "STR_VAL": "x"}
    # No value is ever None in the outgoing payload.
    assert all(
        isinstance(c["configuration_value"], str)
        for c in body["connector_instance_configurations"]
    )


async def test_connector_instance_deploy_requires_catalog_id():
    executor, _ = _build()
    result = await _run(
        executor,
        "manage_openaev_connector_instance",
        {"action": "deploy", "config": "{}"},
    )
    assert not result.is_error, result.content
    assert "catalog_connector_id is required" in result.content


async def test_connector_instance_start_sets_requested_status(monkeypatch):
    from app.integrations.providers.openaev import platform

    executor, _ = _build()
    fake = _RecordingApi({})
    monkeypatch.setattr(platform, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_connector_instance",
        {"action": "start", "connector_instance_id": "ci1"},
    )
    assert not result.is_error, result.content
    body = fake.last_json()
    assert body == {"connector_instance_requested_status": "starting"}
    assert fake.calls[-1]["path"].endswith("/requested-status")


async def test_connector_instance_logs_searches_one_page(monkeypatch):
    # OpenAEV has no GET .../logs: the only read is the paginated
    # POST .../logs/search with a SearchPaginationInput body.
    from app.integrations.providers.openaev import platform

    executor, _ = _build()
    fake = _RecordingApi({"content": [{"i": 1}], "totalElements": 1})
    monkeypatch.setattr(platform, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_connector_instance",
        {"action": "logs", "connector_instance_id": "ci1"},
    )
    assert not result.is_error, result.content
    assert fake.calls == [
        {
            "method": "post",
            "path": "/api/connector-instances/ci1/logs/search",
            "json": {"page": 0, "size": 50},
        }
    ]


async def test_connector_instance_logs_returns_valid_json_when_truncated(monkeypatch):
    # The logs action caps by entry count and returns STRUCTURALLY VALID JSON
    # (slicing the serialized string could cut a token mid-string). The page
    # comes newest first; the tool returns it oldest first, so the most recent
    # entry reads last, with a marker saying older entries were left out.
    import json as _json

    from app.integrations.providers.openaev import platform

    executor, _ = _build()
    newest_first = [{"i": n, "msg": "x" * 200} for n in range(119, 69, -1)]
    monkeypatch.setattr(
        platform,
        "_api",
        _RecordingApi({"content": newest_first, "totalElements": 120}),
    )
    result = await _run(
        executor,
        "manage_openaev_connector_instance",
        {"action": "logs", "connector_instance_id": "ci1"},
    )
    assert not result.is_error, result.content
    parsed = _json.loads(result.content)  # must not raise
    assert [e["i"] for e in parsed["logs"]] == list(range(70, 120))
    assert parsed["truncated"] == "showing the most recent 50 of 120 log entries"


async def test_connector_instance_logs_valid_json_when_small(monkeypatch):
    import json as _json

    from app.integrations.providers.openaev import platform

    executor, _ = _build()
    monkeypatch.setattr(
        platform,
        "_api",
        _RecordingApi({"content": [{"i": 2}, {"i": 1}], "totalElements": 2}),
    )
    result = await _run(
        executor,
        "manage_openaev_connector_instance",
        {"action": "logs", "connector_instance_id": "ci1"},
    )
    assert not result.is_error, result.content
    parsed = _json.loads(result.content)
    assert parsed == {"logs": [{"i": 1}, {"i": 2}]}


async def test_connector_instance_logs_reports_an_empty_page(monkeypatch):
    from app.integrations.providers.openaev import platform

    executor, _ = _build()
    monkeypatch.setattr(
        platform, "_api", _RecordingApi({"content": [], "totalElements": 0})
    )
    result = await _run(
        executor,
        "manage_openaev_connector_instance",
        {"action": "logs", "connector_instance_id": "ci1"},
    )
    assert not result.is_error, result.content
    assert result.content == "No logs for connector instance ci1."


# ── Connector instance resolved from an injector / collector / executor id ──


class _PathApi:
    """Stub for `_api` answering by path: a dict value is the JSON, a str an error."""

    def __init__(self, routes):
        self._routes = routes
        self.calls: list[dict] = []

    async def __call__(self, base_url, headers, method, path, action, *, json=None, params=None, **kw):
        self.calls.append({"method": method, "path": path})
        answer = self._routes.get(path)
        if answer is None:
            return None, f"OpenAEV {action} failed: HTTP 404 Not Found"
        if isinstance(answer, str):
            return None, answer
        return _FakeResp(answer), None


_RELATED = {
    "connector_instance_id": "ci1",
    "catalog_connector_id": "cc1",
    "connector_registered": True,
}
_LOGS = {"content": [{"i": 1}], "totalElements": 1}


async def test_connector_instance_logs_from_an_injector_id(monkeypatch):
    from app.integrations.providers.openaev import platform

    executor, _ = _build()
    fake = _PathApi(
        {
            "/api/injectors/inj1/related-ids": _RELATED,
            "/api/connector-instances/ci1/logs/search": _LOGS,
        }
    )
    monkeypatch.setattr(platform, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_connector_instance",
        {"action": "logs", "connector_id": "inj1"},
    )
    assert not result.is_error, result.content
    assert json.loads(result.content) == {
        "connector_instance_id": "ci1",
        "logs": [{"i": 1}],
    }
    assert [c["path"] for c in fake.calls] == [
        "/api/injectors/inj1/related-ids",
        "/api/connector-instances/ci1/logs/search",
    ]


@pytest.mark.parametrize("kind", ["collectors", "executors"])
async def test_connector_instance_from_a_collector_or_executor_id(monkeypatch, kind):
    # Without connector_kind the kinds are tried in turn, a 404 moving on.
    from app.integrations.providers.openaev import platform

    executor, _ = _build()
    fake = _PathApi(
        {
            f"/api/{kind}/c1/related-ids": _RELATED,
            "/api/connector-instances/ci1/requested-status": {},
        }
    )
    monkeypatch.setattr(platform, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_connector_instance",
        {"action": "stop", "connector_id": "c1"},
    )
    assert not result.is_error, result.content
    assert result.content == "Connector instance ci1 is now stopping."
    assert fake.calls[-1]["path"] == "/api/connector-instances/ci1/requested-status"


async def test_connector_kind_reads_one_related_ids_endpoint(monkeypatch):
    from app.integrations.providers.openaev import platform

    executor, _ = _build()
    fake = _PathApi(
        {
            "/api/executors/e1/related-ids": _RELATED,
            "/api/connector-instances/ci1": {
                "connector_instance_id": "ci1",
                "connector_instance_current_status": "started",
            },
        }
    )
    monkeypatch.setattr(platform, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_connector_instance",
        {"action": "get", "connector_id": "e1", "connector_kind": "executor"},
    )
    assert not result.is_error, result.content
    assert "Connector instance ci1 (resolved from executor e1)" in result.content
    assert fake.calls[0]["path"] == "/api/executors/e1/related-ids"


async def test_instance_get_shows_image_and_container_runtime(monkeypatch):
    from app.integrations.providers.openaev import platform

    executor, _ = _build()
    fake = _PathApi(
        {
            "/api/connector-instances/ci1": {
                "connector_instance_id": "ci1",
                "connector_instance_current_status": "started",
                "connector_instance_requested_status": "starting",
                "connector_image": "openaev/injector-nmap:rolling",
                "connector_instance_started_at": "2026-11-20T10:00:00Z",
                "connector_instance_restart_count": 2,
                "connector_instance_is_in_reboot_loop": False,
            },
        }
    )
    monkeypatch.setattr(platform, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_connector_instance",
        {"action": "get", "connector_instance_id": "ci1"},
    )
    assert not result.is_error, result.content
    assert "  image: openaev/injector-nmap:rolling" in result.content
    assert "  started at: 2026-11-20T10:00:00Z" in result.content
    assert "  restart count: 2" in result.content
    assert "  in reboot loop: no" in result.content
    assert "not reported" not in result.content


async def test_instance_get_says_when_start_time_is_not_reported(monkeypatch):
    from app.integrations.providers.openaev import platform

    executor, _ = _build()
    fake = _PathApi(
        {
            "/api/connector-instances/ci1": {
                "connector_instance_id": "ci1",
                "connector_instance_current_status": "started",
            },
        }
    )
    monkeypatch.setattr(platform, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_connector_instance",
        {"action": "get", "connector_instance_id": "ci1"},
    )
    assert not result.is_error, result.content
    assert "  started at: not reported by this OpenAEV" in result.content
    assert "  image:" not in result.content
    assert "restart count" not in result.content


async def test_connector_without_an_instance_says_it_is_not_managed(monkeypatch):
    from app.integrations.providers.openaev import platform

    executor, _ = _build()
    fake = _PathApi(
        {
            "/api/injectors/inj1/related-ids": {
                "connector_instance_id": None,
                "catalog_connector_id": None,
                "connector_registered": True,
            }
        }
    )
    monkeypatch.setattr(platform, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_connector_instance",
        {"action": "logs", "connector_id": "inj1"},
    )
    assert not result.is_error, result.content
    assert "no connector instance managed by OpenAEV" in result.content
    assert "registered externally" in result.content
    assert len(fake.calls) == 1


async def test_unknown_connector_id_names_every_kind_tried(monkeypatch):
    from app.integrations.providers.openaev import platform

    executor, _ = _build()
    fake = _PathApi({})
    monkeypatch.setattr(platform, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_connector_instance",
        {"action": "logs", "connector_id": "nope"},
    )
    assert "No injector, collector or executor with id nope" in result.content
    # The three kinds, then the id tried as a connector instance id.
    assert [c["path"] for c in fake.calls][-1] == "/api/connector-instances/nope"
    assert len(fake.calls) == 4


async def test_connector_instance_id_passed_as_connector_id(monkeypatch):
    # query_openaev_connectors lists both ids; an instance id given as
    # connector_id is resolved as the instance instead of failing.
    from app.integrations.providers.openaev import platform

    executor, _ = _build()
    fake = _PathApi(
        {
            "/api/connector-instances/ci1": {"connector_instance_id": "ci1"},
            "/api/connector-instances/ci1/logs/search": _LOGS,
        }
    )
    monkeypatch.setattr(platform, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_connector_instance",
        {"action": "logs", "connector_id": "ci1"},
    )
    assert not result.is_error, result.content
    assert json.loads(result.content) == {
        "connector_instance_id": "ci1",
        "logs": [{"i": 1}],
    }
    assert [c["path"] for c in fake.calls] == [
        "/api/injectors/ci1/related-ids",
        "/api/collectors/ci1/related-ids",
        "/api/executors/ci1/related-ids",
        "/api/connector-instances/ci1",
        "/api/connector-instances/ci1/logs/search",
    ]


async def test_connector_list_footer_names_connector_instance_id(monkeypatch):
    from app.integrations.providers.openaev import platform

    executor, _ = _build()
    monkeypatch.setattr(platform, "_api", _PathApi({"/api/injectors": [{"injector_id": "inj1"}]}))
    result = await _run(executor, "query_openaev_connectors", {"kind": "injectors"})
    assert "takes that id as connector_instance_id" in result.content


async def test_catalog_id_alone_never_picks_a_deployment(monkeypatch):
    from app.integrations.providers.openaev import platform

    executor, _ = _build()
    fake = _PathApi({})
    monkeypatch.setattr(platform, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_connector_instance",
        {"action": "logs", "catalog_connector_id": "cc1"},
    )
    assert "several deployments" in result.content
    assert fake.calls == []


async def test_instance_404_suggests_connector_id(monkeypatch):
    from app.integrations.providers.openaev import platform

    executor, _ = _build()
    monkeypatch.setattr(platform, "_api", _PathApi({}))
    result = await _run(
        executor,
        "manage_openaev_connector_instance",
        {"action": "logs", "connector_instance_id": "inj1"},
    )
    assert "pass it as connector_id instead" in result.content


async def test_connector_list_shows_the_connector_instance_id(monkeypatch):
    from app.integrations.providers.openaev import platform

    executor, _ = _build()
    monkeypatch.setattr(
        platform,
        "_api",
        _RecordingApi(
            [
                {
                    "injector_id": "inj1",
                    "injector_name": "Censys",
                    "connector_instance": {"connector_instance_id": "ci1"},
                },
                {"injector_id": "inj2", "injector_name": "HTTP"},
            ]
        ),
    )
    result = await _run(executor, "query_openaev_connectors", {"kind": "injectors"})
    assert not result.is_error, result.content
    assert "- Censys (id: inj1, connector_instance_id: ci1)" in result.content
    assert "- HTTP (id: inj2)" in result.content


# ── Scenario update and variables: full-entity PUTs ─────────────


async def test_scenario_update_sends_the_stored_fields_with_the_change(monkeypatch):
    # PUT /api/scenarios/{id} copies the whole UpdateScenarioInput onto the
    # scenario: a body with only the changed field is a 400 (no scenario_name)
    # and would otherwise wipe every other field, tags included.
    from app.integrations.providers.openaev import core

    stored = {
        "scenario_id": "s1",
        "scenario_name": "Ransomware drill",
        "scenario_subtitle": "Q3",
        "scenario_description": "old",
        "scenario_category": "attack-scenario",
        "scenario_main_focus": "incident-response",
        "scenario_severity": "high",
        "scenario_default_kill_chain": None,
        "scenario_tags": ["t1", "t2"],
        "scenario_mails_reply_to": ["soc@example.com"],
        "scenario_custom_dashboard": "d1",
        "scenario_lessons_enabled": True,
        "scenario_injects": ["i1"],
    }
    calls: list[dict] = []

    async def fake_safe_request(client, method, url, provider, action, *, headers=None, json=None):
        calls.append({"method": method, "url": url, "json": json})
        return _FakeResp(stored if method == "get" else {**stored, **(json or {})}), None

    monkeypatch.setattr(core, "scoped_safe_request", fake_safe_request)
    executor, _ = _build()
    result = await _run(
        executor,
        "update_openaev_scenario",
        {"scenario_id": "s1", "description": "new"},
    )
    assert not result.is_error, result.content
    assert [(c["method"], c["url"]) for c in calls] == [
        ("get", "https://demo.openaev.io/api/scenarios/s1"),
        ("put", "https://demo.openaev.io/api/scenarios/s1"),
    ]
    assert calls[1]["json"] == {
        "scenario_name": "Ransomware drill",
        "scenario_subtitle": "Q3",
        "scenario_description": "new",
        "scenario_category": "attack-scenario",
        "scenario_main_focus": "incident-response",
        "scenario_severity": "high",
        "scenario_tags": ["t1", "t2"],
        "scenario_mails_reply_to": ["soc@example.com"],
        "scenario_custom_dashboard": "d1",
        "scenario_lessons_enabled": True,
    }


async def test_scenario_update_does_not_put_when_the_read_fails(monkeypatch):
    from app.integrations.providers.openaev import core

    calls: list[str] = []

    async def fake_safe_request(client, method, url, provider, action, *, headers=None, json=None):
        calls.append(method)
        return None, "Error: OpenAEV fetch scenario failed (404)."

    monkeypatch.setattr(core, "scoped_safe_request", fake_safe_request)
    executor, _ = _build()
    result = await _run(
        executor, "update_openaev_scenario", {"scenario_id": "s1", "name": "x"}
    )
    assert calls == ["get"]
    assert result.content == "Error: OpenAEV fetch scenario failed (404)."


async def test_variable_create_rejects_a_key_openaev_refuses(monkeypatch):
    # VariableInput.key is @Pattern("^[a-z_]+$"): a digit, capital, hyphen or
    # trailing newline is a bare 400, so the tool says why before sending.
    from app.integrations.providers.openaev import taxonomy

    executor, _ = _build()
    fake = _RecordingApi({})
    monkeypatch.setattr(taxonomy, "_api", fake)
    for bad in ("xtm_5346", "Target", "target-host", "target host", "target\n"):
        for action, extra in (("create", {}), ("update", {"variable_id": "v1"})):
            result = await _run(
                executor,
                "manage_openaev_variable",
                {"action": action, "parent_id": "s1", "key": bad, **extra},
            )
            assert "only lowercase letters and underscores" in result.content, (action, bad)
    assert fake.calls == []


async def test_variable_create_posts_the_variable_input(monkeypatch):
    from app.integrations.providers.openaev import taxonomy

    executor, _ = _build()
    fake = _RecordingApi({"variable_key": "target_host", "variable_id": "v1"})
    monkeypatch.setattr(taxonomy, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_variable",
        {"action": "create", "parent_id": "s1", "key": "target_host", "value": "1"},
    )
    assert not result.is_error, result.content
    assert fake.calls == [
        {
            "method": "post",
            "path": "/api/scenarios/s1/variables",
            "json": {"variable_key": "target_host", "variable_value": "1"},
        }
    ]


async def test_variable_update_sends_the_stored_fields_with_the_change(monkeypatch):
    # The variable PUT copies the whole VariableInput too: no key is a 400 and
    # an absent value or description is wiped.
    from app.integrations.providers.openaev import taxonomy

    executor, _ = _build()
    fake = _RecordingApi(
        [
            {"variable_id": "v0", "variable_key": "other", "variable_value": "0"},
            {
                "variable_id": "v1",
                "variable_key": "target_host",
                "variable_value": "10.0.0.1",
                "variable_description": "Host to reach",
            },
        ]
    )
    monkeypatch.setattr(taxonomy, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_variable",
        {
            "action": "update",
            "scope": "simulation",
            "parent_id": "e1",
            "variable_id": "v1",
            "value": "10.0.0.2",
        },
    )
    assert not result.is_error, result.content
    assert fake.calls == [
        {"method": "get", "path": "/api/exercises/e1/variables", "json": None},
        {
            "method": "put",
            "path": "/api/exercises/e1/variables/v1",
            "json": {
                "variable_key": "target_host",
                "variable_value": "10.0.0.2",
                "variable_description": "Host to reach",
            },
        },
    ]


async def test_variable_update_of_an_unknown_variable_does_not_put(monkeypatch):
    from app.integrations.providers.openaev import taxonomy

    executor, _ = _build()
    fake = _RecordingApi([{"variable_id": "v0", "variable_key": "other"}])
    monkeypatch.setattr(taxonomy, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_variable",
        {"action": "update", "parent_id": "s1", "variable_id": "v9", "value": "x"},
    )
    assert [c["method"] for c in fake.calls] == ["get"]
    assert result.content.startswith("Error: variable v9 was not found")


# ── Reporting module (v3 /api/reportings replaces the deleted /api/reports) ──


async def test_reporting_create_requires_name_and_context_type():
    executor, _ = _build()
    result = await _run(
        executor, "manage_openaev_reporting", {"action": "create", "name": "Weekly"}
    )
    assert not result.is_error, result.content
    assert "context_type" in result.content


async def test_reporting_create_maps_fields_and_modules(monkeypatch):
    # The tool speaks the reporting_* namespace and expands the module type
    # list into the API's {module_type: ...} objects.
    from app.integrations.providers.openaev import lessons_reports

    executor, _ = _build()
    fake = _RecordingApi({"reporting_id": "rep1", "reporting_name": "Weekly"})
    monkeypatch.setattr(lessons_reports, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_reporting",
        {
            "action": "create",
            "name": "Weekly",
            "context_type": "simulation",  # lowercase must be accepted
            "context_id": "e1",
            "output_format": "pdf",
            "time_range": "last_30_days",
            "modules": ["cover", "failed_expectations"],
        },
    )
    assert not result.is_error, result.content
    body = fake.last_json()
    assert fake.calls[-1]["path"] == "/api/reportings"
    assert body["reporting_name"] == "Weekly"
    assert body["reporting_context_type"] == "SIMULATION"
    assert body["reporting_context_id"] == "e1"
    assert body["reporting_default_format"] == "PDF"
    assert body["reporting_time_range"] == "LAST_30_DAYS"
    assert body["reporting_modules"] == [
        {"module_type": "COVER"},
        {"module_type": "FAILED_EXPECTATIONS"},
    ]
    # No legacy report_* field leaks into the payload.
    assert not any(k.startswith("report_") for k in body)


async def test_reporting_rejects_unknown_enum_value():
    executor, _ = _build()
    result = await _run(
        executor,
        "manage_openaev_reporting",
        {"action": "create", "name": "X", "context_type": "SIMULATIONS"},
    )
    assert not result.is_error, result.content
    assert "context_type must be one of" in result.content


async def test_reporting_schedule_omits_enabled_when_not_provided(monkeypatch):
    from app.integrations.providers.openaev import lessons_reports

    executor, _ = _build()
    fake = _RecordingApi({"reporting_schedule_id": "s1"})
    monkeypatch.setattr(lessons_reports, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_reporting",
        {
            "action": "update_schedule",
            "reporting_id": "rep1",
            "schedule_id": "s1",
            "schedule_period": "week",
        },
    )
    assert not result.is_error, result.content
    body = fake.last_json()
    assert body["reporting_schedule_period"] == "WEEK"
    assert "reporting_schedule_enabled" not in body


# ── Preview features: explain the 404 instead of reproducing it ───────────


def _patch_capabilities(monkeypatch, settings: dict):
    """Stub the GET /api/settings probe behind PlatformCapabilities.

    The probe lives in `_common`, so patching a domain module's `_api` does not
    reach it - the snapshot would silently fall back to its fail-open path.
    """
    from app.integrations.providers.openaev import _common

    monkeypatch.setattr(_common, "_api", _RecordingApi(settings))


async def test_preview_tools_explain_a_disabled_feature_flag(monkeypatch):
    # ATTACK_PATH / INJECT_CHAINING endpoints answer 404 when the flag is off,
    # which is indistinguishable from a wrong URL. The tools must say so.
    from app.integrations.providers.openaev import preview

    executor, _ = _build()
    _patch_capabilities(monkeypatch, {"platform_version": "2.3"})
    unreached = _RecordingApi({})
    monkeypatch.setattr(preview, "_api", unreached)
    for name, args in (
        ("query_openaev_attack_path", {"action": "list_simulations"}),
        ("query_openaev_chaining", {"action": "steps", "workflow_id": "w1"}),
    ):
        result = await _run(executor, name, args)
        assert not result.is_error, result.content
        assert "preview feature" in result.content
        assert "enabled_dev_features" in result.content
    # The gate short-circuits before any request is issued.
    assert unreached.calls == []


async def test_preview_tools_run_when_feature_flag_all_is_set(monkeypatch):
    # "*" (FEATURE_FLAG_ALL) enables every preview feature.
    from app.integrations.providers.openaev import preview

    executor, _ = _build()
    _patch_capabilities(
        monkeypatch, {"enabled_dev_features": ["*"], "default_tenant_id": "t1"}
    )
    fake = _RecordingApi([])
    monkeypatch.setattr(preview, "_api", fake)
    result = await _run(
        executor, "query_openaev_attack_path", {"action": "list_simulations"}
    )
    assert not result.is_error, result.content
    assert "preview feature" not in result.content
    assert fake.calls[-1]["path"] == "/api/attack-path/simulations"


async def test_preview_tools_run_when_openaev_serialises_wildcard_by_name(monkeypatch):
    # OpenAEV serialises the PreviewFeature enum by NAME, so a platform configured
    # with enabled-dev-features=* reports the wildcard as the string
    # "FEATURE_FLAG_ALL" (not "*") on GET /api/settings - which is also what
    # OpenAEV's own frontend matches. It must still enable every preview feature;
    # otherwise every flagged tool wrongly 404-explains on a wildcard platform.
    from app.integrations.providers.openaev import preview

    executor, _ = _build()
    _patch_capabilities(
        monkeypatch,
        {"enabled_dev_features": ["FEATURE_FLAG_ALL"], "default_tenant_id": "t1"},
    )
    fake = _RecordingApi([])
    monkeypatch.setattr(preview, "_api", fake)
    result = await _run(
        executor, "query_openaev_attack_path", {"action": "list_simulations"}
    )
    assert not result.is_error, result.content
    assert "preview feature" not in result.content
    assert fake.calls[-1]["path"] == "/api/attack-path/simulations"


async def test_chaining_threads_the_tenant_id(monkeypatch):
    # Every chaining route exists ONLY in tenant-scoped form, so the resolved
    # default tenant id has to reach _api.
    from app.integrations.providers.openaev import preview

    executor, _ = _build()
    _patch_capabilities(
        monkeypatch,
        {"enabled_dev_features": ["INJECT_CHAINING"], "default_tenant_id": "t1"},
    )
    captured: dict = {}

    class _TenantApi:
        async def __call__(
            self,
            base_url,
            headers,
            method,
            path,
            action,
            *,
            json=None,
            params=None,
            tenant_id="",
        ):
            captured["path"] = path
            captured["tenant_id"] = tenant_id
            return _FakeResp({}), None

    monkeypatch.setattr(preview, "_api", _TenantApi())
    result = await _run(
        executor,
        "query_openaev_chaining",
        {"action": "workflow_config", "workflow_id": "w1"},
    )
    assert not result.is_error, result.content
    assert captured["path"] == "/api/workflows/w1/configuration"
    assert captured["tenant_id"] == "t1"


async def test_chaining_steps_and_conditions_hit_the_tenant_root_routes(monkeypatch):
    # StepApi / ConditionApi live at /api/tenants/{id}/steps and /conditions,
    # with no /chaining segment.
    from app.integrations.providers.openaev import preview

    executor, _ = _build()
    _patch_capabilities(
        monkeypatch,
        {"enabled_dev_features": ["INJECT_CHAINING"], "default_tenant_id": "t1"},
    )
    calls: list[tuple] = []

    async def fake_api(
        base_url, headers, method, path, action, *, json=None, params=None, tenant_id=""
    ):
        calls.append((method, path, params, tenant_id))
        return _FakeResp([]), None

    monkeypatch.setattr(preview, "_api", fake_api)
    for args in (
        {"action": "steps", "workflow_id": "w1"},
        {"action": "step", "step_id": "st1"},
        {"action": "conditions", "workflow_id": "w1"},
        {"action": "condition", "condition_id": "c1"},
    ):
        result = await _run(executor, "query_openaev_chaining", args)
        assert not result.is_error, result.content
    assert calls == [
        ("get", "/api/steps", {"workflow_id": "w1"}, "t1"),
        ("get", "/api/steps/st1", None, "t1"),
        ("get", "/api/conditions", {"workflow_id": "w1"}, "t1"),
        ("get", "/api/conditions/c1", None, "t1"),
    ]


async def test_chaining_reports_a_missing_tenant_id(monkeypatch):
    executor, _ = _build()
    _patch_capabilities(monkeypatch, {"enabled_dev_features": ["INJECT_CHAINING"]})
    result = await _run(
        executor,
        "query_openaev_chaining",
        {"action": "workflow_config", "workflow_id": "w1"},
    )
    assert not result.is_error, result.content
    assert "tenant" in result.content.lower()


def test_tenant_path_rewrites_only_api_paths():
    from app.integrations.providers.openaev._common import _tenant_path

    assert _tenant_path("/api/threat_arsenals/a1", "t1") == (
        "/api/tenants/t1/threat_arsenals/a1"
    )
    # Already tenant-scoped, or no tenant: unchanged.
    assert _tenant_path("/api/tenants/t1/x", "t1") == "/api/tenants/t1/x"
    assert _tenant_path("/api/threat_arsenals/a1", "") == "/api/threat_arsenals/a1"


def test_filter_group_validation_rejects_silent_coercions():
    from app.integrations.providers.openaev._common import _validate_filter_group

    assert _validate_filter_group({"mode": "and", "filters": []}) == ""
    assert _validate_filter_group(
        {"mode": "and", "filters": [{"key": "asset_name", "operator": "contains"}]}
    ) == ""
    # An unknown operator would be coerced to `eq` server-side.
    err = _validate_filter_group(
        {"mode": "and", "filters": [{"key": "asset_name", "operator": "conatins"}]}
    )
    assert "conatins" in err and "eq" in err
    # An unknown mode would be coerced to `and`.
    assert "and" in _validate_filter_group({"mode": "xor", "filters": []})
    # A missing key is rejected with guidance on where keys come from (no tool
    # name: the settings tool is not exposed over the MCP surface).
    _missing_key_err = _validate_filter_group(
        {"filters": [{"operator": "eq", "values": ["x"]}]}
    )
    assert "missing 'key'" in _missing_key_err
    assert "attribute names" in _missing_key_err


def test_not_found_errors_explain_the_three_causes():
    from app.integrations.providers.openaev._common import _annotate_not_found

    annotated = _annotate_not_found("OpenAEV returned HTTP 404 for get asset.")
    assert "RBAC" in annotated or "permission" in annotated
    assert "preview feature" in annotated
    # Non-404 errors are passed through untouched.
    assert _annotate_not_found("HTTP 500 boom") == "HTTP 500 boom"


# ── Expectations: analyst-facing surface, not the collector polling queue ──


def test_expectation_tool_does_not_expose_collector_endpoints():
    # The /api/injects/expectations* routes are collector-facing (an EDR posting
    # its own verdict). They must not be reachable from the agent tool.
    from app.integrations.providers.openaev import expectations as expectations_mod

    with open(expectations_mod.__file__, encoding="utf-8") as fh:
        body = fh.read()
    # Match a quoted request path, so the docstring explaining WHY those routes
    # are excluded does not trip the check.
    assert '"/api/injects/expectations' not in body
    assert "f\"/api/injects/expectations" not in body
    # The analyst read paths are present instead.
    assert "/results" in body
    assert "results-by-attack-patterns" in body
    assert "target_results" in body


def test_expectation_tool_teaches_the_core_distinction():
    _, tools = _build()
    tool = next(t for t in tools if t.name == "manage_openaev_expectation")
    desc = tool.description
    # Execution status vs expectation outcome, and the inverted vulnerability
    # polarity, are the two misreadings that matter most.
    assert "attack RAN" in desc
    assert "INVERTED" in desc
    assert "PENDING is not FAILED" in desc


async def test_expectation_validate_requires_score():
    executor, _ = _build()
    result = await _run(
        executor,
        "manage_openaev_expectation",
        {"action": "validate", "expectation_id": "e1"},
    )
    assert not result.is_error, result.content
    assert "score" in result.content


async def test_expectation_dismiss_drift_requires_explicit_flag():
    executor, _ = _build()
    result = await _run(
        executor,
        "manage_openaev_expectation",
        {"action": "dismiss_drift", "scope": "simulation", "exercise_id": "e1"},
    )
    assert not result.is_error, result.content
    assert "dismissed" in result.content


async def test_expectation_drift_scope_selects_the_right_parent(monkeypatch):
    from app.integrations.providers.openaev import expectations as expectations_mod

    executor, _ = _build()
    fake = _RecordingApi({"drift_detected": False, "total_inject_count": 3})
    monkeypatch.setattr(expectations_mod, "_api", fake)
    for scope, kwargs, expected in (
        ("simulation", {"exercise_id": "e1"}, "/api/exercises/e1/expectations-drift"),
        ("scenario", {"scenario_id": "s1"}, "/api/scenarios/s1/expectations-drift"),
        ("atomic", {"inject_id": "i1"}, "/api/atomic-testings/i1/expectations-drift"),
    ):
        result = await _run(
            executor,
            "manage_openaev_expectation",
            {"action": "drift", "scope": scope, **kwargs},
        )
        assert not result.is_error, result.content
        assert fake.calls[-1]["path"] == expected


async def test_expectation_results_formatter_explains_pending_and_polarity(monkeypatch):
    # The formatter must (a) use OpenAEV's own per-type wording, (b) warn that
    # pending rows are excluded from the average, and (c) state the inverted
    # vulnerability polarity.
    from app.integrations.providers.openaev import expectations as expectations_mod

    executor, _ = _build()
    payload = [
        {
            "type": "DETECTION",
            "avgResult": "SUCCESS",
            "distribution": [
                {"id": "SUCCESS", "label": "Detected", "value": 1},
                {"id": "PENDING", "label": "Pending", "value": 9},
                {"id": "PARTIAL", "label": "Partially Detected", "value": 0},
                {"id": "FAILED", "label": "Not Detected", "value": 0},
            ],
        },
        {
            "type": "VULNERABILITY",
            "avgResult": "FAILED",
            "distribution": [
                {"id": "SUCCESS", "label": "Not vulnerable", "value": 0},
                {"id": "PENDING", "label": "Pending", "value": 0},
                {"id": "PARTIAL", "label": "Partially vulnerable", "value": 0},
                {"id": "FAILED", "label": "Vulnerable", "value": 2},
            ],
        },
    ]
    monkeypatch.setattr(expectations_mod, "_api", _RecordingApi(payload))
    result = await _run(
        executor,
        "manage_openaev_expectation",
        {"action": "simulation_results", "exercise_id": "e1"},
    )
    assert not result.is_error, result.content
    out = result.content
    assert "Detected" in out
    assert "9 target(s) are still PENDING" in out
    assert "Vulnerable" in out
    assert "INVERTED polarity" in out
    # Success rate counts pending in the denominator: 1 of 10 = 10%.
    assert "10%" in out


async def test_expectation_row_formatter_flags_expired_sources(monkeypatch):
    from app.integrations.providers.openaev import expectations as expectations_mod

    executor, _ = _build()
    rows = [
        {
            "inject_expectation_id": "x1",
            "inject_expectation_type": "DETECTION",
            "inject_expectation_status": "FAILED",
            "inject_expectation_score": 0,
            "inject_expectation_expected_score": 100,
            "inject_expectation_agent": "a1",
            "inject_expectation_group": False,
            "inject_expectation_results": [
                {"sourceName": "Defender", "result": "Expired"},
            ],
        }
    ]
    monkeypatch.setattr(expectations_mod, "_api", _RecordingApi(rows))
    result = await _run(
        executor,
        "manage_openaev_expectation",
        {
            "action": "target_results",
            "inject_id": "i1",
            "target_id": "t1",
            "target_type": "agent",
        },
    )
    assert not result.is_error, result.content
    out = result.content
    assert "Not Detected" in out
    assert "never answered" in out
    assert "absence of evidence" in out
    assert "all targets must validate" in out


async def test_expectation_by_attack_pattern_parses_the_real_payload(monkeypatch):
    # `inject_attack_pattern` is serialized with OpenAEV's MonoIdSerializer, so
    # it is a BARE ID STRING - treating it as an object raises AttributeError on
    # every ATT&CK-mapped simulation. The verdicts also nest one level deeper
    # than the outer field: inject_expectation_results[].results[].
    from app.integrations.providers.openaev import expectations as expectations_mod

    executor, _ = _build()
    payload = [
        {
            "inject_attack_pattern": "ap-uuid-1",
            "inject_expectation_results": [
                {
                    "inject_id": "i1",
                    "inject_title": "Dump LSASS",
                    "results": [
                        {
                            "type": "PREVENTION",
                            "avgResult": "FAILED",
                            "distribution": [
                                {"id": "SUCCESS", "label": "Blocked", "value": 0},
                                {"id": "FAILED", "label": "Not blocked", "value": 3},
                            ],
                        }
                    ],
                }
            ],
        }
    ]
    monkeypatch.setattr(expectations_mod, "_api", _RecordingApi(payload))
    result = await _run(
        executor,
        "manage_openaev_expectation",
        {"action": "by_attack_pattern", "exercise_id": "e1"},
    )
    assert not result.is_error, result.content
    out = result.content
    assert "ap-uuid-1" in out  # id surfaced, no crash on the bare string
    assert "Dump LSASS" in out  # per-inject row was actually reached
    # The nested `results` list was rendered with its prevention semantics.
    assert "[PREVENTION]" in out
    assert "allowed to run" in out


async def test_expectation_by_attack_pattern_accepts_an_inlined_object(monkeypatch):
    # Older builds that inline the attack pattern must still render, mirroring
    # the defensive read in services/ctem_metrics_collector.
    from app.integrations.providers.openaev import expectations as expectations_mod

    executor, _ = _build()
    payload = [
        {
            "inject_attack_pattern": {
                "attack_pattern_id": "ap1",
                "attack_pattern_external_id": "T1003",
                "attack_pattern_name": "OS Credential Dumping",
            },
            "inject_expectation_results": [],
        }
    ]
    monkeypatch.setattr(expectations_mod, "_api", _RecordingApi(payload))
    result = await _run(
        executor,
        "manage_openaev_expectation",
        {"action": "by_attack_pattern", "exercise_id": "e1"},
    )
    assert not result.is_error, result.content
    assert "T1003 OS Credential Dumping" in result.content


async def test_expectation_agent_breakdown_shows_agent_names(monkeypatch):
    # InjectExpectationAgentOutput carries the readable name in
    # `inject_expectation_agent_name`; falling back to the id would print a UUID.
    from app.integrations.providers.openaev import expectations as expectations_mod

    executor, _ = _build()
    payload = [
        {
            "inject_expectation_agent": "agent-uuid-1",
            "inject_expectation_agent_name": "WIN-DC-01",
            "inject_expectation_status": "SUCCESS",
        }
    ]
    monkeypatch.setattr(expectations_mod, "_api", _RecordingApi(payload))
    result = await _run(
        executor,
        "manage_openaev_expectation",
        {
            "action": "agent_breakdown",
            "inject_id": "i1",
            "target_id": "t1",
            "expectation_type": "DETECTION",
        },
    )
    assert not result.is_error, result.content
    assert "WIN-DC-01" in result.content
    assert "agent-uuid-1" not in result.content


async def test_reporting_download_falls_back_to_url_without_persistence(monkeypatch):
    # With no persist_file_fn injected (channel-bot / evaluation contexts) the
    # tool must still tell the agent where the document lives.
    from app.integrations.providers.openaev import lessons_reports

    class _BytesResp:
        content = b"%PDF-1.7 fake"
        headers = {"content-type": "application/pdf"}

        def json(self):
            return {}

    class _BytesApi:
        calls: list = []

        async def __call__(
            self, base_url, headers, method, path, action, *, json=None, params=None
        ):
            self.calls.append(path)
            return _BytesResp(), None

    executor, _ = _build()
    monkeypatch.setattr(lessons_reports, "_api", _BytesApi())
    result = await _run(
        executor,
        "manage_openaev_reporting",
        {"action": "download", "generation_id": "g1"},
    )
    assert not result.is_error, result.content
    assert "/api/reportings/generations/g1/file" in result.content
    assert "[[FILE:" not in result.content


# ── Threat arsenal: expected_security_platforms scoping validation ────────

_ARSENAL_CREATE_BASE = {
    "action": "create",
    "action_type": "Command",
    "name": "Recon sweep",
    "platforms": ["Windows"],
    "expectations": ["DETECTION"],
    "domain_ids": ["d1"],
    "command_content": "whoami",
}


async def test_threat_arsenal_scopes_expected_security_platforms(monkeypatch):
    # A valid per-expectation scope reaches the body under the API key, with the
    # keys and platform tokens normalised to their canonical (upper) form.
    from app.integrations.providers.openaev import threat_arsenal

    executor, _ = _build()
    fake = _RecordingApi({"injector_contract_id": "a1", "action_name": "Recon sweep"})
    monkeypatch.setattr(threat_arsenal, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_threat_arsenal",
        {
            **_ARSENAL_CREATE_BASE,
            "expected_security_platforms": {"detection": ["edr", "XDR"]},
        },
    )
    assert not result.is_error, result.content
    scoped = fake.last_json().get("action_expected_security_platforms")
    assert scoped == {"DETECTION": ["EDR", "XDR"]}


async def test_threat_arsenal_rejects_non_technical_expectation_key(monkeypatch):
    # Only PREVENTION / DETECTION / VULNERABILITY may be scoped; a human-validated
    # type is rejected locally (never reaches the API).
    from app.integrations.providers.openaev import threat_arsenal

    executor, _ = _build()
    fake = _RecordingApi({"injector_contract_id": "a1"})
    monkeypatch.setattr(threat_arsenal, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_threat_arsenal",
        {
            **_ARSENAL_CREATE_BASE,
            "expected_security_platforms": {"MANUAL": ["EDR"]},
        },
    )
    assert not result.is_error, result.content
    assert "PREVENTION, DETECTION, VULNERABILITY" in result.content
    assert not fake.calls  # rejected before any request left


async def test_threat_arsenal_rejects_unknown_security_platform(monkeypatch):
    from app.integrations.providers.openaev import threat_arsenal

    executor, _ = _build()
    fake = _RecordingApi({"injector_contract_id": "a1"})
    monkeypatch.setattr(threat_arsenal, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_threat_arsenal",
        {
            **_ARSENAL_CREATE_BASE,
            "expected_security_platforms": {"DETECTION": ["BOGUS"]},
        },
    )
    assert not result.is_error, result.content
    assert "EDR" in result.content  # names the allowed platform set
    assert not fake.calls


def test_threat_arsenal_schema_constrains_expectation_keys():
    # The schema must match the runtime validation: object keys are limited to the
    # technical expectation types.
    _, tools = _build()
    tool = next(t for t in tools if t.name == "manage_openaev_threat_arsenal")
    esp = tool.input_schema["properties"]["expected_security_platforms"]
    assert esp["propertyNames"]["enum"] == ["PREVENTION", "DETECTION", "VULNERABILITY"]


async def test_threat_arsenal_create_sends_empty_association_lists(monkeypatch):
    # Regression guard: a create with no attack-pattern / tag associations must send
    # explicit empty lists, never omit them and never null. The OpenAEV payload path
    # feeds these straight into Spring Data's findAllById, which throws "Ids must not
    # be null" (HTTP 400) on a null argument.
    from app.integrations.providers.openaev import threat_arsenal

    executor, _ = _build()
    fake = _RecordingApi({"injector_contract_id": "a1", "action_name": "Recon sweep"})
    monkeypatch.setattr(threat_arsenal, "_api", fake)
    result = await _run(
        executor, "manage_openaev_threat_arsenal", dict(_ARSENAL_CREATE_BASE)
    )
    assert not result.is_error, result.content
    body = fake.last_json()
    assert body.get("action_attack_patterns") == []
    assert body.get("action_tags") == []


async def test_threat_arsenal_create_coerces_null_associations_from_config(monkeypatch):
    # The agent can pass an explicit null through the free-form `config` JSON
    # (e.g. {"action_attack_patterns": null}). setdefault would leave that null in
    # place and reproduce the 400, so the create path must coerce None to [].
    import json

    from app.integrations.providers.openaev import threat_arsenal

    executor, _ = _build()
    fake = _RecordingApi({"injector_contract_id": "a1", "action_name": "Recon sweep"})
    monkeypatch.setattr(threat_arsenal, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_threat_arsenal",
        {
            **_ARSENAL_CREATE_BASE,
            "config": json.dumps(
                {"action_attack_patterns": None, "action_tags": None}
            ),
        },
    )
    assert not result.is_error, result.content
    body = fake.last_json()
    assert body.get("action_attack_patterns") == []
    assert body.get("action_tags") == []


# ── Threat arsenal: update fetch-and-merge + AI provenance tag ────────────


class _SeqRecordingApi:
    """`_api` stub returning a canned response per (method, path) pattern.

    Records every call so a test can assert both the GET-then-PUT sequence and
    the exact merged body that left the tool. Any unmatched call falls back to
    an empty dict so a stray request never raises.
    """

    def __init__(self, routes):
        # routes: list of (method, path_contains, data)
        self._routes = routes
        self.calls: list[dict] = []

    async def __call__(
        self, base_url, headers, method, path, action, *, json=None, params=None
    ):
        self.calls.append({"method": method, "path": path, "json": json})
        for m, needle, data in self._routes:
            if method == m and needle in path:
                return _FakeResp(data), None
        return _FakeResp({}), None

    def call(self, method, needle):
        for c in self.calls:
            if c["method"] == method and needle in c["path"]:
                return c
        return None


async def test_threat_arsenal_update_fetches_and_merges_mandatory_fields(monkeypatch):
    # The PUT is a full-replace validated as a whole, so a partial patch 400s on
    # the missing @NotBlank/@NotNull fields. The tool must GET the current action
    # first and merge the caller's change onto it: the PUT body carries the name,
    # domains, execution arch and expectations even though the caller only touched
    # the output parsers.
    from app.integrations.providers.openaev import threat_arsenal

    executor, _ = _build()
    current = {
        "action_id": "a1",
        "action_labels": {"en": "Search PowerShell history"},
        "action_domains": ["d1"],
        "action_execution_arch": "ALL_ARCHITECTURES",
        "action_expectations": ["DETECTION"],
        "command_executor": "psh",
        "command_content": "Get-Content (Get-PSReadlineOption).HistorySavePath",
    }
    seq = _SeqRecordingApi(
        [
            ("get", "/threat_arsenals/a1", current),
            ("put", "/threat_arsenals/a1", {"action_id": "a1"}),
        ]
    )
    monkeypatch.setattr(threat_arsenal, "_api", seq)
    result = await _run(
        executor,
        "manage_openaev_threat_arsenal",
        {
            "action": "update",
            "action_id": "a1",
            "config": json.dumps(
                {
                    "action_output_parsers": [
                        {
                            "output_parser_mode": "STDOUT",
                            "output_parser_type": "REGEX",
                            "output_parser_contract_output_elements": [
                                {
                                    "contract_output_element_is_finding": True,
                                    "contract_output_element_rule": "(?i)password",
                                    "contract_output_element_name": "Credential",
                                    "contract_output_element_key": "credential",
                                    "contract_output_element_type": "credentials",
                                    "contract_output_element_regex_groups": [],
                                }
                            ],
                        }
                    ]
                }
            ),
        },
    )
    assert not result.is_error, result.content
    assert seq.call("get", "/threat_arsenals/a1") is not None
    put = seq.call("put", "/threat_arsenals/a1")
    assert put is not None
    body = put["json"]
    # Mandatory fields carried forward from the fetched action.
    assert body["action_name"] == "Search PowerShell history"
    assert body["action_domains"] == ["d1"]
    assert body["action_execution_arch"] == "ALL_ARCHITECTURES"
    assert body["action_expectations"] == ["DETECTION"]
    # The caller's change wins.
    assert body["action_output_parsers"][0]["output_parser_type"] == "REGEX"


async def test_threat_arsenal_update_normalizes_carried_output_parser_tags(monkeypatch):
    # A pre-existing parser's element tags come back from the GET as full Tag
    # objects, but the update input binds them as a list of ids. Carrying the
    # object shape forward would 400; the tool must flatten to ids.
    from app.integrations.providers.openaev import threat_arsenal

    executor, _ = _build()
    current = {
        "action_id": "a1",
        "action_labels": {"en": "Recon"},
        "action_domains": ["d1"],
        "action_execution_arch": "ALL_ARCHITECTURES",
        "action_expectations": ["DETECTION"],
        "action_output_parsers": [
            {
                "output_parser_mode": "STDOUT",
                "output_parser_type": "REGEX",
                "output_parser_contract_output_elements": [
                    {
                        "contract_output_element_key": "host",
                        "contract_output_element_tags": [
                            {"tag_id": "t1", "tag_name": "recon"},
                            {"tag_id": "t2", "tag_name": "net"},
                        ],
                    }
                ],
            }
        ],
    }
    seq = _SeqRecordingApi(
        [
            ("get", "/threat_arsenals/a1", current),
            ("put", "/threat_arsenals/a1", {"action_id": "a1"}),
        ]
    )
    monkeypatch.setattr(threat_arsenal, "_api", seq)
    # Change something OTHER than the parsers so the existing parsers are carried.
    result = await _run(
        executor,
        "manage_openaev_threat_arsenal",
        {"action": "update", "action_id": "a1", "description": "updated"},
    )
    assert not result.is_error, result.content
    put = seq.call("put", "/threat_arsenals/a1")
    element = put["json"]["action_output_parsers"][0][
        "output_parser_contract_output_elements"
    ][0]
    assert element["contract_output_element_tags"] == ["t1", "t2"]


async def test_threat_arsenal_update_preserves_carried_output_parser_tag_ids(monkeypatch):
    # The GET serialises contract_output_element_tags with MultiIdSetSerializer,
    # i.e. as a list of id STRINGS (already the input shape). Carrying that
    # forward must preserve the ids verbatim - a naive "keep only dict entries"
    # flatten would drop every string and wipe the tags on update.
    from app.integrations.providers.openaev import threat_arsenal

    executor, _ = _build()
    current = {
        "action_id": "a1",
        "action_labels": {"en": "Recon"},
        "action_domains": ["d1"],
        "action_execution_arch": "ALL_ARCHITECTURES",
        "action_expectations": ["DETECTION"],
        "action_output_parsers": [
            {
                "output_parser_mode": "STDOUT",
                "output_parser_type": "REGEX",
                "output_parser_contract_output_elements": [
                    {
                        "contract_output_element_key": "host",
                        "contract_output_element_tags": ["t1", "t2"],
                    }
                ],
            }
        ],
    }
    seq = _SeqRecordingApi(
        [
            ("get", "/threat_arsenals/a1", current),
            ("put", "/threat_arsenals/a1", {"action_id": "a1"}),
        ]
    )
    monkeypatch.setattr(threat_arsenal, "_api", seq)
    result = await _run(
        executor,
        "manage_openaev_threat_arsenal",
        {"action": "update", "action_id": "a1", "description": "updated"},
    )
    assert not result.is_error, result.content
    put = seq.call("put", "/threat_arsenals/a1")
    element = put["json"]["action_output_parsers"][0][
        "output_parser_contract_output_elements"
    ][0]
    assert element["contract_output_element_tags"] == ["t1", "t2"]


async def test_threat_arsenal_update_rejects_action_with_no_name_or_domain(monkeypatch):
    # If the fetched action has no readable name and none is supplied, the tool
    # refuses locally with actionable guidance instead of sending a doomed PUT.
    from app.integrations.providers.openaev import threat_arsenal

    executor, _ = _build()
    seq = _SeqRecordingApi(
        [("get", "/threat_arsenals/a1", {"action_id": "a1", "action_labels": {}})]
    )
    monkeypatch.setattr(threat_arsenal, "_api", seq)
    result = await _run(
        executor,
        "manage_openaev_threat_arsenal",
        {"action": "update", "action_id": "a1", "description": "x"},
    )
    assert not result.is_error, result.content
    assert "name" in result.content.lower()
    assert seq.call("put", "/threat_arsenals/a1") is None  # never attempted


async def test_threat_arsenal_update_sends_expectations_when_source_lacks_them(
    monkeypatch,
):
    # Root cause of the in-place fork/update 400: OpenAEV's
    # updateActionPayloadBased hard-rejects a payload-based PUT whose
    # action_expectations is null with "action_expectations is required for
    # payload-based actions". The field is NULLABLE on the Payload entity, so a
    # source whose expectations were never set (collector-/catalog-imported
    # content) comes back from the GET as null and used to be dropped from the
    # merged body - a deterministic 400 on every in-place update of such an
    # action. The tool must seed the service-required fields so the full-replace
    # PUT is always well-formed.
    from app.integrations.providers.openaev import threat_arsenal

    executor, _ = _build()
    current = {
        "action_id": "a1",
        "action_labels": {"en": "Dump LSASS"},
        "action_domains": ["d1"],
        "command_executor": "psh",
        "command_content": "rundll32 comsvcs.dll, MiniDump",
        # Jackson serialises a null Payload.expectations as JSON null, which
        # Python's resp.json() turns into None. No action_execution_arch either
        # - the same missing-field shape that produced the 400.
        "action_expectations": None,
    }
    seq = _SeqRecordingApi(
        [
            ("get", "/threat_arsenals/a1", current),
            ("put", "/threat_arsenals/a1", {"action_id": "a1"}),
        ]
    )
    monkeypatch.setattr(threat_arsenal, "_api", seq)
    result = await _run(
        executor,
        "manage_openaev_threat_arsenal",
        {"action": "update", "action_id": "a1", "description": "updated"},
    )
    assert not result.is_error, result.content
    put = seq.call("put", "/threat_arsenals/a1")
    assert put is not None
    body = put["json"]
    # The service-required payload fields are always present, so the 400 can no
    # longer happen: expectations defaults to [] (== "no expectations", which is
    # what the null value already meant) and the execution arch is carried.
    assert body["action_expectations"] == []
    assert body["action_execution_arch"] == "ALL_ARCHITECTURES"


async def test_arsenal_create_refuses_duplicate_suffixed_name(monkeypatch):
    # No-duplicate-fallback guard: 'create' must refuse a name ending in
    # "(duplicate)" - the suffix OpenAEV's POST /{id}/duplicate stamps on a copy.
    # A create carrying it is the broken fork fallback that leaves both the
    # original and an orphaned "X (duplicate)" in the catalog. Refused locally,
    # before any REST call, so a failed in-place update can never silently become
    # a duplicate-create.
    from app.integrations.providers.openaev import threat_arsenal

    executor, _ = _build()
    fake = _RecordingApi({"injector_contract_id": "a1"})
    monkeypatch.setattr(threat_arsenal, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_threat_arsenal",
        {**_ARSENAL_CREATE_BASE, "name": "Dump LSASS (duplicate)"},
    )
    assert not result.is_error, result.content
    assert "(duplicate)" in result.content
    assert "fork" in result.content.lower()
    assert not fake.calls  # refused before any request left


async def test_arsenal_create_refuses_duplicate_suffixed_name_from_config(monkeypatch):
    # The guard also catches the name when it arrives through config
    # (action_name), case-insensitively, not just via the name argument.
    from app.integrations.providers.openaev import threat_arsenal

    executor, _ = _build()
    fake = _RecordingApi({"injector_contract_id": "a1"})
    monkeypatch.setattr(threat_arsenal, "_api", fake)
    base = {k: v for k, v in _ARSENAL_CREATE_BASE.items() if k != "name"}
    result = await _run(
        executor,
        "manage_openaev_threat_arsenal",
        {**base, "config": json.dumps({"action_name": "Recon sweep (Duplicate)"})},
    )
    assert not result.is_error, result.content
    assert "(duplicate)" in result.content
    assert not fake.calls


async def test_threat_arsenal_create_stamps_ai_generated_tag(monkeypatch):
    # Every action authored through the tool is tagged 'ai generated' without the
    # agent resolving a tag id: the tool upserts the tag and appends its id.
    from app.integrations.providers.openaev import threat_arsenal

    executor, _ = _build()
    seq = _SeqRecordingApi(
        [
            ("post", "/api/tags/upsert", {"tag_id": "ai-tag", "tag_name": "ai generated"}),
            ("post", "/api/threat_arsenals", {"injector_contract_id": "a1"}),
        ]
    )
    monkeypatch.setattr(threat_arsenal, "_api", seq)
    result = await _run(
        executor, "manage_openaev_threat_arsenal", dict(_ARSENAL_CREATE_BASE)
    )
    assert not result.is_error, result.content
    assert seq.call("post", "/api/tags/upsert") is not None
    create = seq.call("post", "/api/threat_arsenals")
    assert create is not None
    assert "ai-tag" in create["json"]["action_tags"]


async def test_threat_arsenal_create_survives_ai_tag_resolution_failure(monkeypatch):
    # Tag resolution is best-effort: if the upsert fails, the action is still
    # created (untagged) rather than the whole create being blocked.
    from app.integrations.providers.openaev import threat_arsenal

    executor, _ = _build()

    class _TagFailsApi(_SeqRecordingApi):
        async def __call__(self, base_url, headers, method, path, action, *, json=None, params=None):
            self.calls.append({"method": method, "path": path, "json": json})
            if "tags/upsert" in path:
                return None, "Error: tag service unavailable"
            return _FakeResp({"injector_contract_id": "a1"}), None

    seq = _TagFailsApi([])
    monkeypatch.setattr(threat_arsenal, "_api", seq)
    result = await _run(
        executor, "manage_openaev_threat_arsenal", dict(_ARSENAL_CREATE_BASE)
    )
    assert not result.is_error, result.content
    create = seq.call("post", "/api/threat_arsenals")
    assert create is not None
    assert create["json"].get("action_tags") == []  # created, just untagged


# ── Findings-aware contract selection (output_types "providing" filter) ────


def test_resolve_output_types_canonicalizes_and_dedupes():
    from app.integrations.providers.openaev._common import _resolve_output_types

    resolved, err = _resolve_output_types(["IPv4", "ip", "port", "port"])
    assert err == ""
    # "IPv4" lowercases to ipv4, "ip" aliases to ipv4 -> deduped to one entry.
    assert resolved == ["ipv4", "port"]


def test_resolve_output_types_rejects_unknown_naming_allowed_set():
    from app.integrations.providers.openaev._common import _resolve_output_types

    resolved, err = _resolve_output_types(["bogus"])
    assert resolved == []
    # A typo must fail loudly here - OpenAEV would otherwise resolve it to the
    # empty set and return zero rows with a 200 (a silent "no matches").
    assert "output_types must be drawn from" in err
    assert "ipv4" in err


def test_providing_filter_clause_uses_eq_any_of():
    from app.integrations.providers.openaev._common import (
        PROVIDING_FILTER_KEY,
        _providing_filter_clause,
    )

    clause = _providing_filter_clause(["ipv4", "port"])
    assert clause == {
        "key": PROVIDING_FILTER_KEY,
        "operator": "eq",
        "values": ["ipv4", "port"],
    }


def test_merge_filter_clause_appends_and_wraps():
    from app.integrations.providers.openaev._common import _merge_filter_clause

    clause = {"key": "injector_contract_providing", "operator": "eq", "values": ["cve"]}
    # No existing group -> wraps in an AND group.
    merged, err = _merge_filter_clause(None, clause)
    assert err == ""
    assert merged == {"mode": "and", "filters": [clause]}
    # Existing group -> appends without mutating the original. A missing mode
    # defaults to "and" (matching the platform), so it merges too.
    existing = {"mode": "and", "filters": [{"key": "x", "operator": "eq", "values": ["1"]}]}
    merged, err = _merge_filter_clause(existing, clause)
    assert err == ""
    assert merged["filters"] == [existing["filters"][0], clause]
    assert len(existing["filters"]) == 1  # original untouched
    merged, err = _merge_filter_clause({"filters": []}, clause)
    assert err == ""
    assert merged["filters"] == [clause]


def test_merge_filter_clause_rejects_or_mode():
    # OpenAEV filter groups are flat (no nesting), so a narrowing clause cannot
    # be AND-composed with an OR group - appending would broaden the results.
    # That case must fail loudly instead of silently changing semantics.
    from app.integrations.providers.openaev._common import _merge_filter_clause

    clause = {"key": "injector_contract_providing", "operator": "eq", "values": ["cve"]}
    or_group = {"mode": "or", "filters": [{"key": "x", "operator": "eq", "values": ["1"]}]}
    merged, err = _merge_filter_clause(or_group, clause)
    assert merged == {}
    assert "mode is 'and'" in err
    assert len(or_group["filters"]) == 1  # original untouched


def test_fmt_injector_contract_surfaces_producible_findings():
    from app.integrations.providers.openaev._common import _fmt_injector_contract

    out = _fmt_injector_contract(
        {
            "injector_contract_id": "c1",
            "injector_contract_labels": {"en": "Nmap scan"},
            "injector_contract_providing": ["ipv4", "port"],
        }
    )
    assert "Produces findings: ipv4, port" in out


def test_fmt_injector_contract_flags_no_findings_on_empty_list():
    from app.integrations.providers.openaev._common import _fmt_injector_contract

    native = _fmt_injector_contract(
        {
            "injector_contract_id": "c2",
            "injector_contract_labels": {"en": "NetExec SMB"},
            "injector_contract_providing": [],
        }
    )
    assert "Produces findings: none" in native
    assert "native injector" in native
    assert "cannot be duplicated" in native
    assert "FORK CANDIDATE" not in native

    payload = _fmt_injector_contract(
        {
            "injector_contract_id": "c-wifi",
            "injector_contract_labels": {"en": "Wi-Fi netsh harvest"},
            "injector_contract_payload": {"payload_id": "p-wifi"},
            "injector_contract_providing": [],
        }
    )
    assert "FORK CANDIDATE" in payload
    assert "payload-based (duplicable)" in payload
    assert "cannot be duplicated" not in payload


def test_is_payload_based_contract_detects_payload_shapes():
    from app.integrations.providers.openaev._common import _is_payload_based_contract

    assert _is_payload_based_contract(
        {"injector_contract_payload": {"payload_id": "p1"}}
    )
    assert _is_payload_based_contract({"injector_contract_payload_type": "Command"})
    assert _is_payload_based_contract({"action_payload": {"payload_id": "p2"}})
    assert not _is_payload_based_contract({"injector_contract_id": "native"})
    assert not _is_payload_based_contract({"action_id": "nxc", "action_payload": {}})


def test_without_providing_filter_drops_producing_clause():
    from app.integrations.providers.openaev._common import (
        PROVIDING_FILTER_KEY,
        _without_providing_filter,
    )

    only_providing = {
        "mode": "and",
        "filters": [
            {"key": PROVIDING_FILTER_KEY, "operator": "eq", "values": ["credentials"]},
        ],
    }
    assert _without_providing_filter(only_providing) is None
    mixed = {
        "mode": "and",
        "filters": [
            {"key": "action_source", "operator": "eq", "values": ["FILIGRAN"]},
            {"key": PROVIDING_FILTER_KEY, "operator": "eq", "values": ["credentials"]},
        ],
    }
    stripped = _without_providing_filter(mixed)
    assert stripped is not None
    assert [f["key"] for f in stripped["filters"]] == ["action_source"]
    assert _without_providing_filter(None) is None


def test_merge_fork_candidates_keeps_producing_then_payload_extras():
    from app.integrations.providers.openaev._common import _merge_fork_candidates

    producing = [
        {"injector_contract_id": "prod-1", "injector_contract_providing": ["credentials"]},
        {"action_id": "prod-2", "action_providing": ["port"]},
    ]
    unfiltered = [
        producing[0],  # duplicate id - skip
        {
            "injector_contract_id": "fork-wifi",
            "injector_contract_payload": {"payload_id": "p-wifi"},
            "injector_contract_providing": [],
        },
        {
            "injector_contract_id": "native-nxc",
            "injector_contract_providing": [],
        },
        {
            "action_id": "fork-chrome",
            "action_payload": {"payload_id": "p-chrome"},
        },
    ]
    merged = _merge_fork_candidates(producing, unfiltered)
    ids = [
        item.get("injector_contract_id") or item.get("action_id") for item in merged
    ]
    assert ids == ["prod-1", "prod-2", "fork-wifi", "fork-chrome"]


def test_fmt_injector_contract_surfaces_attack_patterns():
    from app.integrations.providers.openaev._common import _fmt_injector_contract

    out = _fmt_injector_contract(
        {
            "injector_contract_id": "c3",
            "injector_contract_labels": {"en": "NetExec SMB"},
            "injector_contract_attack_patterns": [
                "T1003",
                {"attack_pattern_external_id": "T1059"},
            ],
            "injector_contract_attack_patterns_external_id": ["T1021"],
            "injector_contract_injector_type": "openaev_implant",
            "injector_contract_payload": {
                "payload_id": "p1",
                "payload_name": "nxc smb",
                "command_executor": "psh",
            },
            "injector_contract_providing": ["credentials"],
        }
    )
    assert "T1021" in out
    assert "T1003" in out
    assert "T1059" in out
    assert "Executor: psh" in out
    assert "Injector type: openaev_implant" in out
    assert "Produces findings: credentials" in out


def test_normalize_output_parsers_rejects_finding_type_as_parser_type():
    from app.integrations.providers.openaev._common import _normalize_output_parsers

    body = {
        "action_output_parsers": [
            {
                "output_parser_mode": "STDOUT",
                "output_parser_type": "credentials",
                "output_parser_contract_output_elements": [
                    {"contract_output_element_type": "credentials"}
                ],
            }
        ]
    }
    err = _normalize_output_parsers(body)
    assert err is not None
    assert "REGEX" in err
    assert "contract_output_element_type" in err
    assert body["action_output_parsers"][0]["output_parser_type"] == "credentials"


def test_normalize_output_parsers_accepts_regex_and_alias():
    # A lowercase 'regex' alias canonicalizes to 'REGEX'. RC-2: an element-less
    # parser is now REJECTED (it would store an action with no outputs), so each
    # parser carries a valid finding element - the alias handling is what this
    # asserts, in isolation from the new element requirement.
    from app.integrations.providers.openaev._common import _normalize_output_parsers

    def _element() -> dict:
        return {
            "contract_output_element_type": "username",
            "contract_output_element_rule": r"user=(\w+)",
        }

    body = {
        "payload_output_parsers": [
            {
                "output_parser_mode": "STDOUT",
                "output_parser_type": "REGEX",
                "output_parser_contract_output_elements": [_element()],
            },
            {
                "output_parser_mode": "STDERR",
                "output_parser_type": "regex",
                "output_parser_contract_output_elements": [_element()],
            },
        ]
    }
    assert _normalize_output_parsers(body) is None
    assert body["payload_output_parsers"][0]["output_parser_type"] == "REGEX"
    assert body["payload_output_parsers"][1]["output_parser_type"] == "REGEX"


def test_normalize_output_parsers_defaults_missing_type_to_regex():
    # A missing output_parser_type defaults to REGEX. RC-2: the parser must still
    # carry a finding element or it is rejected as element-less, so one is
    # included - this isolates the type-defaulting behavior.
    from app.integrations.providers.openaev._common import _normalize_output_parsers

    body = {
        "action_output_parsers": [
            {
                "output_parser_mode": "STDOUT",
                "output_parser_contract_output_elements": [
                    {
                        "contract_output_element_type": "username",
                        "contract_output_element_rule": r"user=(\w+)",
                    }
                ],
            },
        ]
    }
    assert _normalize_output_parsers(body) is None
    assert body["action_output_parsers"][0]["output_parser_type"] == "REGEX"


def test_normalize_output_parsers_derives_missing_element_key():
    # RC-2: contract_output_element_name / _key are @NotBlank labels with NO
    # chaining meaning, and the model kept omitting them and 400-ing the finalize.
    # They are now DERIVED from the finding type instead of rejected (the finding
    # SEMANTICS live in _type + _is_finding, which is what the chaining engine
    # keys on). A missing key must therefore be filled, not error.
    from app.integrations.providers.openaev._common import _normalize_output_parsers

    body = {
        "action_output_parsers": [
            {
                "output_parser_mode": "STDOUT",
                "output_parser_type": "REGEX",
                "output_parser_contract_output_elements": [
                    {
                        "contract_output_element_rule": "(?i)user=(\\w+)",
                        "contract_output_element_name": "User",
                        "contract_output_element_type": "username",
                        # contract_output_element_key deliberately omitted
                    }
                ],
            }
        ]
    }
    err = _normalize_output_parsers(body)
    assert err is None
    element = body["action_output_parsers"][0][
        "output_parser_contract_output_elements"
    ][0]
    # Derived (non-blank) so the finalize PUT satisfies @NotBlank, and the
    # finding flag is forced on so the copy actually produces the finding.
    assert element["contract_output_element_key"]
    assert element["contract_output_element_is_finding"] is True


def test_normalize_output_parsers_requires_regex_group_index_values():
    # RegexGroupInput has @NotBlank field + index_values, so a group missing
    # index_values must be caught client-side rather than 400ing at finalize.
    from app.integrations.providers.openaev._common import _normalize_output_parsers

    body = {
        "action_output_parsers": [
            {
                "output_parser_mode": "STDOUT",
                "output_parser_type": "REGEX",
                "output_parser_contract_output_elements": [
                    {
                        "contract_output_element_rule": "(?i)user=(\\w+)",
                        "contract_output_element_name": "User",
                        "contract_output_element_key": "user",
                        "contract_output_element_type": "username",
                        "contract_output_element_regex_groups": [
                            {"regex_group_field": "username"}  # index_values omitted
                        ],
                    }
                ],
            }
        ]
    }
    err = _normalize_output_parsers(body)
    assert err is not None
    assert "regex_group_index_values" in err


def test_normalize_output_parsers_accepts_complete_element_with_group():
    # A fully-specified element with a complete regex group passes untouched.
    from app.integrations.providers.openaev._common import _normalize_output_parsers

    body = {
        "action_output_parsers": [
            {
                "output_parser_mode": "STDOUT",
                "output_parser_type": "REGEX",
                "output_parser_contract_output_elements": [
                    {
                        "contract_output_element_rule": "(?i)user=(\\w+)",
                        "contract_output_element_name": "User",
                        "contract_output_element_key": "user",
                        "contract_output_element_type": "username",
                        "contract_output_element_regex_groups": [
                            {
                                "regex_group_field": "username",
                                "regex_group_index_values": "$1$",
                            }
                        ],
                    }
                ],
            }
        ]
    }
    assert _normalize_output_parsers(body) is None


def test_fmt_arsenal_action_surfaces_producible_findings():
    from app.integrations.providers.openaev._common import _fmt_arsenal_action

    out = _fmt_arsenal_action(
        {"action_name": "CVE probe", "action_id": "a1", "action_providing": ["cve"]}
    )
    assert "Produces findings: cve" in out


async def test_injector_contract_search_pushes_providing_filter(monkeypatch):
    # output_types must be translated into an injector_contract_providing eq
    # clause on the FIRST search (reuse). A second unfiltered search collects
    # payload-based fork candidates.
    from app.integrations.providers.openaev import core

    calls: list[dict] = []

    # Routed through _common._api now (WS2 JWT auto-refresh on long runs), so patch
    # that seam - the request BODY the assertions inspect is unchanged.
    async def fake_api(base_url, headers, method, path, action, *, json=None, **kw):
        calls.append(json)
        return _FakeResp({"content": [], "totalElements": 0}), None

    monkeypatch.setattr(core, "_api", fake_api)
    executor, _ = _build()
    result = await _run(
        executor,
        "search_openaev_injector_contracts",
        {"search": "nmap", "output_types": ["ipv4", "port"]},
    )
    assert not result.is_error, result.content
    assert len(calls) == 2
    fg = calls[0]["filterGroup"]
    assert fg["mode"] == "and"
    assert fg["filters"] == [
        {
            "key": "injector_contract_providing",
            "operator": "eq",
            "values": ["ipv4", "port"],
        }
    ]
    assert "filterGroup" not in calls[1]
    assert calls[1]["textSearch"] == "nmap"


async def test_injector_contract_search_merges_payload_based_fork_candidates(monkeypatch):
    # Production failure: providing filter hid payload-based catalog actions
    # that produce none. The second search must merge those as FORK CANDIDATES
    # and drop native injectors.
    from app.integrations.providers.openaev import core

    producing = [
        {
            "injector_contract_id": "already-produces",
            "injector_contract_labels": {"en": "LSASS dump"},
            "injector_contract_payload": {"payload_id": "p-lsass"},
            "injector_contract_providing": ["credentials"],
        }
    ]
    unfiltered = [
        producing[0],
        {
            "injector_contract_id": "wifi-netsh",
            "injector_contract_labels": {"en": "Wi-Fi netsh harvest"},
            "injector_contract_payload": {"payload_id": "p-wifi"},
            "injector_contract_providing": [],
        },
        {
            "injector_contract_id": "netexec-smb",
            "injector_contract_labels": {"en": "NetExec SMB"},
            "injector_contract_providing": [],
        },
    ]

    async def fake_api(base_url, headers, method, path, action, *, json=None, **kw):
        if json and json.get("filterGroup"):
            return _FakeResp({"content": producing, "totalElements": 1}), None
        return _FakeResp({"content": unfiltered, "totalElements": 3}), None

    monkeypatch.setattr(core, "_api", fake_api)
    executor, _ = _build()
    result = await _run(
        executor,
        "search_openaev_injector_contracts",
        {"search": "credentials", "output_types": ["credentials"]},
    )
    assert not result.is_error, result.content
    assert "LSASS dump" in result.content
    assert "Wi-Fi netsh harvest" in result.content
    assert "FORK CANDIDATE" in result.content
    assert "NetExec SMB" not in result.content


async def test_attack_pattern_search_accepts_query_alias(monkeypatch):
    # LLMs often call this tool with `query` (the common search-tool convention)
    # instead of the declared `search`. The alias must reach the request body as
    # text_search so a mislabelled argument does not waste an agent iteration.
    from app.integrations.providers.openaev import core

    captured: dict = {}

    async def fake_safe_request(client, method, url, provider, action, *, headers=None, json=None):
        captured["url"] = url
        captured["json"] = json
        return _FakeResp({"content": [], "totalElements": 0}), None

    monkeypatch.setattr(core, "scoped_safe_request", fake_safe_request)
    executor, _ = _build()
    result = await _run(
        executor, "search_openaev_attack_patterns", {"query": "T1059"}
    )
    assert not result.is_error, result.content
    assert captured["url"].endswith("/api/attack_patterns/search")
    assert captured["json"]["textSearch"] == "T1059"


async def test_attack_pattern_search_prefers_search_over_query_alias(monkeypatch):
    # When both are supplied, the declared `search` wins over the `query` alias.
    from app.integrations.providers.openaev import core

    captured: dict = {}

    async def fake_safe_request(client, method, url, provider, action, *, headers=None, json=None):
        captured["json"] = json
        return _FakeResp({"content": [], "totalElements": 0}), None

    monkeypatch.setattr(core, "scoped_safe_request", fake_safe_request)
    executor, _ = _build()
    result = await _run(
        executor,
        "search_openaev_attack_patterns",
        {"search": "T1003", "query": "T1059"},
    )
    assert not result.is_error, result.content
    assert captured["json"]["textSearch"] == "T1003"


async def test_injector_contract_search_scope_uses_canonical_tokens(monkeypatch):
    # The empty-result diagnostic must reflect the CANONICAL tokens actually
    # sent in the filter (aliases resolved, casing normalized), not the raw
    # input - "ip"/"PORT" ran as ipv4/port, so that is what the message says.
    from app.integrations.providers.openaev import core

    async def fake_api(base_url, headers, method, path, action, *, json=None, **kw):
        return _FakeResp({"content": [], "totalElements": 0}), None

    monkeypatch.setattr(core, "_api", fake_api)
    executor, _ = _build()
    result = await _run(
        executor,
        "search_openaev_injector_contracts",
        {"output_types": ["ip", "PORT"]},
    )
    assert not result.is_error, result.content
    assert "producing ipv4, port" in result.content
    assert "producing ip," not in result.content


async def test_search_findings_scopes_to_simulation(monkeypatch):
    # On a busy tenant an unscoped findings search can surface / PROOF-cite / promote
    # ANOTHER run's finding, so simulation_id scopes it via the finding_simulation eq
    # filter (item 4). Routed through _api (WS2 JWT auto-refresh on long runs).
    from app.integrations.providers.openaev import core

    captured: dict = {}

    async def fake_api(
        base_url, headers, method, path, action, *, json=None, params=None, **kw
    ):
        captured.update(path=path, json=json, params=params)
        return _FakeResp({"content": [], "totalElements": 0}), None

    monkeypatch.setattr(core, "_api", fake_api)
    executor, _ = _build()
    result = await _run(
        executor,
        "search_openaev_findings",
        {"search": "smb", "simulation_id": "sim-123"},
    )
    assert not result.is_error, result.content
    assert captured["path"] == "/api/findings/search"
    assert captured["params"] == {"distinct": "true"}
    assert captured["json"]["filterGroup"] == {
        "mode": "and",
        "filters": [
            {"key": "finding_simulation", "operator": "eq", "values": ["sim-123"]}
        ],
    }
    assert "in simulation sim-123" in result.content  # empty-state scope note


async def test_search_findings_unscoped_has_no_simulation_filter(monkeypatch):
    # Without simulation_id the search stays unscoped (backward-compatible): no
    # filterGroup is sent, so existing non-autonomous callers are unaffected.
    from app.integrations.providers.openaev import core

    captured: dict = {}

    async def fake_api(
        base_url, headers, method, path, action, *, json=None, params=None, **kw
    ):
        captured.update(json=json)
        return _FakeResp({"content": [], "totalElements": 0}), None

    monkeypatch.setattr(core, "_api", fake_api)
    executor, _ = _build()
    result = await _run(executor, "search_openaev_findings", {"search": "smb"})
    assert not result.is_error, result.content
    assert "filterGroup" not in captured["json"]


async def test_search_findings_by_inject_uses_the_per_inject_route(monkeypatch):
    # inject_id reads the findings ONE inject produced through OpenAEV's per-inject
    # route (it checks the inject is in scope), the id path-quoted.
    from app.integrations.providers.openaev import core

    captured: dict = {}

    async def fake_api(base_url, headers, method, path, action, *, json=None, params=None, **kw):
        captured.update(method=method, path=path, json=json, params=params)
        return _FakeResp({"content": [], "totalElements": 0}), None

    monkeypatch.setattr(core, "_api", fake_api)
    executor, _ = _build()
    result = await _run(executor, "search_openaev_findings", {"inject_id": " inj/1 "})
    assert not result.is_error, result.content
    assert captured["method"] == "post"
    assert captured["path"] == "/api/findings/injects/inj%2F1/search"
    assert captured["params"] == {"distinct": "true"}
    assert "filterGroup" not in captured["json"]
    assert "inject inj/1" in result.content


async def test_search_findings_by_inject_passes_a_not_found_through(monkeypatch):
    # An unknown inject id answers OpenAEV's error, never "No findings found".
    from app.integrations.providers.openaev import core

    async def fake_api(*args, **kw):
        return None, "OpenAEV error 404: inject not found"

    monkeypatch.setattr(core, "_api", fake_api)
    executor, _ = _build()
    result = await _run(executor, "search_openaev_findings", {"inject_id": "nope"})
    assert "404" in result.content
    assert "No findings found" not in result.content


def test_fmt_finding_surfaces_inject_id():
    # The producing inject id ties a finding back to the step that emitted it (item 4).
    from app.integrations.providers.openaev import _common

    out = _common._fmt_finding(
        {
            "finding_id": "f1",
            "finding_value": "445",
            "finding_type": "port",
            "finding_inject_id": "inj-9",
        }
    )
    assert "Inject: inj-9" in out
    # Absent -> no Inject line (inert-safe).
    out2 = _common._fmt_finding({"finding_id": "f2", "finding_value": "x"})
    assert "Inject:" not in out2


def test_output_types_schema_descriptions_list_full_allowed_set():
    # The allowed-token list shown to tool callers is derived from
    # CONTRACT_OUTPUT_TYPES; every canonical token must appear so the schema
    # cannot drift from what _resolve_output_types actually accepts.
    from app.integrations.providers.openaev._common import CONTRACT_OUTPUT_TYPES

    _, tools = _build()
    for tool_name in ("search_openaev_injector_contracts", "manage_openaev_threat_arsenal"):
        tool = next(t for t in tools if t.name == tool_name)
        description = tool.input_schema["properties"]["output_types"]["description"]
        missing = [t for t in CONTRACT_OUTPUT_TYPES if t not in description]
        assert not missing, f"{tool_name} output_types description omits: {missing}"


async def test_injector_contract_search_rejects_unknown_output_type(monkeypatch):
    from app.integrations.providers.openaev import core

    called = {"hit": False}

    async def fake_safe_request(*a, **k):
        called["hit"] = True
        return _FakeResp({"content": []}), None

    monkeypatch.setattr(core, "scoped_safe_request", fake_safe_request)
    executor, _ = _build()
    result = await _run(
        executor,
        "search_openaev_injector_contracts",
        {"output_types": ["bogus"]},
    )
    assert not result.is_error, result.content
    assert "output_types must be drawn from" in result.content
    assert called["hit"] is False  # rejected before any request left


async def test_threat_arsenal_list_merges_providing_filter(monkeypatch):
    # output_types on a list becomes an injector_contract_providing clause merged
    # (AND) with any caller-supplied filter_group.
    import json

    from app.integrations.providers.openaev import threat_arsenal

    fake = _RecordingApi({"content": [], "totalElements": 0})
    monkeypatch.setattr(threat_arsenal, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "manage_openaev_threat_arsenal",
        {
            "action": "list",
            "filter_group": json.dumps(
                {"mode": "and", "filters": [{"key": "action_source", "operator": "eq", "values": ["FILIGRAN"]}]}
            ),
            "output_types": ["credentials"],
        },
    )
    assert not result.is_error, result.content
    providing = {
        "key": "injector_contract_providing",
        "operator": "eq",
        "values": ["credentials"],
    }
    assert len(fake.calls) == 2
    first_fg = fake.calls[0]["json"]["filterGroup"]
    assert providing in first_fg["filters"]
    assert any(f.get("key") == "action_source" for f in first_fg["filters"])
    second_fg = fake.calls[1]["json"].get("filterGroup") or {"filters": []}
    assert providing not in second_fg.get("filters", [])
    assert any(f.get("key") == "action_source" for f in second_fg.get("filters", []))


async def test_threat_arsenal_list_rejects_unknown_output_type(monkeypatch):
    from app.integrations.providers.openaev import threat_arsenal

    fake = _RecordingApi({"content": []})
    monkeypatch.setattr(threat_arsenal, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "manage_openaev_threat_arsenal",
        {"action": "list", "output_types": ["bogus"]},
    )
    assert not result.is_error, result.content
    assert "output_types must be drawn from" in result.content
    assert not fake.calls  # rejected before any request left


async def test_threat_arsenal_rejects_output_types_on_non_list_action(monkeypatch):
    # output_types is documented for 'list'/'list_non_tabletop' only. On any
    # other action it would be silently ignored (get/create/update) or silently
    # reshape the scope of bulk_delete / the counts, so it must fail loudly.
    from app.integrations.providers.openaev import threat_arsenal

    fake = _RecordingApi({})
    monkeypatch.setattr(threat_arsenal, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "manage_openaev_threat_arsenal",
        {"action": "get", "action_id": "a1", "output_types": ["credentials"]},
    )
    assert not result.is_error, result.content
    assert "only applies to action='list'" in result.content
    assert not fake.calls  # rejected before any request left


async def test_threat_arsenal_list_rejects_or_filter_group_with_output_types(monkeypatch):
    # An OR filter_group cannot AND-compose with the providing clause (flat
    # filter groups) - the tool must refuse rather than broaden the results.
    import json

    from app.integrations.providers.openaev import threat_arsenal

    fake = _RecordingApi({"content": []})
    monkeypatch.setattr(threat_arsenal, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "manage_openaev_threat_arsenal",
        {
            "action": "list",
            "filter_group": json.dumps(
                {"mode": "or", "filters": [{"key": "action_source", "operator": "eq", "values": ["FILIGRAN"]}]}
            ),
            "output_types": ["credentials"],
        },
    )
    assert not result.is_error, result.content
    assert "mode is 'and'" in result.content
    assert not fake.calls  # rejected before any request left


def test_native_duplicate_error_rewrites_payload_based_message_any_status():
    from app.integrations.providers.openaev.threat_arsenal import (
        _NATIVE_DUPLICATE_MSG,
        _native_duplicate_error,
    )

    # Current OpenAEV releases reject a native-injector duplicate with this
    # wording (a 400). The rewrite keys on the "not payload-based" substring,
    # not the status code.
    current = (
        "This threat arsenal item is a native injector (not payload-based) and "
        "cannot be duplicated. Reuse its injector contract id as-is."
    )
    assert _native_duplicate_error(f"400: {current}") == _NATIVE_DUPLICATE_MSG
    assert _native_duplicate_error(f"404: {current}") == _NATIVE_DUPLICATE_MSG
    # The legacy wording (older OpenAEV, surfaced as a 404) is still matched so a
    # mixed-version deployment keeps the actionable message; the 404
    # RBAC-three-meanings text must be suppressed either way.
    legacy = "Only threat arsenal items based on a payload can be duplicated"
    assert _native_duplicate_error(f"400: {legacy}") == _NATIVE_DUPLICATE_MSG
    annotated_404 = (
        f"404: {legacy} In OpenAEV a 404 can mean three different things: the "
        "entity really does not exist"
    )
    assert _native_duplicate_error(annotated_404) == _NATIVE_DUPLICATE_MSG
    assert "RBAC" not in (_native_duplicate_error(annotated_404) or "")
    assert "three different things" not in (_native_duplicate_error(annotated_404) or "")
    # Unrelated errors pass through untouched, whatever their status.
    assert _native_duplicate_error("404: action not found") is None
    assert _native_duplicate_error("400: bad request") is None
    assert _native_duplicate_error("500 internal error") is None


async def test_duplicate_native_injector_returns_structured_error(monkeypatch):
    from app.integrations.providers.openaev import threat_arsenal
    from app.integrations.providers.openaev.threat_arsenal import _NATIVE_DUPLICATE_MSG

    # Current OpenAEV releases return this as a 400 with the "not payload-based"
    # wording (no _annotate_not_found RBAC text). The rewrite is status-agnostic
    # and matches the current wording, so the structured message is returned.
    class _ErrApi:
        async def __call__(self, *a, **k):
            return None, (
                "HTTP 400: This threat arsenal item is a native injector (not "
                "payload-based) and cannot be duplicated. Reuse its injector "
                "contract id as-is."
            )

    monkeypatch.setattr(threat_arsenal, "_api", _ErrApi())
    executor, _ = _build()
    result = await _run(
        executor,
        "manage_openaev_threat_arsenal",
        {"action": "duplicate", "action_id": "netexec_smb"},
    )
    assert not result.is_error, result.content
    assert result.content == _NATIVE_DUPLICATE_MSG
    assert "three different things" not in result.content


# ── Threat arsenal: atomic verified fork (one call: dup -> rename -> tag ->
#    REGEX parsers -> read-back verify) + deterministic cross-cycle dedup ────

_FORK_PARSERS = [
    {
        "output_parser_mode": "STDOUT",
        "output_parser_type": "REGEX",
        "output_parser_contract_output_elements": [
            {
                "contract_output_element_is_finding": True,
                "contract_output_element_rule": "(?i)key=(.+)",
                "contract_output_element_name": "Credential",
                "contract_output_element_key": "credential",
                "contract_output_element_type": "credentials",
                "contract_output_element_regex_groups": [],
            }
        ],
    }
]


def _arsenal_source(**overrides):
    base = {
        "injector_contract_id": "src1",
        "action_id": "src1",
        "action_labels": {"en": "Wi-Fi netsh harvest"},
        "action_domains": ["d1"],
        "action_execution_arch": "ALL_ARCHITECTURES",
        "action_expectations": ["DETECTION"],
        "command_executor": "psh",
        "command_content": "netsh wlan show profiles",
        "action_tags": ["srcTag"],
        "action_providing": [],
    }
    base.update(overrides)
    return base


class _ArsenalStoreApi:
    """Stateful ``_api`` fake modelling enough of the threat-arsenal REST surface
    to exercise the atomic fork end to end: tag upsert, duplicate, search-by-tag,
    GET, and a PUT that REFLECTS onto the store.

    The read-back verification GET must see the merged state the PUT wrote, so a
    plain per-route canned responder is not enough - the store transition is the
    whole point (rename + tag + parsers land in ONE tool call).
    """

    def __init__(self, store):
        self.store = store  # id -> action dict
        self.calls: list[dict] = []
        self._dup_seq = 0

    @staticmethod
    def _name(action):
        labels = action.get("action_labels")
        if isinstance(labels, dict) and labels.get("en"):
            return labels["en"]
        return action.get("action_name") or ""

    @staticmethod
    def _tag_id(name):
        if name == "ai generated":
            return "ai-tag"
        if name.startswith("ai-fork-src:"):
            return "marker-" + name.split(":", 1)[1]
        return "tag-" + name

    async def __call__(
        self, base_url, headers, method, path, action, *,
        json=None, params=None, tenant_id="",
    ):
        self.calls.append({"method": method, "path": path, "json": json})
        if method == "post" and path == "/api/tags/upsert":
            name = (json or {}).get("tag_name", "")
            return _FakeResp({"tag_id": self._tag_id(name), "tag_name": name}), None
        if method == "post" and path.endswith("/duplicate"):
            src_id = path.rsplit("/", 2)[-2]
            src = self.store.get(src_id, {})
            self._dup_seq += 1
            new_id = f"fork{self._dup_seq}"
            copy = {
                k: (list(v) if isinstance(v, list)
                    else dict(v) if isinstance(v, dict) else v)
                for k, v in src.items()
            }
            copy["injector_contract_id"] = new_id
            copy["action_id"] = new_id
            # OpenAEV appends "(duplicate)" and the raw copy produces nothing yet.
            copy["action_labels"] = {"en": f"{self._name(src)} (duplicate)"}
            copy["action_name"] = f"{self._name(src)} (duplicate)"
            copy["action_providing"] = []
            self.store[new_id] = copy
            return _FakeResp(copy), None
        if method == "post" and path.endswith("/search"):
            wanted = None
            fg = (json or {}).get("filterGroup") or {}
            for f in fg.get("filters", []):
                if f.get("key") == "injector_contract_tags":
                    vals = f.get("values") or []
                    wanted = vals[0] if vals else None
            matches = [
                a for a in self.store.values()
                if wanted and wanted in (a.get("action_tags") or [])
            ]
            return _FakeResp({"content": matches}), None
        if method == "get":
            aid = path.rsplit("/", 1)[-1]
            return _FakeResp(self.store.get(aid, {})), None
        if method == "put":
            aid = path.rsplit("/", 1)[-1]
            body = json or {}
            # Mirror ThreatArsenalService.updateActionPayloadBased: a payload-based
            # PUT whose action_expectations is null/absent is HTTP 400. An empty
            # list is valid ("no expectations"). Without this, a fork test could
            # pass even when the finalize body omitted the field.
            if body.get("action_expectations") is None:
                return None, (
                    "HTTP 400: action_expectations is required for payload-based "
                    "actions"
                )
            if not body.get("action_execution_arch"):
                return None, (
                    "HTTP 400: action_execution_arch is required for payload-based "
                    "actions"
                )
            cur = self.store.setdefault(aid, {})
            cur.update(body)
            nm = body.get("action_name")
            if nm:
                cur["action_labels"] = {"en": nm}
            parsers = body.get("action_output_parsers") or []
            # Landing a finding-typed REGEX parser makes the copy PRODUCE findings.
            cur["action_providing"] = ["credentials"] if parsers else []
            return _FakeResp(cur), None
        return _FakeResp({}), None

    def count(self, method, needle):
        return sum(
            1 for c in self.calls if c["method"] == method and needle in c["path"]
        )


async def test_fork_is_atomic_single_duplicate_renamed_tagged_and_verified(monkeypatch):
    # ONE fork call: duplicate -> strip "(duplicate)" -> stamp 'ai generated' +
    # the source-unique provenance marker -> apply REGEX parsers -> read back and
    # confirm the copy PRODUCES findings. Exactly one duplicate is issued.
    from app.integrations.providers.openaev import threat_arsenal

    store = {"src1": _arsenal_source()}
    fake = _ArsenalStoreApi(store)
    monkeypatch.setattr(threat_arsenal, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "manage_openaev_threat_arsenal",
        {
            "action": "fork",
            "action_id": "src1",
            "config": json.dumps({"action_output_parsers": _FORK_PARSERS}),
        },
    )
    assert not result.is_error, result.content
    assert result.content.startswith("Forked"), result.content
    # Exactly one copy was minted (no half-forked second copy).
    assert fake.count("post", "/duplicate") == 1
    fork = store["fork1"]
    # Renamed (no tell-tale "(duplicate)"), AI + provenance tagged, findings-producing.
    assert "(duplicate)" not in fork["action_labels"]["en"]
    assert "ai-tag" in fork["action_tags"]
    assert "marker-src1" in fork["action_tags"]
    assert fork["action_providing"] == ["credentials"]


async def test_fork_reuses_existing_fork_instead_of_making_a_second_copy(monkeypatch):
    # Deterministic cross-cycle dedup: an earlier fork of the SAME source carries
    # the source-unique provenance marker, so a repeat fork REUSES and repairs it
    # (re-applying parsers) rather than minting a duplicate - no more twin copies.
    from app.integrations.providers.openaev import threat_arsenal

    existing = _arsenal_source(
        injector_contract_id="forkOld",
        action_id="forkOld",
        action_labels={"en": "Wi-Fi netsh harvest"},
        action_tags=["srcTag", "ai-tag", "marker-src1"],
        action_providing=[],  # parsers were lost previously; reuse must repair
    )
    store = {"src1": _arsenal_source(), "forkOld": existing}
    fake = _ArsenalStoreApi(store)
    monkeypatch.setattr(threat_arsenal, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "manage_openaev_threat_arsenal",
        {
            "action": "fork",
            "action_id": "src1",
            "config": json.dumps({"action_output_parsers": _FORK_PARSERS}),
        },
    )
    assert not result.is_error, result.content
    assert result.content.startswith("Reused"), result.content
    # No second copy was ever created.
    assert fake.count("post", "/duplicate") == 0
    # The existing fork was repaired to produce findings.
    assert store["forkOld"]["action_providing"] == ["credentials"]
    assert "fork1" not in store


async def test_fork_finalize_sends_expectations_when_source_lacks_them(monkeypatch):
    # The in-place 400 root cause on the FORK path: a payload-based dead-end whose
    # payload has no expectations (the duplicate inherits that null) must still
    # finalize in place. The finalize PUT has to carry action_expectations or
    # OpenAEV's updateActionPayloadBased 400s and the copy is abandoned half-forked
    # (the orphaned "(duplicate)"). The fork must complete in ONE call instead.
    from app.integrations.providers.openaev import threat_arsenal

    # Jackson GET shape: action_expectations is JSON null, inherited by the copy.
    source = _arsenal_source(action_expectations=None)
    store = {"src1": source}
    fake = _ArsenalStoreApi(store)
    monkeypatch.setattr(threat_arsenal, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "manage_openaev_threat_arsenal",
        {
            "action": "fork",
            "action_id": "src1",
            "config": json.dumps({"action_output_parsers": _FORK_PARSERS}),
        },
    )
    assert not result.is_error, result.content
    assert result.content.startswith("Forked"), result.content
    # Exactly one copy minted, and the finalize PUT (the one carrying the parsers)
    # also carried action_expectations, so the in-place finalize succeeded.
    assert fake.count("post", "/duplicate") == 1
    finalize_puts = [
        c
        for c in fake.calls
        if c["method"] == "put" and "action_output_parsers" in (c["json"] or {})
    ]
    assert finalize_puts, "the fork must issue a finalize PUT carrying the parsers"
    assert finalize_puts[0]["json"]["action_expectations"] == []
    assert finalize_puts[0]["json"]["action_execution_arch"] == "ALL_ARCHITECTURES"
    assert store["fork1"]["action_providing"] == ["credentials"]


async def test_fork_domainless_source_falls_through_to_inherited_domains(monkeypatch):
    # A domain-less SOURCE (its duplicate inherits no domains) must NOT be blocked
    # by the pre-PUT domain guard: the fork falls through to the copy's
    # server-inherited (here empty) domains and completes, rather than hard-failing
    # client-side and orphaning the just-duplicated copy.
    from app.integrations.providers.openaev import threat_arsenal

    store = {"src1": _arsenal_source(action_domains=[])}
    fake = _ArsenalStoreApi(store)
    monkeypatch.setattr(threat_arsenal, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "manage_openaev_threat_arsenal",
        {
            "action": "fork",
            "action_id": "src1",
            "config": json.dumps({"action_output_parsers": _FORK_PARSERS}),
        },
    )
    assert not result.is_error, result.content
    assert result.content.startswith("Forked"), result.content
    assert "no security domain" not in result.content
    assert fake.count("post", "/duplicate") == 1


class _FinalizePutFailsApi(_ArsenalStoreApi):
    """Fails the finalize PUT (the one carrying the parsers) with a residual 400.
    The provenance marker is stamped by a separate tag-only PUT UP FRONT (which
    carries no parsers), so it still lands - only the parser finalize fails, and
    the copy stays dedup-discoverable for reuse."""

    async def __call__(
        self, base_url, headers, method, path, action, *,
        json=None, params=None, tenant_id="",
    ):
        if method == "put" and "action_output_parsers" in (json or {}):
            self.calls.append({"method": method, "path": path, "json": json})
            return None, "HTTP 400: residual finalize error"
        return await super().__call__(
            base_url, headers, method, path, action,
            json=json, params=params, tenant_id=tenant_id,
        )


async def test_fork_keeps_marked_copy_and_points_to_reuse_when_finalize_fails(
    monkeypatch,
):
    # The provenance marker is stamped on the copy UP FRONT (right after the
    # duplicate), so a finalize failure no longer orphans anything: the copy is
    # dedup-discoverable and a retry REUSES and repairs it. There is NO
    # delete/restart dance - the failure just returns a loud, actionable reason
    # that names the copy id and points at reuse.
    from app.integrations.providers.openaev import threat_arsenal

    _patch_capabilities(monkeypatch, {"default_tenant_id": "t1"})
    store = {"src1": _arsenal_source()}
    fake = _FinalizePutFailsApi(store)
    monkeypatch.setattr(threat_arsenal, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "manage_openaev_threat_arsenal",
        {
            "action": "fork",
            "action_id": "src1",
            "config": json.dumps({"action_output_parsers": _FORK_PARSERS}),
        },
    )
    assert not result.is_error, result.content
    # The OpenAEV error is preserved (loud) and names the copy for reuse/repair.
    assert "residual finalize error" in result.content
    assert "fork copy action_id fork1" in result.content
    assert "provenance marker" in result.content
    assert "REUSE and repair" in result.content
    assert fake.count("post", "/duplicate") == 1
    # No delete/restart dance: the marked copy is kept for reuse, never deleted.
    assert not [c for c in fake.calls if c["method"] == "delete"]
    assert "removed so a retry starts clean" not in result.content


class _NeverProducesFindingsApi(_ArsenalStoreApi):
    """The finalize PUT succeeds (the marker lands) but the copy never produces
    findings, so read-back verification keeps failing - a persistent parser drop.

    The verify counts flagged findings on ``action_output_parsers`` (never the
    server-derived ``action_providing``), so a persistent parser drop is modelled
    by clearing the parsers on every PUT - the copy reads back finding-less no
    matter how many times finalize re-applies the REGEX outputs.
    """

    async def __call__(
        self, base_url, headers, method, path, action, *,
        json=None, params=None, tenant_id="",
    ):
        resp, err = await super().__call__(
            base_url, headers, method, path, action,
            json=json, params=params, tenant_id=tenant_id,
        )
        if method == "put" and err is None:
            aid = path.rsplit("/", 1)[-1]
            self.store[aid]["action_output_parsers"] = []
            self.store[aid]["action_providing"] = []
        return resp, err


async def test_fork_verify_failure_keeps_marked_copy_for_reuse(monkeypatch):
    # A verification failure AFTER a successful finalize PUT is DIFFERENT from a
    # pre-marker failure: the copy carries the provenance marker, so a retry
    # REUSES and repairs it. The copy must NOT be deleted, and the error must
    # point at the reuse path.
    from app.integrations.providers.openaev import threat_arsenal

    _patch_capabilities(monkeypatch, {"default_tenant_id": "t1"})
    store = {"src1": _arsenal_source()}
    fake = _NeverProducesFindingsApi(store)
    monkeypatch.setattr(threat_arsenal, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "manage_openaev_threat_arsenal",
        {
            "action": "fork",
            "action_id": "src1",
            "config": json.dumps({"action_output_parsers": _FORK_PARSERS}),
        },
    )
    assert not result.is_error, result.content
    assert "verification failed" in result.content.lower()
    assert "provenance marker" in result.content
    # The marked copy is preserved for reuse - never deleted.
    assert not [c for c in fake.calls if c["method"] == "delete"]
    assert "removed so a retry starts clean" not in result.content


class _FinalizePutCommitsThenFailsApi(_ArsenalStoreApi):
    """Commits the finalize PUT (the marker + parsers land) but STILL returns an
    error, modelling a timeout / dropped connection AFTER OpenAEV committed - the
    ambiguous case where the copy is valid despite the reported failure."""

    async def __call__(
        self, base_url, headers, method, path, action, *,
        json=None, params=None, tenant_id="",
    ):
        if method == "put" and "action_output_parsers" in (json or {}):
            # Let the store reflect the write (marker lands), then report failure.
            await super().__call__(
                base_url, headers, method, path, action,
                json=json, params=params, tenant_id=tenant_id,
            )
            return None, "HTTP 504: gateway timeout after commit"
        return await super().__call__(
            base_url, headers, method, path, action,
            json=json, params=params, tenant_id=tenant_id,
        )


async def test_fork_ambiguous_put_failure_preserves_committed_copy(monkeypatch):
    # A finalize PUT error does NOT prove the copy is bad: a timeout can occur
    # AFTER OpenAEV commits. The copy carries its provenance marker (stamped up
    # front), so it is KEPT for reuse and never deleted - deleting would destroy a
    # valid fork a concurrent caller may already have reused. The reason points
    # honestly at reuse/repair.
    from app.integrations.providers.openaev import threat_arsenal

    _patch_capabilities(monkeypatch, {"default_tenant_id": "t1"})
    store = {"src1": _arsenal_source()}
    fake = _FinalizePutCommitsThenFailsApi(store)
    monkeypatch.setattr(threat_arsenal, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "manage_openaev_threat_arsenal",
        {
            "action": "fork",
            "action_id": "src1",
            "config": json.dumps({"action_output_parsers": _FORK_PARSERS}),
        },
    )
    assert not result.is_error, result.content
    # The OpenAEV error stays loud, and the copy is preserved for reuse.
    assert "gateway timeout after commit" in result.content
    assert "provenance marker" in result.content
    assert "REUSE and repair" in result.content
    # The committed copy must NOT be deleted, despite the reported PUT failure.
    assert not [c for c in fake.calls if c["method"] == "delete"]
    assert "removed so a retry starts clean" not in result.content


class _MarkerUnresolvableFinalizeFailsApi(_ArsenalStoreApi):
    """The fork-provenance tag upsert fails (marker_tag_id stays None) AND the
    finalize PUT fails - the one case where a retry genuinely cannot rediscover
    the copy, so the reason must NOT promise dedup. The 'ai generated' tag upsert
    still succeeds; only the source-keyed fork marker cannot be resolved."""

    async def __call__(
        self, base_url, headers, method, path, action, *,
        json=None, params=None, tenant_id="",
    ):
        if method == "post" and path == "/api/tags/upsert":
            name = (json or {}).get("tag_name", "")
            if name.startswith("ai-fork-src:"):
                self.calls.append({"method": method, "path": path, "json": json})
                return None, "HTTP 500: tag service unavailable"
        if method == "put" and "action_output_parsers" in (json or {}):
            self.calls.append({"method": method, "path": path, "json": json})
            return None, "HTTP 400: residual finalize error"
        return await super().__call__(
            base_url, headers, method, path, action,
            json=json, params=params, tenant_id=tenant_id,
        )


async def test_fork_finalize_failure_without_marker_does_not_promise_reuse(monkeypatch):
    # When the provenance marker could not be resolved, dedup is unavailable: the
    # reason must be HONEST (a retry may create a second copy; remove the stray
    # copy) instead of falsely promising the copy carries the marker and reuse is
    # guaranteed - overstating it makes the orchestrator retry expecting dedup and
    # still leak a duplicate.
    from app.integrations.providers.openaev import threat_arsenal

    _patch_capabilities(monkeypatch, {"default_tenant_id": "t1"})
    store = {"src1": _arsenal_source()}
    fake = _MarkerUnresolvableFinalizeFailsApi(store)
    monkeypatch.setattr(threat_arsenal, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "manage_openaev_threat_arsenal",
        {
            "action": "fork",
            "action_id": "src1",
            "config": json.dumps({"action_output_parsers": _FORK_PARSERS}),
        },
    )
    assert not result.is_error, result.content
    assert "residual finalize error" in result.content
    assert "could not be resolved" in result.content
    assert "may create a second one" in result.content
    # It must NOT falsely claim the copy carries the marker / guarantees reuse.
    assert "It carries the fork provenance marker" not in result.content
    assert not [c for c in fake.calls if c["method"] == "delete"]


# ── WS4-C: findings-required create gate (autonomous authors only) ───────────


async def test_arsenal_create_command_without_findings_refused_for_autonomous_author(
    monkeypatch,
):
    # WS4-C: when the caller is an autonomous arsenal author (orchestrator /
    # Payload Creator, signalled by the arsenal_findings_required ContextVar), a
    # command/executable create that declares NO REGEX output parser is refused
    # BEFORE any REST call - it would RUN but produce nothing and cannot feed a
    # chained step (OpenAEV chaining is findings-based).
    from app.integrations.providers.openaev import threat_arsenal
    from app.services.builtin_tools.filtering import (
        reset_arsenal_findings_required,
        set_arsenal_findings_required,
    )

    executor, _ = _build()
    fake = _RecordingApi({"injector_contract_id": "a1"})
    monkeypatch.setattr(threat_arsenal, "_api", fake)
    token = set_arsenal_findings_required(True)
    try:
        result = await _run(
            executor, "manage_openaev_threat_arsenal", dict(_ARSENAL_CREATE_BASE)
        )
    finally:
        reset_arsenal_findings_required(token)
    assert not result.is_error, result.content
    assert "chain-useless" in result.content
    assert "action_output_parsers" in result.content
    assert not fake.calls  # refused before any request left


async def test_arsenal_create_command_with_findings_allowed_for_autonomous_author(
    monkeypatch,
):
    # The same autonomous context ALLOWS the create once a finding-typed REGEX
    # parser is supplied via config - the copy can now feed a chained step.
    from app.integrations.providers.openaev import threat_arsenal
    from app.services.builtin_tools.filtering import (
        reset_arsenal_findings_required,
        set_arsenal_findings_required,
    )

    executor, _ = _build()
    fake = _RecordingApi({"injector_contract_id": "a1", "action_name": "Recon sweep"})
    monkeypatch.setattr(threat_arsenal, "_api", fake)
    token = set_arsenal_findings_required(True)
    try:
        result = await _run(
            executor,
            "manage_openaev_threat_arsenal",
            {
                **_ARSENAL_CREATE_BASE,
                "config": json.dumps({"action_output_parsers": _FORK_PARSERS}),
            },
        )
    finally:
        reset_arsenal_findings_required(token)
    assert not result.is_error, result.content
    assert fake.calls  # the create reached the API
    assert fake.calls[-1]["method"] == "post"


async def test_arsenal_create_command_without_findings_allowed_outside_autonomous_ctx(
    monkeypatch,
):
    # Scoping guard: with the ContextVar UNSET (a human / general-chat create),
    # the findings gate never fires - a command create with no parser is a valid
    # request and reaches the API unchanged.
    from app.integrations.providers.openaev import threat_arsenal

    executor, _ = _build()
    fake = _RecordingApi({"injector_contract_id": "a1", "action_name": "Recon sweep"})
    monkeypatch.setattr(threat_arsenal, "_api", fake)
    result = await _run(
        executor, "manage_openaev_threat_arsenal", dict(_ARSENAL_CREATE_BASE)
    )
    assert not result.is_error, result.content
    assert fake.calls and fake.calls[-1]["method"] == "post"


# ── WS4-B: arsenal search error absorption (base_id / transient full-text) ───


class _SearchErrApi:
    """`_api` fake that returns a canned error on the FIRST search, then a canned
    response on any follow-up - models the two WS4-B failure shapes."""

    def __init__(self, first_error: str, retry_data=None):
        self._first_error = first_error
        self._retry = _FakeResp(retry_data if retry_data is not None else {"content": []})
        self.calls: list[dict] = []

    async def __call__(
        self, base_url, headers, method, path, action, *,
        json=None, params=None, tenant_id="",
    ):
        self.calls.append({"method": method, "path": path, "json": json})
        if len(self.calls) == 1:
            return None, self._first_error
        return self._retry, None


async def test_arsenal_search_rejects_unindexed_filter_key_with_actionable_message(
    monkeypatch,
):
    # WS4-B: the model guessing an unindexed key (e.g. base_id) makes OpenAEV
    # answer with a Spring "not handled by Queryable annotation" error. Retrying
    # with another key only guesses again, so the tool surfaces ONE actionable
    # message naming the safe knobs and does NOT retry.
    from app.integrations.providers.openaev import threat_arsenal

    executor, _ = _build()
    fake = _SearchErrApi(
        "OpenAEV error: ... base_id not handled by Queryable annotation ..."
    )
    monkeypatch.setattr(threat_arsenal, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_threat_arsenal",
        {
            "action": "list",
            "filter_group": json.dumps(
                {
                    "mode": "and",
                    "filters": [
                        {"key": "base_id", "operator": "eq", "values": ["x"]}
                    ],
                }
            ),
        },
    )
    assert result.is_error, result.content
    assert "not an indexed/Queryable attribute" in result.content
    assert "injector_contract_tags" in result.content
    assert len(fake.calls) == 1  # no blind retry with another key


async def test_arsenal_search_absorbs_transient_full_text_500_and_notes_degraded(
    monkeypatch,
):
    # WS4-B: a transient 5xx on the server-side full-text search is absorbed -
    # the tool retries ONCE without textSearch (filter/tag scope still applies)
    # and tells the caller the free-text term was dropped, rather than failing.
    from app.integrations.providers.openaev import threat_arsenal

    executor, _ = _build()
    fake = _SearchErrApi(
        "OpenAEV server error (500) on search",
        retry_data={"content": []},
    )
    monkeypatch.setattr(threat_arsenal, "_api", fake)
    result = await _run(
        executor,
        "manage_openaev_threat_arsenal",
        {"action": "list", "search": "wifi harvest"},
    )
    assert not result.is_error, result.content
    # Exactly one retry, and the first carried textSearch, the second did not.
    assert len(fake.calls) == 2
    assert "textSearch" in (fake.calls[0]["json"] or {})
    assert "textSearch" not in (fake.calls[1]["json"] or {})
    assert "full-text search was temporarily unavailable" in result.content


# ── Phishing components (landing pages + email templates) ────────────────────


async def test_phishing_rejects_unknown_resource(monkeypatch):
    from app.integrations.providers.openaev import phishing

    fake = _RecordingApi({})
    monkeypatch.setattr(phishing, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "manage_openaev_phishing",
        {"action": "list", "resource": "campaign"},
    )
    assert not result.is_error, result.content
    assert "resource must be one of" in result.content
    assert not fake.calls  # rejected before any request left


async def test_phishing_create_landing_page_requires_name(monkeypatch):
    from app.integrations.providers.openaev import phishing

    fake = _RecordingApi({})
    monkeypatch.setattr(phishing, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "manage_openaev_phishing",
        {"action": "create", "resource": "landing_page", "html": "<h1>hi</h1>"},
    )
    assert not result.is_error, result.content
    assert "name" in result.content and "required" in result.content
    assert not fake.calls  # never left before the required field is supplied


async def test_phishing_create_email_template_requires_subject(monkeypatch):
    from app.integrations.providers.openaev import phishing

    fake = _RecordingApi({})
    monkeypatch.setattr(phishing, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "manage_openaev_phishing",
        {"action": "create", "resource": "email_template", "name": "Lure"},
    )
    assert not result.is_error, result.content
    assert "subject" in result.content and "required" in result.content
    assert not fake.calls


async def test_phishing_create_sends_only_provided_fields(monkeypatch):
    # Booleans omitted by the caller must NOT be sent, so OpenAEV's own defaults
    # (capture_* = true, tracking pixel = true) stand; an explicit False is sent.
    from app.integrations.providers.openaev import phishing

    fake = _RecordingApi({"phishing_landing_page_id": "lp1", "phishing_landing_page_name": "Login"})
    monkeypatch.setattr(phishing, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "manage_openaev_phishing",
        {
            "action": "create",
            "resource": "landing_page",
            "name": "Login",
            "html": "<h1>hi</h1>",
            "capture_passwords": False,
        },
    )
    assert not result.is_error, result.content
    body = fake.last_json()
    assert body.get("phishing_landing_page_name") == "Login"
    assert body.get("phishing_landing_page_html") == "<h1>hi</h1>"
    assert body.get("phishing_landing_page_capture_passwords") is False
    # capture_submitted_data was not provided -> not sent (backend default holds).
    assert "phishing_landing_page_capture_submitted_data" not in body
    assert fake.calls[-1]["method"] == "post"


async def test_phishing_update_is_full_replace_merge(monkeypatch):
    # The PUT is full-replace: the tool must read the current entity and merge
    # the caller's change over it, so unspecified fields (html/css/name) survive.
    from app.integrations.providers.openaev import phishing

    current = {
        "phishing_landing_page_id": "lp1",
        "phishing_landing_page_name": "Original",
        "phishing_landing_page_html": "<h1>keep me</h1>",
        "phishing_landing_page_css": "body{}",
        "phishing_landing_page_capture_submitted_data": True,
    }
    fake = _RecordingApi(current)
    monkeypatch.setattr(phishing, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "manage_openaev_phishing",
        {
            "action": "update",
            "resource": "landing_page",
            "resource_id": "lp1",
            "description": "Now with a description",
        },
    )
    assert not result.is_error, result.content
    # Two calls: GET (fetch current) then PUT (full-replace merge).
    assert [c["method"] for c in fake.calls] == ["get", "put"]
    body = fake.last_json()
    assert body.get("phishing_landing_page_description") == "Now with a description"
    assert body.get("phishing_landing_page_name") == "Original"
    assert body.get("phishing_landing_page_html") == "<h1>keep me</h1>"
    assert body.get("phishing_landing_page_css") == "body{}"


async def test_phishing_delete_requires_resource_id(monkeypatch):
    from app.integrations.providers.openaev import phishing

    fake = _RecordingApi({})
    monkeypatch.setattr(phishing, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "manage_openaev_phishing",
        {"action": "delete", "resource": "email_template"},
    )
    assert not result.is_error, result.content
    assert "resource_id is required" in result.content
    assert not fake.calls


# ── Regression tests: analytics widget_config discriminator (missing/wrong) ──


async def test_analytics_missing_discriminator_on_series_errors(monkeypatch):
    # 'series' accepts two shapes (temporal/structural), so the discriminator
    # cannot be auto-filled: the tool must refuse locally with the exact fix
    # rather than round-tripping OpenAEV's opaque "missing discriminator" 400.
    from app.integrations.providers.openaev import dashboards

    fake = _RecordingApi({})
    monkeypatch.setattr(dashboards, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "query_openaev_analytics",
        {"kind": "series", "widget_config": '{"series": []}'},
    )
    assert not result.is_error, result.content
    assert "widget_configuration_type" in result.content
    assert "temporal-histogram" in result.content
    assert "structural-histogram" in result.content
    assert not fake.calls  # never left before a valid discriminator is set


async def test_analytics_autofills_discriminator_for_count(monkeypatch):
    # 'count' maps to exactly one shape ('flat'), so a missing discriminator is
    # auto-filled and the request proceeds - the common case must just work.
    from app.integrations.providers.openaev import dashboards

    fake = _RecordingApi({"count": 3})
    monkeypatch.setattr(dashboards, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "query_openaev_analytics",
        {"kind": "count", "widget_config": '{"series": []}'},
    )
    assert not result.is_error, result.content
    assert fake.calls, "count query should reach the API once auto-filled"
    body = fake.last_json()
    assert body["widget_config"]["widget_configuration_type"] == "flat"


async def test_analytics_rejects_wrong_discriminator_for_kind(monkeypatch):
    # A discriminator that names a shape the kind does not accept (here 'list'
    # for 'count') is rejected locally with the accepted set.
    from app.integrations.providers.openaev import dashboards

    fake = _RecordingApi({})
    monkeypatch.setattr(dashboards, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "query_openaev_analytics",
        {
            "kind": "count",
            "widget_config": '{"widget_configuration_type": "list", "series": []}',
        },
    )
    assert not result.is_error, result.content
    assert "not valid for kind='count'" in result.content
    assert "flat" in result.content
    assert not fake.calls


async def test_analytics_blank_discriminator_treated_as_missing(monkeypatch):
    # A present-but-blank discriminator cannot resolve a Jackson subtype either,
    # so a single-shape kind ('count') auto-fills it exactly as if it were absent
    # rather than sending a guaranteed "missing discriminator" 400 upstream.
    from app.integrations.providers.openaev import dashboards

    fake = _RecordingApi({"count": 1})
    monkeypatch.setattr(dashboards, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "query_openaev_analytics",
        {
            "kind": "count",
            "widget_config": '{"widget_configuration_type": "   ", "series": []}',
        },
    )
    assert not result.is_error, result.content
    assert fake.calls, "blank discriminator should be auto-filled, then proceed"
    body = fake.last_json()
    assert body["widget_config"]["widget_configuration_type"] == "flat"


async def test_analytics_non_string_discriminator_rejected(monkeypatch):
    # A non-string discriminator (here a number) can never name a subtype id, so
    # it is rejected locally with the accepted set rather than round-tripping the
    # same opaque deserialization 400.
    from app.integrations.providers.openaev import dashboards

    fake = _RecordingApi({})
    monkeypatch.setattr(dashboards, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "query_openaev_analytics",
        {
            "kind": "count",
            "widget_config": '{"widget_configuration_type": 5, "series": []}',
        },
    )
    assert not result.is_error, result.content
    assert "must be a string" in result.content
    assert "flat" in result.content
    assert not fake.calls


# ── Regression tests: analytics base_entity and time_range (#272) ───────────


def _entity_config(entity, **extra):
    config = {
        "widget_configuration_type": "flat",
        "series": [
            {
                "name": "s",
                "filter": {
                    "mode": "and",
                    "filters": [
                        {"key": "base_entity", "operator": "eq", "values": [entity], "mode": "or"},
                    ],
                },
            }
        ],
    }
    config.update(extra)
    return json.dumps(config)


async def test_analytics_refuses_retired_endpoint_base_entity(monkeypatch):
    # 'endpoint' was retired for 'asset': OpenAEV answers a silent 0, so the
    # tool refuses before any query and names the replacement.
    from app.integrations.providers.openaev import dashboards

    fake = _RecordingApi({})
    monkeypatch.setattr(dashboards, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "query_openaev_analytics",
        {"kind": "count", "widget_config": _entity_config("endpoint")},
    )
    assert "nothing queried" in result.content
    assert "'asset'" in result.content and "asset_category" in result.content
    assert not fake.calls


async def test_analytics_refuses_unknown_base_entity(monkeypatch):
    from app.integrations.providers.openaev import dashboards

    fake = _RecordingApi({})
    monkeypatch.setattr(dashboards, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "query_openaev_analytics",
        {"kind": "count", "widget_config": _entity_config("hosts")},
    )
    assert "not an OpenAEV engine entity" in result.content
    assert "vulnerable-endpoint" in result.content
    assert not fake.calls


async def test_analytics_defaults_absent_time_range_to_all_time(monkeypatch):
    # An absent time_range is DEFAULT in OpenAEV, which is LAST_QUARTER on an
    # ad-hoc query: the tool sends ALL_TIME and says so.
    from app.integrations.providers.openaev import dashboards

    fake = _RecordingApi({"interval_count": 440})
    monkeypatch.setattr(dashboards, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "query_openaev_analytics",
        {"kind": "count", "widget_config": _entity_config("asset")},
    )
    assert fake.calls
    assert fake.last_json()["widget_config"]["time_range"] == "ALL_TIME"
    assert "ALL_TIME applied" in result.content


async def test_analytics_keeps_explicit_time_range_and_flags_default(monkeypatch):
    from app.integrations.providers.openaev import dashboards

    fake = _RecordingApi({"interval_count": 1})
    monkeypatch.setattr(dashboards, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "query_openaev_analytics",
        {"kind": "count", "widget_config": _entity_config("inject", time_range="last_week")},
    )
    assert fake.last_json()["widget_config"]["time_range"] == "LAST_WEEK"
    assert "time_range LAST_WEEK" in result.content
    result = await _run(
        executor,
        "query_openaev_analytics",
        {"kind": "count", "widget_config": _entity_config("inject", time_range="DEFAULT")},
    )
    assert "LAST_QUARTER" in result.content


async def test_analytics_refuses_invalid_time_range(monkeypatch):
    from app.integrations.providers.openaev import dashboards

    fake = _RecordingApi({})
    monkeypatch.setattr(dashboards, "_api", fake)
    executor, _ = _build()
    result = await _run(
        executor,
        "query_openaev_analytics",
        {"kind": "count", "widget_config": _entity_config("inject", time_range="LAST_30_DAYS")},
    )
    assert "not an OpenAEV time range" in result.content
    assert not fake.calls


# ── Regression tests: chaining placeholder / fabricated-id guard ─────────────


def test_placeholder_id_guard_rejects_fabricated_ids():
    # A fabricated or template id must fail locally with the way to resolve a
    # real one, instead of surfacing as OpenAEV's overloaded 404.
    from app.integrations.providers.openaev.preview import _placeholder_id_error

    hint = "read it from the run state"
    for bad in ("", "placeholder", "PLACEHOLDER", "{{workflow_id}}", "<your id>",
                "your-workflow-id", "example", "todo", "n/a"):
        err = _placeholder_id_error("workflow_id", bad, hint)
        assert err, f"expected {bad!r} to be rejected"
        assert hint in err

    # A real-looking id passes the guard (returns None).
    assert _placeholder_id_error(
        "workflow_id", "7f3a9c12-1b2c-4d5e-9f00-abcdef012345", hint
    ) is None
