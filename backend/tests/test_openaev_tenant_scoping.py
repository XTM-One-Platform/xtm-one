"""OpenAEV tenant scoping (spec 026).

Every tool request is routed to the default tenant's ``/api/tenants/{id}/...``
twin when ``GET /api/settings`` reports one. The tests patch
``_common.safe_request``, the single transport behind both ``_api`` and
``scoped_safe_request``, and record the URLs the tools really request.
"""

import asyncio
import json
import re
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.integrations.providers.openaev import _common, build_tools
from app.integrations.providers.openaev._common import (
    PlatformCapabilities,
    TenantScope,
    _scope_path,
    _select_tenant,
)
from app.services.integration_tools import BindingBasis, ToolBindings
from app.tools.base import CallableToolExecutor, ToolCall

BASE = "https://demo.openaev.io"
TOKEN = f"{BASE}||test-token"


class _Resp:
    """Permissive response: tools only need ``json()`` / ``content`` here."""

    status_code = 200
    content = b"{}"
    text = "{}"
    headers: dict = {}

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _BadBodyResp(_Resp):
    def json(self):
        raise ValueError("<html>proxy error page</html>")


class _Transport:
    """Stands in for ``safe_request``; answers the settings probe, records the rest."""

    def __init__(
        self,
        settings=None,
        *,
        settings_error=None,
        bad_body=False,
        fail=None,
        base=BASE,
        memberships=None,
    ):
        self.base = base
        # ``GET /api/me/tenants`` body; None answers 404, as an OpenAEV without the route.
        self.memberships = memberships
        self.membership_calls = 0
        self.settings_kwargs: dict = {}
        self.settings = settings if settings is not None else {}
        self.settings_error = settings_error
        self.bad_body = bad_body
        self.fail = fail  # error string returned for every non-settings request
        self.settings_calls = 0
        self.requests: list[tuple[str, str]] = []

    @property
    def urls(self) -> list[str]:
        return [u for _, u in self.requests]

    async def __call__(self, client, method, url, provider, action, **kwargs):
        if url == f"{self.base}/api/settings":
            self.settings_calls += 1
            self.settings_kwargs = kwargs
            if self.settings_error:
                return None, self.settings_error
            if self.bad_body:
                return _BadBodyResp(None), None
            return _Resp(self.settings), None
        if url == f"{self.base}/api/me/tenants":
            self.membership_calls += 1
            if self.memberships is None:
                return None, "OpenAEV error (HTTP 404): Not Found"
            return _Resp([{"tenant_id": t} for t in self.memberships]), None
        self.requests.append((method, url))
        if self.fail:
            return None, self.fail
        return _Resp(
            {"content": [], "totalElements": 0, "scenario_id": "s1", "exercise_id": "e1"}
        ), None


def _install(
    monkeypatch, *, tenant_id: str = "", **kwargs
) -> tuple[_Transport, CallableToolExecutor]:
    transport = _Transport(**kwargs)
    monkeypatch.setattr(_common, "safe_request", transport)
    executor = CallableToolExecutor()
    build_tools(f"{transport.base}||test-token", executor, tenant_id=tenant_id)
    return transport, executor


async def _run(executor, name, arguments):
    return await executor.execute_tool(
        ToolCall(id=f"call-{name}", name=name, arguments=arguments)
    )


# ── _scope_path (contracts/path-scoping.md) ─────────────────────────────


class _Headers(dict):
    def __init__(self, scope: TenantScope | None):
        super().__init__()
        if scope is not None:
            self.tenant_scope = _FakeResolver(scope)


class _FakeResolver:
    def __init__(self, scope: TenantScope):
        self._scope = scope
        self.calls = 0

    async def tenant_scope(self) -> TenantScope:
        self.calls += 1
        return self._scope


RESOLVED = TenantScope("t1", None)
NONE = TenantScope("", None)
FAILED = TenantScope("", "HTTP 403")


@pytest.mark.parametrize(
    ("scope", "path", "expected"),
    [
        (RESOLVED, "/api/scenarios", "/api/tenants/t1/scenarios"),
        (RESOLVED, "/api/scenarios/s1/injects", "/api/tenants/t1/scenarios/s1/injects"),
        (RESOLVED, "/api/organizations/search", "/api/tenants/t1/organizations/search"),
        # already tenant-scoped (FR-008)
        (RESOLVED, "/api/tenants/t1/scenarios", "/api/tenants/t1/scenarios"),
        (RESOLVED, "/api/tenants/t9/scenarios", "/api/tenants/t9/scenarios"),
        # platform-level families have no twin (FR-010)
        (RESOLVED, "/api/settings", "/api/settings"),
        (RESOLVED, "/api/settings/version", "/api/settings/version"),
        (RESOLVED, "/api/platform-users", "/api/platform-users"),
        (RESOLVED, "/api/platform-users/u1", "/api/platform-users/u1"),
        (RESOLVED, "/api/platform-roles/search", "/api/platform-roles/search"),
        (RESOLVED, "/api/platform-groups", "/api/platform-groups"),
        (RESOLVED, "/api/schemas/x?y=1", "/api/schemas/x?y=1"),
        # identity endpoints: only GET /api/me has a twin (FR-010)
        (RESOLVED, "/api/me", "/api/me"),
        (RESOLVED, "/api/me/profile", "/api/me/profile"),
        (RESOLVED, "/api/me/tokens", "/api/me/tokens"),
        (RESOLVED, "/api/media", "/api/tenants/t1/media"),
        # run callbacks: OpenAEV derives the tenant from the run (FR-010)
        (RESOLVED, "/api/autonomous-runs/r1/events", "/api/autonomous-runs/r1/events"),
        (RESOLVED, "/api/autonomous-runs/capabilities/resolve", "/api/autonomous-runs/capabilities/resolve"),
        # a family that merely starts with an exempt prefix is not exempt
        (RESOLVED, "/api/settingsx", "/api/tenants/t1/settingsx"),
        # not an /api path
        (RESOLVED, "/health", "/health"),
        # nothing to scope with
        (NONE, "/api/scenarios", "/api/scenarios"),
        (FAILED, "/api/scenarios", "/api/scenarios"),
    ],
)
async def test_scope_path_table(scope, path, expected):
    headers = _Headers(scope)
    scoped, _hint = await _scope_path(headers, path)
    assert scoped == expected


async def test_scope_path_without_resolver_is_unchanged():
    scoped, hint = await _scope_path(_Headers(None), "/api/scenarios")
    assert (scoped, hint) == ("/api/scenarios", "")


async def test_scope_path_never_consults_the_resolver_for_exempt_paths():
    # /api/settings is how the resolver loads: consulting it would recurse.
    headers = _Headers(RESOLVED)
    await _scope_path(headers, "/api/settings")
    await _scope_path(headers, "/api/tenants/t1/x")
    assert headers.tenant_scope.calls == 0


async def test_scope_path_reports_a_failed_lookup_hint():
    scoped, hint = await _scope_path(_Headers(FAILED), "/api/scenarios")
    assert scoped == "/api/scenarios"
    assert "HTTP 403" in hint
    assert "could not be resolved" in hint


# ── PlatformCapabilities tri-state (data-model.md) ──────────────────────


def _caps(
    monkeypatch, *, tenant_id: str = "", **kwargs
) -> tuple[PlatformCapabilities, _Transport]:
    transport = _Transport(**kwargs)
    monkeypatch.setattr(_common, "safe_request", transport)
    caps = PlatformCapabilities(BASE, _common._headers("tok"), configured_tenant_id=tenant_id)
    return caps, transport


async def test_tenant_scope_resolved(monkeypatch):
    caps, _ = _caps(monkeypatch, settings={"default_tenant_id": "t1"})
    assert await caps.tenant_scope() == TenantScope("t1", None, memberships_unknown=True)


async def test_tenant_scope_none_when_the_platform_exposes_no_tenant(monkeypatch):
    caps, _ = _caps(monkeypatch, settings={"platform_version": "2.3"})
    assert await caps.tenant_scope() == TenantScope("", None)


async def test_tenant_scope_failed_is_not_none_and_is_not_retried(monkeypatch):
    caps, transport = _caps(monkeypatch, settings_error="HTTP 403: denied")
    first = await caps.tenant_scope()
    second = await caps.tenant_scope()
    assert first.tenant_id == "" and "403" in (first.error or "")
    assert second == first
    assert transport.settings_calls == 1
    # existing callers keep the empty-string contract
    assert await caps.tenant_id() == ""


async def test_tenant_scope_non_json_body_is_a_failure_not_an_exception(monkeypatch):
    caps, transport = _caps(monkeypatch, bad_body=True)
    scope = await caps.tenant_scope()
    assert scope.tenant_id == ""
    assert scope.error
    assert await caps.tenant_scope() == scope
    assert transport.settings_calls == 1


async def test_concurrent_first_callers_share_one_settings_fetch(monkeypatch):
    # Autonomous callbacks gather() tool calls: a second caller must wait for
    # the fetch, not read the empty pre-load state and go out unscoped.
    caps, transport = _caps(monkeypatch, settings={"default_tenant_id": "t1"})
    original = transport.__call__

    async def slow(client, method, url, provider, action, **kwargs):
        await asyncio.sleep(0.05)
        return await original(client, method, url, provider, action, **kwargs)

    monkeypatch.setattr(_common, "safe_request", slow)
    results = await asyncio.gather(*(caps.tenant_scope() for _ in range(5)))
    assert all(r == TenantScope("t1", None, memberships_unknown=True) for r in results)
    assert transport.settings_calls == 1


# ── US1: scenario / inject / launch on a multi-tenant OpenAEV ────────────

MT = {"default_tenant_id": "t1"}


async def test_scenario_inject_and_launch_target_the_default_tenant(monkeypatch):
    transport, executor = _install(monkeypatch, settings=MT)

    await _run(executor, "create_openaev_scenario", {"name": "S"})
    await _run(
        executor,
        "add_openaev_inject_to_scenario",
        {"scenario_id": "s1", "title": "I", "injector_contract_id": "c1"},
    )
    await _run(
        executor, "manage_openaev_scenario", {"action": "launch", "scenario_id": "s1"}
    )
    await _run(
        executor, "manage_openaev_simulation", {"action": "list"}
    )

    assert transport.urls == [
        f"{BASE}/api/tenants/t1/scenarios",
        # The inject-add reads the contract to default a technical inject's
        # expectations; the read is tenant-scoped like every other call.
        f"{BASE}/api/tenants/t1/injector_contracts/c1",
        f"{BASE}/api/tenants/t1/scenarios/s1/injects",
        f"{BASE}/api/tenants/t1/scenarios/s1/exercise/running",
        f"{BASE}/api/tenants/t1/exercises/search",
    ]
    assert transport.settings_calls == 1


# ── US2: every module, reads and writes ──────────────────────────────────

# (module, tool, arguments, method, path under /api/tenants/t1)
_READS = [
    ("access", "manage_openaev_organization", {"action": "list"}, "post", "/organizations/search"),
    ("assets", "manage_openaev_asset_group", {"action": "list"}, "post", "/asset_groups/search"),
    ("content", "manage_openaev_document", {"action": "list"}, "post", "/documents/search"),
    ("dashboards", "manage_openaev_dashboard", {"action": "list"}, "post", "/custom-dashboards/search"),
    ("expectations", "manage_openaev_expectation", {"action": "simulation_results", "exercise_id": "e1"}, "get", "/exercises/e1/results"),
    ("findings", "manage_openaev_vulnerability", {"action": "list"}, "post", "/vulnerabilities/search"),
    ("injects", "manage_openaev_inject", {"action": "list", "scope": "scenario", "parent_id": "s1"}, "post", "/scenarios/s1/injects/simple"),
    ("lessons_reports", "manage_openaev_reporting", {"action": "list"}, "post", "/reportings/search"),
    ("notifications", "manage_openaev_notification", {"action": "list_triggers"}, "post", "/notification-triggers/search"),
    ("phishing", "manage_openaev_phishing", {"action": "list"}, "post", "/phishing/landing-pages/search"),
    ("platform", "manage_openaev_connector_instance", {"action": "get", "connector_instance_id": "ci1"}, "get", "/connector-instances/ci1"),
    # export_openaev_scenario streams through fetch_file (not safe_request): see
    # test_scenario_export_is_scoped_too.
    ("taxonomy", "manage_openaev_tag", {"action": "list"}, "post", "/tags/search"),
    ("teams", "manage_openaev_team", {"action": "list"}, "post", "/teams/search"),
    ("testing", "manage_openaev_atomic_testing", {"action": "list"}, "post", "/atomic-testings/search"),
]
_WRITES = [
    ("access", "manage_openaev_organization", {"action": "create", "name": "Org"}, "post", "/organizations"),
    ("assets", "manage_openaev_asset_group", {"action": "create", "name": "G"}, "post", "/asset_groups"),
    ("content", "manage_openaev_channel", {"action": "create", "channel_type": "email", "name": "n", "description": "d"}, "post", "/channels"),
    ("dashboards", "manage_openaev_dashboard", {"action": "create", "name": "D"}, "post", "/custom-dashboards"),
    ("expectations", "manage_openaev_expectation", {"action": "validate", "expectation_id": "x1", "score": 1}, "put", "/expectations/x1"),
    ("findings", "manage_openaev_finding", {"action": "create", "field": "f", "finding_type": "text", "value": "v", "inject_id": "i1"}, "post", "/findings"),
    ("injects", "manage_openaev_inject", {"action": "create", "scope": "scenario", "parent_id": "s1", "title": "T", "injector_contract_id": "c1"}, "post", "/scenarios/s1/injects"),
    ("lessons_reports", "manage_openaev_reporting", {"action": "create", "name": "R", "context_type": "PLATFORM"}, "post", "/reportings"),
    ("notifications", "manage_openaev_notification", {"action": "create_notifier", "name": "N", "notifier_type": "UI"}, "post", "/notifiers"),
    ("phishing", "manage_openaev_phishing", {"action": "create", "name": "P"}, "post", "/phishing/landing-pages"),
    ("platform", "manage_openaev_connector_instance", {"action": "delete", "connector_instance_id": "ci1"}, "delete", "/connector-instances/ci1"),
    ("taxonomy", "manage_openaev_tag", {"action": "create", "name": "t", "color": "#ffffff"}, "post", "/tags"),
    ("teams", "manage_openaev_team", {"action": "create", "name": "T"}, "post", "/teams"),
    ("testing", "manage_openaev_atomic_testing", {"action": "create", "title": "T", "injector_contract_id": "c1"}, "post", "/atomic-testings"),
]


@pytest.mark.parametrize(
    ("module", "tool", "arguments", "method", "suffix"),
    _READS + _WRITES,
    ids=[f"read-{r[0]}" for r in _READS] + [f"write-{w[0]}" for w in _WRITES],
)
async def test_every_module_requests_the_default_tenant(
    monkeypatch, module, tool, arguments, method, suffix
):
    transport, executor = _install(monkeypatch, settings=MT)
    await _run(executor, tool, arguments)
    assert transport.requests[:1] == [(method, f"{BASE}/api/tenants/t1{suffix}")]


async def test_document_upload_is_scoped_too(monkeypatch):
    # The multipart upload sends stripped headers through its own client.
    from app.integrations.providers.openaev import content

    seen: list[str] = []

    async def fake(client, method, url, provider, action, **kwargs):
        seen.append(url)
        return _Resp({"document_id": "d1", "document_name": "n"}), None

    _, executor = _install(monkeypatch, settings=MT)
    monkeypatch.setattr(content, "safe_request", fake)
    await _run(
        executor,
        "manage_openaev_document",
        {"action": "upload", "name": "n.txt", "content": "hello"},
    )
    assert seen == [f"{BASE}/api/tenants/t1/documents"]


async def test_scenario_import_is_scoped_too(monkeypatch):
    # The multipart scenario import sends stripped headers through its own client.
    from app.integrations.providers.openaev import simulations

    seen: list[str] = []

    async def fake(client, method, url, provider, action, **kwargs):
        seen.append(url)
        return _Resp({"missingActions": []}), None

    async def fake_pipe(file_context, file_id, *, max_bytes, target):
        return (b"PK\x03\x04-scenario-zip", "scenario.zip", "application/zip")

    _, executor = _install(monkeypatch, settings=MT)
    monkeypatch.setattr(simulations, "safe_request", fake)
    monkeypatch.setattr(simulations, "read_pipe_file", fake_pipe)
    result = await _run(executor, "import_openaev_scenario", {"file_id": "f1"})
    assert not result.is_error, result.content
    assert seen == [f"{BASE}/api/tenants/t1/scenarios/import"]


async def test_scenario_export_is_scoped_too(monkeypatch):
    # The streamed export goes through fetch_file (not safe_request), so assert the
    # scoped URL it is handed and the privacy-safe query params.
    from app.integrations.file_delivery import FetchedFile
    from app.integrations.providers.openaev import simulations

    seen: list[tuple[str, dict]] = []

    async def fake_fetch(client, method, url, *, provider, action, max_bytes, **kwargs):
        seen.append((url, kwargs.get("params", {})))
        return FetchedFile(
            data=b"PK\x03\x04-scenario-zip",
            content_type="application/zip",
            filename="whatever.zip",
        )

    _, executor = _install(monkeypatch, settings=MT)
    monkeypatch.setattr(simulations, "fetch_file", fake_fetch)
    result = await _run(executor, "export_openaev_scenario", {"scenario_id": "s1"})
    assert not result.is_error, result.content
    assert [u for u, _ in seen] == [f"{BASE}/api/tenants/t1/scenarios/s1/export"]
    # Player data and variable values are never exported.
    assert seen[0][1]["isWithPlayers"] == "false"
    assert seen[0][1]["isWithVariableValues"] == "false"


# ── export/import behaviour: missingActions honesty, quarantine, export safety ──
#
# The scoped-URL / privacy-param contract is covered above; these cover the
# branches the tool exists for.


def _import_executor(monkeypatch, response, *, piped=None, **install):
    """Build the tools with ``simulations.safe_request`` / ``read_pipe_file`` patched.

    *response* is the ``(resp, err)`` the import POST answers with; *piped*
    overrides what ``read_pipe_file`` returns (default: a valid zip tuple).
    Returns the executor and a list recording each import POST URL reached.
    """
    from app.integrations.providers.openaev import simulations

    install.setdefault("settings", MT)
    sent: list[str] = []

    async def fake(client, method, url, provider, action, **kwargs):
        sent.append(url)
        return response

    async def fake_pipe(file_context, file_id, *, max_bytes, target):
        return piped if piped is not None else (
            b"PK\x03\x04-scenario-zip", "scenario.zip", "application/zip"
        )

    _, executor = _install(monkeypatch, **install)
    monkeypatch.setattr(simulations, "safe_request", fake)
    monkeypatch.setattr(simulations, "read_pipe_file", fake_pipe)
    return executor, sent


async def test_import_reports_missing_actions_inside_an_untrusted_block(monkeypatch):
    # missingActions[].name is author-authored archive text: it must reach the model
    # only inside the quarantine block, never as open prose.
    name = "Nuclei HTTP vulnerability scanner"
    executor, sent = _import_executor(
        monkeypatch, (_Resp({"missingActions": [{"type": "Injector", "name": name}]}), None)
    )
    result = await _run(executor, "import_openaev_scenario", {"file_id": "f1"})
    assert not result.is_error, result.content
    assert "MISSING 1" in result.content
    assert "Untrusted external content follows" in result.content
    header, block = result.content.split("Untrusted external content follows", 1)
    assert name not in header  # not in the open prose
    assert name in block  # quarantined
    assert sent  # the import POST was reached


async def test_import_keeps_a_long_missing_list_within_the_cut(monkeypatch):
    missing = [{"type": "Injector", "name": f"capability-{i:04d}"} for i in range(400)]
    executor, _ = _import_executor(monkeypatch, (_Resp({"missingActions": missing}), None))
    result = await _run(executor, "import_openaev_scenario", {"file_id": "f1"})
    assert "MISSING 400" in result.content
    assert "capability-0000" in result.content  # the list is serialised, then cut
    assert "capability-0399" not in result.content  # the 1500-char cut holds


async def test_import_does_not_claim_coverage_when_the_field_is_absent(monkeypatch):
    executor, _ = _import_executor(monkeypatch, (_Resp({}), None))
    result = await _run(executor, "import_openaev_scenario", {"file_id": "f1"})
    assert not result.is_error, result.content
    assert "could not be confirmed" in result.content
    assert "has every injector capability" not in result.content


async def test_import_reports_full_coverage_on_an_empty_list(monkeypatch):
    executor, _ = _import_executor(monkeypatch, (_Resp({"missingActions": []}), None))
    result = await _run(executor, "import_openaev_scenario", {"file_id": "f1"})
    assert not result.is_error, result.content
    assert "has every injector capability" in result.content


async def test_import_unreadable_body_is_not_a_failure(monkeypatch):
    # A non-JSON 200 after a non-idempotent import must not look like a tool error,
    # or the model retries and imports twice.
    executor, _ = _import_executor(monkeypatch, (_BadBodyResp(None), None))
    result = await _run(executor, "import_openaev_scenario", {"file_id": "f1"})
    assert not result.is_error, result.content
    assert "did not return a readable result" in result.content


async def test_import_pipe_error_sends_nothing_and_is_an_error(monkeypatch):
    executor, sent = _import_executor(
        monkeypatch, (_Resp({}), None), piped="Error: the attached file is not a .zip."
    )
    result = await _run(executor, "import_openaev_scenario", {"file_id": "f1"})
    assert result.is_error
    assert sent == []  # the import POST is never reached


async def test_import_appends_the_scope_hint_on_a_tenant_rejection(monkeypatch):
    executor, _ = _import_executor(
        monkeypatch, (None, _REJECTED), settings_error="HTTP 403: denied"
    )
    result = await _run(executor, "import_openaev_scenario", {"file_id": "f1"})
    assert _REJECTED in result.content
    assert _HINT in result.content


async def test_import_timeout_warns_the_scenario_may_already_exist(monkeypatch):
    timeout = "Error: OpenAEV request timed out while trying to import scenario."
    executor, _ = _import_executor(monkeypatch, (None, timeout))
    result = await _run(executor, "import_openaev_scenario", {"file_id": "f1"})
    assert "may already have been created" in result.content


async def test_import_http_error_does_not_warn_about_a_duplicate(monkeypatch):
    http = "Error: OpenAEV returned HTTP 400 while trying to import scenario."
    executor, _ = _import_executor(monkeypatch, (None, http))
    result = await _run(executor, "import_openaev_scenario", {"file_id": "f1"})
    assert "may already have been created" not in result.content


def _export_executor(monkeypatch, fetched):
    """Build the tools with ``simulations.fetch_file`` patched to return *fetched*.

    Returns the executor and a list of ``(url, params)`` the export requested.
    """
    from app.integrations.providers.openaev import simulations

    seen: list[tuple[str, dict]] = []

    async def fake_fetch(client, method, url, *, provider, action, max_bytes, **kwargs):
        seen.append((url, kwargs.get("params", {})))
        return fetched

    _, executor = _install(monkeypatch, settings=MT)
    monkeypatch.setattr(simulations, "fetch_file", fake_fetch)
    return executor, seen


def _fetched(data=b"PK\x03\x04-zip", filename="whatever.zip"):
    from app.integrations.file_delivery import FetchedFile

    return FetchedFile(data=data, content_type="application/zip", filename=filename)


async def test_export_over_the_cap_is_returned_as_is(monkeypatch):
    from app.integrations.file_delivery import FetchError

    err = FetchError(
        "Error: the scenario export is over the 25 MB download limit.", too_large=True
    )
    executor, _ = _export_executor(monkeypatch, err)
    result = await _run(executor, "export_openaev_scenario", {"scenario_id": "s1"})
    assert "25 MB download limit" in result.content


async def test_export_empty_archive_is_reported(monkeypatch):
    executor, _ = _export_executor(monkeypatch, _fetched(data=b""))
    result = await _run(executor, "export_openaev_scenario", {"scenario_id": "s1"})
    assert "empty export" in result.content


async def test_export_ignores_the_content_disposition_name(monkeypatch):
    # The stored name is id-derived, so the author-authored Content-Disposition name
    # is never used, quarantined or not.
    executor, _ = _export_executor(monkeypatch, _fetched(filename="evil<<UNTRUSTED-x>>.zip"))
    result = await _run(executor, "export_openaev_scenario", {"scenario_id": "s1"})
    assert "openaev-scenario-s1.zip" in result.content
    assert "evil" not in result.content


async def test_export_quotes_a_path_traversal_scenario_id(monkeypatch):
    executor, seen = _export_executor(monkeypatch, _fetched())
    await _run(
        executor, "export_openaev_scenario", {"scenario_id": "a/../../exercises/b"}
    )
    url = seen[0][0]
    assert "a%2F..%2F..%2Fexercises%2Fb" in url
    assert "/exercises/" not in url  # the traversal cannot re-target another route


async def test_export_with_teams_never_leaks_players_or_variable_values(monkeypatch):
    executor, seen = _export_executor(monkeypatch, _fetched())
    await _run(
        executor,
        "export_openaev_scenario",
        {"scenario_id": "s1", "with_teams": True},
    )
    params = seen[0][1]
    assert params["isWithTeams"] == "true"
    assert params["isWithPlayers"] == "false"
    assert params["isWithVariableValues"] == "false"


async def test_export_coerces_the_string_false_to_a_real_boolean(monkeypatch):
    # bool("false") is True; a model passing the OpenAEV literal must not flip the flag on.
    executor, seen = _export_executor(monkeypatch, _fetched())
    await _run(
        executor,
        "export_openaev_scenario",
        {"scenario_id": "s1", "with_teams": "false", "with_scope_definition": "false"},
    )
    params = seen[0][1]
    assert params["isWithTeams"] == "false"
    assert params["isWithScopeDefinition"] == "false"


async def test_exempt_routes_keep_their_base_path(monkeypatch):
    # FR-010: no twin (settings, platform users/groups) or the run decides its
    # tenant (autonomous-run callbacks).
    transport, executor = _install(monkeypatch, settings=MT)
    await _run(executor, "manage_openaev_settings", {"action": "get"})
    await _run(executor, "manage_openaev_profile", {"action": "get"})
    await _run(executor, "manage_openaev_profile", {"action": "tokens"})
    await _run(executor, "manage_openaev_profile", {"action": "update", "config": "{}"})
    await _run(executor, "manage_openaev_user", {"action": "list", "scope": "platform"})
    await _run(executor, "manage_openaev_group", {"action": "list", "scope": "platform"})
    await _run(
        executor,
        "record_openaev_event",
        {"run_id": "r1", "type": "NARRATION", "title": "t", "content": "c"},
    )
    assert transport.urls == [
        f"{BASE}/api/me",
        f"{BASE}/api/me/tokens",
        f"{BASE}/api/me/profile",
        f"{BASE}/api/platform-users/search",
        f"{BASE}/api/platform-groups/search",
        f"{BASE}/api/autonomous-runs/r1/events",
    ]


_HAND_SCOPED = "tenant-scoped by hand"

# A bare/qualified safe_request call, or a verb called on a client object.
_UNSCOPED_CALL = re.compile(
    r"(?<!scoped_)\bsafe_request\("
    r"|\b\w*client\w*\.(?:get|post|put|patch|delete|request|send|stream)\("
)


def _unscoped_lines(text: str) -> list[int]:
    """1-based lines with a raw request not marked as hand-scoped on the line above."""
    lines = text.splitlines()
    return [
        i + 1
        for i, line in enumerate(lines)
        if _UNSCOPED_CALL.search(line) and not (i and _HAND_SCOPED in lines[i - 1])
    ]


def test_no_tool_module_bypasses_the_scoped_helpers():
    # SC-005 guard: a new module that opens its own client and calls
    # safe_request directly would silently skip tenant scoping.
    import pathlib

    pkg = pathlib.Path(_common.__file__).parent
    offenders = []
    for path in sorted(pkg.glob("*.py")):
        if path.name == "_common.py":
            continue  # defines the scoped wrapper
        offenders += [
            f"{path.name}:{n}" for n in _unscoped_lines(path.read_text(encoding="utf-8"))
        ]
    assert not offenders, f"unscoped requests: {offenders}"


@pytest.mark.parametrize(
    ("line", "flagged"),
    [
        ("await safe_request(client, 'get', url)", True),
        ("await http.safe_request(client, 'get', url)", True),
        ("await app.integrations.http.safe_request(c, 'get', u)", True),
        ("await client.post(url, json=body)", True),
        ("await egress_client_c2.get(url)", True),
        ("await scoped_safe_request(client, 'get', url)", False),
        ("data = resp.json().get('content')", False),
        ("x = payload.get('client')", False),
    ],
)
def test_unscoped_call_pattern(line, flagged):
    assert bool(_UNSCOPED_CALL.search(line)) is flagged


# ── US3: single-tenant instances are unchanged ───────────────────────────


@pytest.mark.parametrize(
    ("module", "tool", "arguments", "method", "suffix"),
    _READS + _WRITES,
    ids=[f"read-{r[0]}" for r in _READS] + [f"write-{w[0]}" for w in _WRITES],
)
async def test_single_tenant_requests_are_unchanged(
    monkeypatch, module, tool, arguments, method, suffix
):
    transport, executor = _install(monkeypatch, settings={"platform_version": "2.3"})
    await _run(executor, tool, arguments)
    assert transport.requests[:1] == [(method, f"{BASE}/api{suffix}")]


async def test_single_tenant_scenario_flow_is_unchanged(monkeypatch):
    transport, executor = _install(monkeypatch, settings={})
    await _run(executor, "create_openaev_scenario", {"name": "S"})
    await _run(
        executor, "manage_openaev_scenario", {"action": "launch", "scenario_id": "s1"}
    )
    assert transport.urls == [
        f"{BASE}/api/scenarios",
        f"{BASE}/api/scenarios/s1/exercise/running",
    ]


async def test_threat_arsenal_keeps_its_explicit_tenant_without_double_scoping(
    monkeypatch,
):
    # FR-009: a call that passes its own tenant_id is not scoped a second time.
    transport, executor = _install(monkeypatch, settings=MT)
    await _run(
        executor,
        "manage_openaev_threat_arsenal",
        {"action": "security_platforms", "action_id": "a1"},
    )
    assert transport.urls == [f"{BASE}/api/tenants/t1/threat_arsenals/a1/security-platforms"]


# ── US4: a failed tenant lookup is diagnosable ───────────────────────────

_HINT = "the OpenAEV tenant for this request could not be resolved"
_REJECTED = "OpenAEV error (HTTP 400): TENANT_WRITE_SCOPE"


async def test_failed_lookup_leaves_a_succeeding_request_untouched(monkeypatch):
    transport, executor = _install(monkeypatch, settings_error="HTTP 403: denied")
    created = await _run(executor, "create_openaev_scenario", {"name": "S"})
    org = await _run(executor, "manage_openaev_organization", {"action": "create", "name": "O"})
    assert transport.urls == [f"{BASE}/api/scenarios", f"{BASE}/api/organizations"]
    for result in (created, org):
        assert not result.is_error
        assert _HINT not in result.content


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("create_openaev_scenario", {"name": "S"}),  # core.py, direct request
        ("manage_openaev_organization", {"action": "create", "name": "O"}),  # _api
    ],
)
async def test_failed_lookup_is_named_when_the_request_fails(monkeypatch, tool, arguments):
    transport, executor = _install(
        monkeypatch, settings_error="HTTP 403: denied", fail=_REJECTED
    )
    result = await _run(executor, tool, arguments)
    assert _REJECTED in result.content
    assert _HINT in result.content
    assert "HTTP 403: denied" in result.content
    assert "/api/tenants/" not in transport.urls[0]


async def test_unparseable_settings_body_does_not_stop_tools(monkeypatch):
    transport, executor = _install(monkeypatch, bad_body=True, fail=_REJECTED)
    result = await _run(executor, "manage_openaev_organization", {"action": "create", "name": "O"})
    assert transport.urls == [f"{BASE}/api/organizations"]
    assert _HINT in result.content


async def test_settings_are_fetched_once_per_tool_set(monkeypatch):
    transport, executor = _install(monkeypatch, settings_error="HTTP 403: denied")
    for _ in range(3):
        await _run(executor, "manage_openaev_team", {"action": "list"})
    await _run(executor, "create_openaev_scenario", {"name": "S"})
    assert transport.settings_calls == 1


async def test_no_tenant_is_not_reported_as_a_failed_lookup(monkeypatch):
    # US3: a platform that simply has no default tenant gets no hint.
    _, executor = _install(monkeypatch, settings={}, fail=_REJECTED)
    result = await _run(executor, "manage_openaev_organization", {"action": "create", "name": "O"})
    assert _REJECTED in result.content
    assert _HINT not in result.content


async def test_failed_lookup_is_named_on_a_failed_document_upload(monkeypatch):
    from app.integrations.providers.openaev import content

    async def rejected(client, method, url, provider, action, **kwargs):
        return None, _REJECTED

    _, executor = _install(monkeypatch, settings_error="HTTP 403: denied")
    monkeypatch.setattr(content, "safe_request", rejected)
    result = await _run(
        executor,
        "manage_openaev_document",
        {"action": "upload", "name": "n.txt", "content": "hello"},
    )
    assert _REJECTED in result.content
    assert _HINT in result.content


# ── review follow-ups ────────────────────────────────────────────────────


def test_headers_carry_an_explicit_resolver_field():
    from app.integrations.providers.openaev._common import OpenAEVHeaders, _headers

    headers = _headers("tok")
    assert isinstance(headers, OpenAEVHeaders)
    assert headers.tenant_scope is None  # unscoped until build_tools binds one


def test_build_tools_binds_the_shared_capabilities_to_the_headers(monkeypatch):
    seen = {}
    real = _common._headers

    def spy(token, refresh_fn=None):
        seen["headers"] = real(token, refresh_fn)
        return seen["headers"]

    monkeypatch.setattr("app.integrations.providers.openaev._headers", spy)
    build_tools(TOKEN, CallableToolExecutor())
    assert isinstance(seen["headers"].tenant_scope, PlatformCapabilities)


async def test_settings_probe_retries_a_transient_failure(monkeypatch):
    # One 502 on the probe must not unscope the whole tool set for good.
    transport, executor = _install(monkeypatch, settings=MT)
    await _run(executor, "manage_openaev_team", {"action": "list"})
    retry = transport.settings_kwargs.get("retry_statuses")
    assert retry and {500, 502, 503, 504} <= set(retry)


async def test_hint_is_only_added_to_tenant_scope_rejections(monkeypatch):
    _, executor = _install(
        monkeypatch, settings_error="HTTP 403: denied", fail="OpenAEV error (HTTP 404): Not Found"
    )
    for tool, arguments in (
        ("create_openaev_scenario", {"name": "S"}),
        ("manage_openaev_organization", {"action": "create", "name": "O"}),
    ):
        result = await _run(executor, tool, arguments)
        assert "Not Found" in result.content
        assert _HINT not in result.content


async def test_scoping_survives_a_base_url_that_contains_api(monkeypatch):
    # The URL is split on the known base URL, not on the first "/api/".
    gateway = "https://gw.example.com/api/openaev"
    transport, executor = _install(monkeypatch, settings=MT, base=gateway)
    await _run(executor, "create_openaev_scenario", {"name": "S"})
    await _run(executor, "manage_openaev_organization", {"action": "create", "name": "O"})
    assert transport.urls == [
        f"{gateway}/api/tenants/t1/scenarios",
        f"{gateway}/api/tenants/t1/organizations",
    ]


async def test_catalog_reads_use_the_tenant_twin(monkeypatch):
    # Catalog entities (attack patterns, kill chain phases, contracts, domains,
    # tags) are tenant-owned in OpenAEV; the twin returns the default tenant's rows.
    transport, executor = _install(monkeypatch, settings=MT)
    await _run(executor, "manage_openaev_attack_pattern", {"action": "list"})
    await _run(executor, "manage_openaev_kill_chain_phase", {"action": "list"})
    await _run(executor, "manage_openaev_tag", {"action": "list"})
    await _run(executor, "list_openaev_domains", {})
    await _run(executor, "search_openaev_injector_contracts", {"search": "x"})
    assert [u.removeprefix(BASE) for u in transport.urls[:5]] == [
        "/api/tenants/t1/attack_patterns/search",
        "/api/tenants/t1/kill_chain_phases/search",
        "/api/tenants/t1/tags/search",
        "/api/tenants/t1/domains",
        "/api/tenants/t1/injector_contracts/search",
    ]


# ── settings probe, hints and the tenant path ────────────────────────────


async def test_settings_probe_never_consults_the_resolver(monkeypatch):
    # Structural, not list-dependent: even with NO exempt prefixes the probe
    # must not re-enter the tenant lookup it is holding the lock for.
    monkeypatch.setattr(_common, "_UNSCOPED_PREFIXES", ())
    caps, transport = _caps(monkeypatch, settings=MT)
    caps._headers.tenant_scope = caps
    scope = await asyncio.wait_for(caps.tenant_scope(), timeout=2)
    assert scope == TenantScope("t1", None, memberships_unknown=True)
    assert transport.urls == []  # the probe itself is not a recorded request


async def test_hint_keeps_only_the_first_sentence_of_a_long_cause(monkeypatch):
    long_cause = "OpenAEV error (HTTP 404): Not Found. " + "In OpenAEV a 404 can mean three things. " * 8
    _, executor = _install(monkeypatch, settings_error=long_cause, fail=_REJECTED)
    result = await _run(executor, "manage_openaev_organization", {"action": "create", "name": "O"})
    assert "Not Found" in result.content
    assert "three things" not in result.content
    assert len(result.content) < len(_REJECTED) + 260


def test_tenant_id_is_quoted_into_the_path():
    assert _common._tenant_path("/api/teams", "a/b?c") == "/api/tenants/a%2Fb%3Fc/teams"
    assert _common._tenant_path("/api/teams", "t1") == "/api/tenants/t1/teams"


def test_a_hand_scoped_marker_covers_only_the_call_below_it():
    text = (
        "# tenant-scoped by hand: path\n"
        "await safe_request(c, 'post', u, p, a)\n"
        "await safe_request(c, 'get', u, p, a)\n"
    )
    assert _unscoped_lines(text) == [3]


# ── The caller's tenant, not only the default one (TENANT_ACCESS_DENIED) ──


@pytest.mark.parametrize(
    ("members", "expected"),
    [
        (None, ("t1", None)),  # memberships unknown: the default tenant, as before
        (["tB"], ("tB", None)),  # only tenant: that tenant, never the default
        (["t1"], ("t1", None)),
        (["tB", "t1"], ("t1", None)),  # several, the default among them
    ],
)
def test_select_tenant_mirrors_the_openaev_fallback(members, expected):
    assert _select_tenant("t1", members) == expected


def test_select_tenant_with_no_membership_says_why():
    # OpenAEV fails closed by itself; the error keeps the cause diagnosable.
    tenant_id, error = _select_tenant("t1", [])
    assert tenant_id == ""
    assert error and "belongs to no tenant" in error


def test_select_tenant_without_the_default_needs_an_explicit_tenant():
    tenant_id, error = _select_tenant("t1", ["tB", "tC"])
    assert tenant_id == ""
    assert error and "2 tenants" in error


async def test_a_caller_outside_the_default_tenant_targets_its_own_tenant(monkeypatch):
    # The default tenant id is a platform constant; naming it in the path for a
    # caller who is not a member answers 403 TENANT_ACCESS_DENIED on every call.
    transport, executor = _install(monkeypatch, settings=MT, memberships=["tB"])
    await _run(executor, "create_openaev_scenario", {"name": "S"})
    await _run(executor, "manage_openaev_team", {"action": "list"})
    assert transport.urls[0] == f"{BASE}/api/tenants/tB/scenarios"
    assert all("/api/tenants/tB/" in u for u in transport.urls)
    assert not any("/api/tenants/t1/" in u for u in transport.urls)


async def test_a_member_of_several_tenants_with_the_default_targets_the_default(
    monkeypatch,
):
    transport, executor = _install(monkeypatch, settings=MT, memberships=["tB", "t1"])
    await _run(executor, "create_openaev_scenario", {"name": "S"})
    assert transport.urls == [f"{BASE}/api/tenants/t1/scenarios"]


async def test_several_tenants_without_the_default_stay_unscoped_and_say_why(
    monkeypatch,
):
    transport, executor = _install(
        monkeypatch, settings=MT, memberships=["tB", "tC"], fail=_REJECTED
    )
    result = await _run(executor, "create_openaev_scenario", {"name": "S"})
    assert transport.urls == [f"{BASE}/api/scenarios"]
    assert _HINT in result.content
    assert "2 tenants" in result.content


async def test_memberships_are_read_once_and_only_on_a_multi_tenant_platform(
    monkeypatch,
):
    transport, executor = _install(monkeypatch, settings=MT, memberships=["tB"])
    for _ in range(3):
        await _run(executor, "manage_openaev_team", {"action": "list"})
    assert transport.membership_calls == 1

    single, executor = _install(monkeypatch, settings={}, memberships=["tB"])
    await _run(executor, "manage_openaev_team", {"action": "list"})
    assert single.membership_calls == 0


async def test_threat_arsenal_explicit_tenant_is_the_callers_tenant(monkeypatch):
    caps, _ = _caps(monkeypatch, settings=MT, memberships=["tB"])
    assert await caps.tenant_id() == "tB"


async def test_an_unresolved_tenant_is_not_a_settings_failure(monkeypatch):
    # Feature gating lets calls through on a settings failure; a tenant that
    # needs an explicit choice must not switch that gating off.
    caps, _ = _caps(
        monkeypatch, settings={**MT, "enabled_dev_features": []}, memberships=["tB", "tC"]
    )
    assert (await caps.tenant_scope()).error
    assert await caps.require_feature("CHAINING", "Chaining")


# ── The integration's Tenant field ──────────────────────────────────────

_DENIED = (
    "Error: OpenAEV access denied while trying to create scenario — insufficient "
    "permissions or scopes. Detail: TENANT_ACCESS_DENIED"
)


async def test_the_tenant_field_wins_over_the_memberships(monkeypatch):
    transport, executor = _install(
        monkeypatch, tenant_id=" tC ", settings=MT, memberships=["tB", "t1"]
    )
    await _run(executor, "create_openaev_scenario", {"name": "S"})
    await _run(executor, "manage_openaev_team", {"action": "list"})
    assert transport.urls[0] == f"{BASE}/api/tenants/tC/scenarios"
    assert all("/api/tenants/tC/" in u for u in transport.urls)
    assert transport.membership_calls == 0


async def test_the_tenant_field_holds_when_the_settings_are_unreadable(monkeypatch):
    transport, executor = _install(
        monkeypatch, tenant_id="tC", settings_error="HTTP 403: denied"
    )
    await _run(executor, "create_openaev_scenario", {"name": "S"})
    assert transport.urls == [f"{BASE}/api/tenants/tC/scenarios"]


async def test_the_tenant_field_reaches_the_explicit_tenant_routes(monkeypatch):
    caps, _ = _caps(monkeypatch, tenant_id="tC", settings=MT, memberships=["tB"])
    assert await caps.tenant_id() == "tC"
    assert await caps.tenant_scope() == TenantScope("tC", None, configured=True)


async def test_a_denied_tenant_field_is_named(monkeypatch):
    _, executor = _install(monkeypatch, tenant_id="tC", settings=MT, fail=_DENIED)
    result = await _run(executor, "create_openaev_scenario", {"name": "S"})
    assert "TENANT_ACCESS_DENIED" in result.content
    assert "tenant tC (set in the integration's Tenant field)" in result.content


async def test_a_denied_automatic_tenant_points_to_the_tenant_field(monkeypatch):
    _, executor = _install(monkeypatch, settings=MT, memberships=["t1", "tB"], fail=_DENIED)
    result = await _run(executor, "manage_openaev_organization", {"action": "create", "name": "O"})
    assert "tenant t1 (chosen automatically; set the integration's Tenant field" in result.content


async def test_a_denied_default_tenant_says_when_the_memberships_were_unreadable(monkeypatch):
    # The membership read failed (404 here): the default tenant is a guess, and the error says so.
    _, executor = _install(monkeypatch, settings=MT, memberships=None, fail=_DENIED)
    result = await _run(executor, "manage_openaev_organization", {"action": "create", "name": "O"})
    assert "the account's tenants could not be read" in result.content
    assert "chosen automatically" not in result.content


async def test_a_succeeding_request_carries_no_tenant_note(monkeypatch):
    _, executor = _install(monkeypatch, tenant_id="tC", settings=MT)
    result = await _run(executor, "create_openaev_scenario", {"name": "S"})
    assert "Note:" not in result.content


async def test_several_tenants_without_the_default_point_to_the_tenant_field(monkeypatch):
    _, executor = _install(
        monkeypatch, settings=MT, memberships=["tB", "tC"], fail=_REJECTED
    )
    result = await _run(executor, "create_openaev_scenario", {"name": "S"})
    assert "set the OpenAEV integration's Tenant field" in result.content


def test_the_catalog_offers_an_optional_tenant_field():
    from app.api.v1.integration_catalog import PROVIDER_CATALOG

    fields = PROVIDER_CATALOG["openaev"]["extra_fields"]
    assert "tenant_id" in fields
    assert "optional" in fields["tenant_id"]


async def _builder_kwargs(monkeypatch, provider: str, auth_type: str, value: str):
    """Run the real tool-build loop for one binding; returns (kwargs, lookup mock)."""
    import app.services.integration_tools as integration_tools

    integration_id = str(uuid.uuid4())
    row = SimpleNamespace(
        id=uuid.UUID(integration_id), name="I", description=None, auth_type=auth_type
    )

    class _Db:
        async def execute(self, *_args, **_kwargs):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [row]))

    monkeypatch.setattr(
        integration_tools,
        "_parallel_refresh_expiring_tokens",
        AsyncMock(return_value=({}, set())),
    )
    monkeypatch.setattr(integration_tools, "_get_token", AsyncMock(return_value="token"))
    lookup = AsyncMock(return_value=value)
    monkeypatch.setattr(integration_tools, "_get_extra_config_value", lookup)
    seen: list[dict] = []

    def _record(_token, _executor, **kwargs):
        seen.append(kwargs)
        return []

    monkeypatch.setitem(integration_tools.PROVIDER_TOOLS, provider, _record)
    await integration_tools.build_integration_tools(
        _Db(), ToolBindings({provider: [integration_id]}, basis=BindingBasis.CALLER_CHECKED, reason="test"), context={"source": "test"}
    )
    [kwargs] = seen
    return kwargs, lookup, integration_id


@pytest.mark.parametrize("auth_type", ["api_key", "oauth"])
async def test_the_tool_build_hands_the_tenant_field_to_openaev(monkeypatch, auth_type):
    kwargs, lookup, integration_id = await _builder_kwargs(
        monkeypatch, "openaev", auth_type, "tC"
    )
    assert kwargs["tenant_id"] == "tC"
    assert lookup.await_args.args[1:] == (integration_id, "tenant_id")
    assert "token_acquirer" not in kwargs


async def test_the_tool_build_keeps_purview_oauth_token_acquirer(monkeypatch):
    # Regression: the OpenAEV branch once captured Purview's OAuth wiring.
    kwargs, lookup, integration_id = await _builder_kwargs(
        monkeypatch, "purview", "oauth", "acme"
    )
    assert kwargs["account_name"] == "acme"
    assert lookup.await_args.args[1:] == (integration_id, "account_name")
    assert callable(kwargs.get("token_acquirer"))


async def test_extra_config_value_reads_one_stripped_field():
    from app.core.security import encrypt_value
    from app.services.integration_tools import _get_extra_config_value

    integration_id = str(uuid.uuid4())
    row = SimpleNamespace(
        __tablename__="integrations",
        id=integration_id,
        extra_config_enc=encrypt_value(json.dumps({"base_url": "https://x", "tenant_id": " tC "})),
    )

    class _Db:
        async def execute(self, *_args, **_kwargs):
            return SimpleNamespace(scalar_one_or_none=lambda: row)

    assert await _get_extra_config_value(_Db(), integration_id, "tenant_id") == "tC"
    assert await _get_extra_config_value(_Db(), integration_id, "missing") == ""


# ── A tenant-scoped 404 can be an entity of another tenant ──────────────

_NOT_FOUND = "OpenAEV error (HTTP 404): Element not found"


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_openaev_scenario", {"scenario_id": "s9"}),  # core.py, scoped_safe_request
        ("manage_openaev_team", {"action": "get", "team_id": "x9"}),  # _api
    ],
)
async def test_a_scoped_404_names_the_other_tenant_cause(monkeypatch, tool, arguments):
    _, executor = _install(monkeypatch, settings=MT, memberships=["t1"], fail=_NOT_FOUND)
    result = await _run(executor, tool, arguments)
    assert "an entity of another tenant: this request only sees the tenant t1" in result.content


async def test_an_unscoped_404_has_no_tenant_cause(monkeypatch):
    _, executor = _install(monkeypatch, settings={}, fail=_NOT_FOUND)
    result = await _run(executor, "get_openaev_scenario", {"scenario_id": "s9"})
    assert "another tenant" not in result.content
