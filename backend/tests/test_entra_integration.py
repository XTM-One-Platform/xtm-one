"""Tests for the Microsoft Entra ID (Azure AD) integration provider.

Covers the dual-auth client mode detection, the tool surface produced by
``build_tools`` (both token shapes), the advanced-query header handling, the
``@odata.nextLink`` pagination extraction, and the request shape / behaviour of
a representative slice of tools across domains (user offboarding, group
membership, RBAC assignment, identity-protection response).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.integrations import oauth
from app.integrations.providers import entra
from app.integrations.providers.entra import EntraClient, build_tools
from app.integrations.providers.entra._common import (
    build_search_params,
    next_skiptoken,
)
from app.integrations.providers.entra.directory import PASSWORD_RULE_TEMPLATE_ID
from app.tools.base import CallableToolExecutor

# tenant||client_id||client_secret triggers app-only mode.
_APP_TOKEN = "tenant-1||client-1||secret-1"
# A plain Graph access token triggers delegated mode.
_DELEGATED_TOKEN = "graph-access-token"


class _FakeResp:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.content = b"{}" if payload is not None else b""

    def json(self) -> dict:
        return self._payload


def _handlers(token: str = _DELEGATED_TOKEN) -> dict:
    executor = CallableToolExecutor()
    build_tools(token, executor)
    return executor._handlers


# ---------------------------------------------------------------------------
# Auth-mode detection + tool surface
# ---------------------------------------------------------------------------


def test_client_detects_app_only_vs_delegated():
    app = EntraClient(_APP_TOKEN)
    assert app.app_only is True
    assert (app.tenant_id, app.client_id, app.client_secret) == (
        "tenant-1",
        "client-1",
        "secret-1",
    )
    assert app.access_token == ""

    delegated = EntraClient(_DELEGATED_TOKEN)
    assert delegated.app_only is False
    assert delegated.access_token == _DELEGATED_TOKEN


def test_client_strips_whitespace_in_compound_token_segments():
    # A client id/secret copy/pasted into the encrypted credential column often
    # carries trailing whitespace/newlines. The app-only client strips every
    # compound-token segment (and the delegated access token) so the minted
    # client-credentials request / Bearer header is always well-formed.
    app = EntraClient("  tenant-1\n||\tclient-1 ||secret-1\r\n")
    assert app.app_only is True
    assert (app.tenant_id, app.client_id, app.client_secret) == (
        "tenant-1",
        "client-1",
        "secret-1",
    )
    assert EntraClient(" graph-access-token\n").access_token == "graph-access-token"


def test_build_tools_full_surface_both_token_shapes():
    delegated = build_tools(_DELEGATED_TOKEN, CallableToolExecutor())
    app_only = build_tools(_APP_TOKEN, CallableToolExecutor())
    # Both auth modes expose the identical, full tool surface.
    assert len(delegated) == len(app_only)
    assert len(delegated) >= 45
    names = {t.name for t in delegated}
    # A representative tool from every domain must be present.
    for expected in (
        "list_entra_users",
        "set_entra_user_account_enabled",
        "revoke_entra_user_sessions",
        "add_entra_group_member",
        "assign_entra_directory_role",
        "list_entra_role_members",
        "list_entra_applications",
        "list_entra_devices",
        "list_entra_signin_logs",
        "list_entra_risky_users",
        "confirm_entra_users_compromised",
        "list_entra_subscribed_skus",
        "get_entra_password_protection",
    ):
        assert expected in names


def test_build_tools_dummy_token_introspection_works():
    # The catalog / tools endpoint builds with the bare "__dummy__" token.
    defs = build_tools("__dummy__", CallableToolExecutor(), refresh_fn=lambda: None)
    assert len(defs) >= 45
    assert all(t.source == "integration:entra" for t in defs)


def test_tool_priorities_sort_reads_before_writes_before_destructive():
    by_name = {t.name: t for t in build_tools(_DELEGATED_TOKEN, CallableToolExecutor())}
    assert by_name["list_entra_users"].priority == 0
    assert by_name["set_entra_user_account_enabled"].priority == 1
    assert by_name["delete_entra_group"].priority == 2
    assert by_name["remove_entra_role_assignment"].priority == 2


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def test_build_search_params_sets_advanced_query_header_for_search():
    params, headers = build_search_params(search="jane")
    assert params["$search"] == '"jane"'
    assert params["$count"] == "true"
    assert headers["ConsistencyLevel"] == "eventual"


def test_build_search_params_filter_only_has_no_consistency_header():
    params, headers = build_search_params(odata_filter="accountEnabled eq false")
    assert params["$filter"] == "accountEnabled eq false"
    assert "ConsistencyLevel" not in headers


def test_next_skiptoken_extracts_token_from_nextlink():
    data = {
        "@odata.nextLink": (
            "https://graph.microsoft.com/v1.0/users?$top=25&$skiptoken=ABC123"
        )
    }
    assert next_skiptoken(data) == "ABC123"
    assert next_skiptoken({}) == ""


# ---------------------------------------------------------------------------
# Request shape / behaviour (patch EntraClient.request — every higher-level
# helper, get_json / paged, routes through it)
# ---------------------------------------------------------------------------


def _patch_request(monkeypatch, *, resp: _FakeResp | None = None, err: str | None = None):
    """Patch EntraClient.request to capture calls and return a canned result."""
    calls: list[dict[str, Any]] = []

    async def _fake_request(self, method, path, action, **kwargs):
        calls.append({"method": method, "path": path, "action": action, **kwargs})
        return (resp if resp is not None else _FakeResp()), err

    monkeypatch.setattr(EntraClient, "request", _fake_request)
    return calls


@pytest.mark.asyncio
async def test_disable_account_patches_account_enabled_and_warns_about_tokens(
    monkeypatch,
):
    calls = _patch_request(monkeypatch)
    handler = _handlers()["set_entra_user_account_enabled"]
    result = await handler(user_id="jane@contoso.com", enabled=False)

    assert calls[0]["method"] == "patch"
    assert calls[0]["path"] == "/users/jane%40contoso.com"
    assert calls[0]["json"] == {"accountEnabled": False}
    assert "disabled" in result
    # Disabling must point the agent at revoke_entra_user_sessions.
    assert "revoke_entra_user_sessions" in result


@pytest.mark.asyncio
async def test_revoke_sessions_posts_to_revoke_endpoint(monkeypatch):
    calls = _patch_request(monkeypatch)
    handler = _handlers()["revoke_entra_user_sessions"]
    result = await handler(user_id="00000000-0000-0000-0000-000000000001")

    assert calls[0]["method"] == "post"
    assert calls[0]["path"].endswith("/revokeSignInSessions")
    assert "Revoked" in result


@pytest.mark.asyncio
async def test_add_group_member_builds_directory_object_ref(monkeypatch):
    calls = _patch_request(monkeypatch)
    handler = _handlers()["add_entra_group_member"]
    await handler(group_id="grp-1", member_id="usr-1")

    assert calls[0]["method"] == "post"
    assert calls[0]["path"] == "/groups/grp-1/members/$ref"
    assert (
        calls[0]["json"]["@odata.id"]
        == "https://graph.microsoft.com/v1.0/directoryObjects/usr-1"
    )


@pytest.mark.asyncio
async def test_assign_directory_role_posts_unified_role_assignment(monkeypatch):
    calls = _patch_request(monkeypatch)
    handler = _handlers()["assign_entra_directory_role"]
    result = await handler(role_definition_id="role-1", principal_id="usr-1")

    assert calls[0]["method"] == "post"
    assert calls[0]["path"] == "/roleManagement/directory/roleAssignments"
    body = calls[0]["json"]
    assert body["@odata.type"] == "#microsoft.graph.unifiedRoleAssignment"
    assert body["roleDefinitionId"] == "role-1"
    assert body["principalId"] == "usr-1"
    assert body["directoryScopeId"] == "/"
    assert "privileged" in result.lower()


@pytest.mark.asyncio
async def test_confirm_compromised_posts_user_ids(monkeypatch):
    calls = _patch_request(monkeypatch)
    handler = _handlers()["confirm_entra_users_compromised"]
    result = await handler(user_ids=["a", "b"])

    assert calls[0]["method"] == "post"
    assert calls[0]["path"].endswith("/confirmCompromised")
    assert calls[0]["json"] == {"userIds": ["a", "b"]}
    assert "2 user" in result


@pytest.mark.asyncio
async def test_confirm_compromised_requires_user_ids(monkeypatch):
    calls = _patch_request(monkeypatch)
    handler = _handlers()["confirm_entra_users_compromised"]
    result = await handler(user_ids=[])
    # Validation happens before any request.
    assert calls == []
    assert "required" in result.lower()


@pytest.mark.asyncio
async def test_assign_license_via_manage_user_requires_sku(monkeypatch):
    calls = _patch_request(monkeypatch)
    handler = _handlers()["manage_entra_user"]
    result = await handler(action="assign_license", user_id="usr-1", sku_id="")
    assert calls == []
    assert "sku_id is required" in result


@pytest.mark.asyncio
async def test_manage_user_unknown_action_is_rejected(monkeypatch):
    _patch_request(monkeypatch)
    handler = _handlers()["manage_entra_user"]
    result = await handler(action="bogus", user_id="usr-1")
    assert "unknown action" in result.lower()


@pytest.mark.asyncio
async def test_list_users_passes_search_and_returns_formatted_list(monkeypatch):
    calls = _patch_request(
        monkeypatch,
        resp=_FakeResp(
            200,
            {
                "value": [
                    {
                        "id": "1",
                        "displayName": "Jane Doe",
                        "userPrincipalName": "jane@contoso.com",
                        "accountEnabled": True,
                    }
                ],
                "@odata.nextLink": (
                    "https://graph.microsoft.com/v1.0/users?$skiptoken=NEXT"
                ),
            },
        ),
    )
    handler = _handlers()["list_entra_users"]
    result = await handler(search="jane")

    # Advanced-query header + $search param are forwarded to the request.
    assert calls[0]["extra_headers"]["ConsistencyLevel"] == "eventual"
    # Graph needs "property:value" clauses: free text is expanded per field.
    assert calls[0]["params"]["$search"] == '"displayName:jane" OR "mail:jane" OR "userPrincipalName:jane"'
    assert "Jane Doe" in result
    assert "jane@contoso.com" in result
    # The nextLink skiptoken surfaces as a pagination hint.
    assert "NEXT" in result


@pytest.mark.asyncio
async def test_app_only_auth_failure_returns_auth_error(monkeypatch):
    # In app-only mode, a failed token mint must surface the auth-error string
    # (not crash) from any tool. Patch get_app_token to fail.
    async def _no_token(*args, **kwargs):
        return None

    monkeypatch.setattr(entra._common, "get_app_token", _no_token)
    handler = _handlers(_APP_TOKEN)["get_entra_organization"]
    result = await handler()
    assert "authenticate with Microsoft Entra" in result


@pytest.mark.asyncio
async def test_validate_connection_true_on_success(monkeypatch):
    _patch_request(monkeypatch, resp=_FakeResp(200, {"value": [{"id": "t"}]}))
    integ = entra.EntraIntegration()
    assert await integ.validate_connection(_DELEGATED_TOKEN) is True


@pytest.mark.asyncio
async def test_validate_connection_false_on_error(monkeypatch):
    _patch_request(monkeypatch, resp=None, err="Error: Entra access denied")
    integ = entra.EntraIntegration()
    assert await integ.validate_connection(_DELEGATED_TOKEN) is False


# ---------------------------------------------------------------------------
# OAuth /common -> tenant-specific endpoint rewrite (single-tenant apps)
#
# Single-tenant Entra app registrations reject the /common authorize/token
# endpoint with AADSTS50194. The provider must be in the set that rewrites
# /common/ to /{tenant_id}/ (the same gate that makes Intune/Defender work).
# ---------------------------------------------------------------------------


def test_entra_is_in_common_endpoint_rewrite_set():
    assert "entra" in oauth._ENTRA_ID_PROVIDERS


async def test_resolve_tenant_urls_rewrites_common_for_entra(monkeypatch):
    monkeypatch.setattr(oauth, "reveal_json_field", AsyncMock(return_value={"tenant_id": "TENANT-XYZ"}))
    integ = SimpleNamespace(provider="entra", extra_config_enc=b"enc")
    authorize, token = await oauth._resolve_tenant_urls(
        integ,
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
    )
    assert "/TENANT-XYZ/" in authorize
    assert "/TENANT-XYZ/" in token
    assert "/common/" not in authorize
    assert "/common/" not in token


async def test_resolve_tenant_urls_noop_when_tenant_id_missing(monkeypatch):
    # No tenant_id configured -> leave /common/ untouched (multi-tenant apps).
    monkeypatch.setattr(oauth, "reveal_json_field", AsyncMock(return_value={}))
    integ = SimpleNamespace(provider="entra", extra_config_enc=b"enc")
    authorize, _ = await oauth._resolve_tenant_urls(
        integ,
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        None,
    )
    assert "/common/" in authorize


# ---------------------------------------------------------------------------
# URL-encoding of path segments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_risky_user_history_url_encodes_user_id(monkeypatch):
    calls = _patch_request(monkeypatch, resp=_FakeResp(200, {"value": []}))
    handler = _handlers()["get_entra_risky_user_history"]
    await handler(user_id="jane@contoso.com")
    # A UPN with reserved characters must be percent-encoded into the path.
    assert (
        calls[0]["path"]
        == "/identityProtection/riskyUsers/jane%40contoso.com/history"
    )


# ---------------------------------------------------------------------------
# Log date-range validation (ISO 8601 / filter-injection guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signin_logs_reject_injection_like_date(monkeypatch):
    calls = _patch_request(monkeypatch)
    handler = _handlers()["list_entra_signin_logs"]
    result = await handler(start_date="2026-06-01T00:00:00Z or userId eq 'x'")
    # A malformed / filter-injection value never reaches Graph.
    assert calls == []
    assert "ISO 8601" in result


@pytest.mark.asyncio
async def test_audit_logs_reject_malformed_end_date(monkeypatch):
    calls = _patch_request(monkeypatch)
    handler = _handlers()["list_entra_audit_logs"]
    result = await handler(end_date="not-a-date")
    assert calls == []
    assert "ISO 8601" in result


@pytest.mark.asyncio
async def test_signin_logs_normalize_valid_date_into_filter(monkeypatch):
    calls = _patch_request(monkeypatch, resp=_FakeResp(200, {"value": []}))
    handler = _handlers()["list_entra_signin_logs"]
    await handler(start_date="2026-06-01T00:00:00Z")
    assert "createdDateTime ge 2026-06-01T00:00:00Z" in calls[0]["params"]["$filter"]


# ---------------------------------------------------------------------------
# Directory audits filter on activityDateTime (directoryAudit has no
# createdDateTime: Graph answers 400 on every filtered call otherwise)
# ---------------------------------------------------------------------------

_ACTIVITY = "Update application - Certificates and secrets management"


@pytest.mark.asyncio
async def test_audit_logs_filter_uses_activity_datetime_with_dates(monkeypatch):
    calls = _patch_request(monkeypatch, resp=_FakeResp(200, {"value": []}))
    handler = _handlers()["list_entra_audit_logs"]
    result = await handler(
        activity=_ACTIVITY,
        start_date="2026-10-01T00:00:00Z",
        end_date="2026-10-31T00:00:00Z",
    )
    assert calls[0]["path"] == "/auditLogs/directoryAudits"
    flt = calls[0]["params"]["$filter"]
    assert flt == (
        "activityDateTime ge 2026-10-01T00:00:00Z"
        " and activityDateTime le 2026-10-31T00:00:00Z"
        f" and activityDisplayName eq '{_ACTIVITY}'"
    )
    assert "createdDateTime" not in flt
    assert "No directory audit events" in result


@pytest.mark.asyncio
async def test_audit_logs_filter_activity_only_never_uses_created_datetime(monkeypatch):
    calls = _patch_request(monkeypatch, resp=_FakeResp(200, {"value": []}))
    handler = _handlers()["list_entra_audit_logs"]
    await handler(activity=_ACTIVITY)
    flt = calls[0]["params"]["$filter"]
    # The default 30-day window keeps the query bounded, on the right property.
    assert flt.startswith("activityDateTime ge ")
    assert " le " not in flt
    assert flt.endswith(f" and activityDisplayName eq '{_ACTIVITY}'")
    assert "createdDateTime" not in flt


@pytest.mark.asyncio
async def test_signin_logs_filter_keeps_created_datetime(monkeypatch):
    calls = _patch_request(monkeypatch, resp=_FakeResp(200, {"value": []}))
    handler = _handlers()["list_entra_signin_logs"]
    await handler(start_date="2026-10-01T00:00:00Z", end_date="2026-10-02T00:00:00Z")
    flt = calls[0]["params"]["$filter"]
    assert flt == (
        "createdDateTime ge 2026-10-01T00:00:00Z"
        " and createdDateTime le 2026-10-02T00:00:00Z"
    )
    assert "activityDateTime" not in flt


@pytest.mark.asyncio
async def test_audit_logs_pass_graph_error_through_unchanged(monkeypatch):
    graph_err = (
        "Error: Microsoft Entra returned HTTP 400 while trying to list directory "
        "audit logs: Invalid filter clause"
    )
    _patch_request(monkeypatch, resp=_FakeResp(400, {}), err=graph_err)
    handler = _handlers()["list_entra_audit_logs"]
    result = await handler(activity=_ACTIVITY)
    assert result == graph_err


@pytest.mark.asyncio
async def test_audit_logs_name_a_user_target_by_upn(monkeypatch):
    # A "Reset user password" event carries the target user's UPN and no display name.
    event = {
        "activityDateTime": "2026-11-20T10:00:00Z",
        "activityDisplayName": "Reset user password",
        "result": "success",
        "category": "UserManagement",
        "initiatedBy": {"user": {"userPrincipalName": "admin@contoso.com"}},
        "targetResources": [
            {"id": "u-1", "displayName": None, "userPrincipalName": "jane@contoso.com"},
            {"id": "u-2", "displayName": None},
        ],
    }
    _patch_request(monkeypatch, resp=_FakeResp(200, {"value": [event]}))
    result = await _handlers()["list_entra_audit_logs"](activity="Reset user password")
    assert "target: jane@contoso.com (+1 more)" in result


@pytest.mark.asyncio
async def test_get_user_selects_and_shows_last_password_change(monkeypatch):
    user = {
        "id": "u-1",
        "displayName": "Jane",
        "userPrincipalName": "jane@contoso.com",
        "lastPasswordChangeDateTime": "2026-11-01T08:00:00Z",
    }
    calls = _patch_request(monkeypatch, resp=_FakeResp(200, user))
    result = await _handlers()["get_entra_user"](user_id="jane@contoso.com")
    assert "lastPasswordChangeDateTime" in calls[0]["params"]["$select"].split(",")
    assert "last password change: " in result


# ---------------------------------------------------------------------------
# Secret/password reveal gating (no accidental credential leak to transcript)
# ---------------------------------------------------------------------------

_SECRET_RESP = {
    "keyId": "k1",
    "endDateTime": "2027-01-01T00:00:00Z",
    "secretText": "SUPER-SECRET-VALUE",
}


@pytest.mark.asyncio
async def test_add_password_hides_secret_by_default(monkeypatch):
    _patch_request(monkeypatch, resp=_FakeResp(200, _SECRET_RESP))
    handler = _handlers()["manage_entra_application"]
    result = await handler(action="add_password", app_id="app-1")
    assert "SUPER-SECRET-VALUE" not in result
    assert "reveal_secret=true" in result
    assert "k1" in result  # keyId + expiry are still surfaced for hygiene.


@pytest.mark.asyncio
async def test_add_password_reveals_secret_on_opt_in(monkeypatch):
    _patch_request(monkeypatch, resp=_FakeResp(200, _SECRET_RESP))
    handler = _handlers()["manage_entra_application"]
    result = await handler(action="add_password", app_id="app-1", reveal_secret=True)
    assert "SUPER-SECRET-VALUE" in result


@pytest.mark.asyncio
async def test_reset_password_hides_generated_password_by_default(monkeypatch):
    _patch_request(monkeypatch, resp=_FakeResp(200, {"newPassword": "Temp!2345"}))
    handler = _handlers()["reset_entra_user_password"]
    result = await handler(user_id="usr-1")
    assert "Temp!2345" not in result
    assert "reveal_password=true" in result


@pytest.mark.asyncio
async def test_reset_password_reveals_generated_password_on_opt_in(monkeypatch):
    _patch_request(monkeypatch, resp=_FakeResp(200, {"newPassword": "Temp!2345"}))
    handler = _handlers()["reset_entra_user_password"]
    result = await handler(user_id="usr-1", reveal_password=True)
    assert "Temp!2345" in result


@pytest.mark.asyncio
async def test_reset_password_never_echoes_caller_supplied_password(monkeypatch):
    # Even with reveal_password=True, a caller-supplied new_password must never be
    # echoed back - Graph may reflect it in the response body, and only an
    # auto-generated password is eligible to be revealed.
    _patch_request(monkeypatch, resp=_FakeResp(200, {"newPassword": "Caller-Secret-1"}))
    handler = _handlers()["reset_entra_user_password"]
    result = await handler(
        user_id="usr-1", new_password="Caller-Secret-1", reveal_password=True
    )
    assert "Caller-Secret-1" not in result


@pytest.mark.asyncio
async def test_reset_password_survives_non_dict_success_body(monkeypatch):
    # A successful reset whose body is not a JSON object (e.g. a proxy/WAF page,
    # an empty 204, or a JSON array) must not raise out of the tool - safe_json
    # degrades it to {} and the tool returns its basic success message.
    class _BadResp:
        content = b"[]"

        def json(self):
            return [1, 2, 3]

    async def _fake_request(self, method, path, action, **kwargs):
        return _BadResp(), None

    monkeypatch.setattr(EntraClient, "request", _fake_request)
    handler = _handlers()["reset_entra_user_password"]
    result = await handler(user_id="usr-1", reveal_password=True)
    assert "Initiated password reset for user usr-1" in result


@pytest.mark.asyncio
async def test_manage_application_add_password_requires_app_id(monkeypatch):
    calls = _patch_request(monkeypatch)
    handler = _handlers()["manage_entra_application"]
    result = await handler(action="add_password", app_id="")
    # Validation happens before any request - no opaque /applications//addPassword.
    assert calls == []
    assert "app_id is required" in result


@pytest.mark.asyncio
async def test_manage_application_list_owners_requires_app_or_sp(monkeypatch):
    calls = _patch_request(monkeypatch)
    handler = _handlers()["manage_entra_application"]
    result = await handler(action="list_owners", app_id="", sp_id="")
    assert calls == []
    assert "app_id or sp_id" in result


@pytest.mark.asyncio
async def test_manage_group_add_owner_requires_principal_id(monkeypatch):
    calls = _patch_request(monkeypatch)
    handler = _handlers()["manage_entra_group"]
    result = await handler(action="add_owner", group_id="grp-1", principal_id="")
    assert calls == []
    assert "principal_id" in result


# ---------------------------------------------------------------------------
# Recycle-bin formatting (deleted groups use a group-shaped formatter)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_deleted_groups_uses_group_formatter(monkeypatch):
    calls = _patch_request(
        monkeypatch,
        resp=_FakeResp(
            200,
            {
                "value": [
                    {
                        "id": "g1",
                        "displayName": "Sales",
                        "mail": "sales@contoso.com",
                        "deletedDateTime": "2026-06-01T00:00:00Z",
                    }
                ]
            },
        ),
    )
    handler = _handlers()["manage_entra_directory"]
    result = await handler(action="list_deleted_groups")
    assert calls[0]["path"] == "/directory/deletedItems/microsoft.graph.group"
    assert "Sales" in result
    assert "sales@contoso.com" in result
    # The user formatter would have rendered a bogus "<?>" UPN placeholder.
    assert "<?>" not in result


# ---------------------------------------------------------------------------
# $search escaping + role-member roleTemplateId fallback
# ---------------------------------------------------------------------------


def test_build_search_params_escapes_quotes_and_backslashes():
    params, headers = build_search_params(search='a"b\\c')
    # Backslashes and quotes are escaped before the outer quote-wrap so the
    # $search value stays syntactically valid (no HTTP 400).
    assert params["$search"] == '"a\\"b\\\\c"'
    assert headers["ConsistencyLevel"] == "eventual"


def test_build_search_params_expands_free_text_into_property_clauses():
    params, headers = build_search_params(search=" boitelle ", search_fields=("displayName", "mail"))
    assert params["$search"] == '"displayName:boitelle" OR "mail:boitelle"'
    assert headers["ConsistencyLevel"] == "eventual"


def test_build_search_params_keeps_a_qualified_clause():
    params, _ = build_search_params(search="displayName:jane doe", search_fields=("displayName", "mail"))
    assert params["$search"] == '"displayName:jane doe"'


def test_build_search_params_escapes_inside_each_clause():
    params, _ = build_search_params(search='a"b', search_fields=("displayName",))
    assert params["$search"] == '"displayName:a\\"b"'


@pytest.mark.asyncio
async def test_role_members_falls_back_to_template_id_parenthesized_form(monkeypatch):
    calls: list[dict[str, Any]] = []

    async def _fake_request(self, method, path, action, **kwargs):
        calls.append({"method": method, "path": path, "action": action, **kwargs})
        # First attempt (object-id form) 404s; the roleTemplateId fallback wins.
        if len(calls) == 1:
            return _FakeResp(404), None
        return (
            _FakeResp(
                200,
                {
                    "value": [
                        {
                            "displayName": "Jane Admin",
                            "userPrincipalName": "jane@contoso.com",
                            "id": "u1",
                        }
                    ]
                },
            ),
            None,
        )

    monkeypatch.setattr(EntraClient, "request", _fake_request)
    handler = _handlers()["list_entra_role_members"]
    tid = "62e90394-69f5-4237-9190-012177145e10"
    result = await handler(role_id=tid)

    assert len(calls) == 2
    assert calls[0]["path"] == f"/directoryRoles/{tid}/members"
    # Fallback uses the documented parenthesised alternate-key form.
    assert calls[1]["path"] == f"/directoryRoles(roleTemplateId='{tid}')/members"
    assert "Jane Admin" in result


# ---------------------------------------------------------------------------
# Pagination: list tools forward skiptoken as $skiptoken so the "More results"
# hint they emit is actually followable.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_group_members_forwards_skiptoken(monkeypatch):
    calls = _patch_request(monkeypatch, resp=_FakeResp(200, {"value": []}))
    handler = _handlers()["list_entra_group_members"]
    await handler(group_id="grp-1", skiptoken="TOK123")
    assert calls[0]["params"]["$skiptoken"] == "TOK123"


@pytest.mark.asyncio
async def test_list_user_groups_omits_skiptoken_when_absent(monkeypatch):
    # Without a skiptoken the param must not be sent (None params), so the first
    # page is requested cleanly.
    calls = _patch_request(monkeypatch, resp=_FakeResp(200, {"value": []}))
    handler = _handlers()["list_entra_user_groups"]
    await handler(user_id="usr-1")
    assert (calls[0].get("params") or {}).get("$skiptoken") is None


@pytest.mark.asyncio
async def test_manage_user_list_action_forwards_skiptoken(monkeypatch):
    calls = _patch_request(monkeypatch, resp=_FakeResp(200, {"value": []}))
    handler = _handlers()["manage_entra_user"]
    await handler(action="list_direct_reports", user_id="usr-1", skiptoken="TOK456")
    assert calls[0]["params"]["$skiptoken"] == "TOK456"


@pytest.mark.asyncio
async def test_manage_application_list_owners_forwards_skiptoken(monkeypatch):
    calls = _patch_request(monkeypatch, resp=_FakeResp(200, {"value": []}))
    handler = _handlers()["manage_entra_application"]
    await handler(action="list_owners", app_id="app-1", skiptoken="TOK789")
    assert calls[0]["params"]["$skiptoken"] == "TOK789"


@pytest.mark.asyncio
async def test_risky_user_history_forwards_skiptoken(monkeypatch):
    calls = _patch_request(monkeypatch, resp=_FakeResp(200, {"value": []}))
    handler = _handlers()["get_entra_risky_user_history"]
    await handler(user_id="usr-1", skiptoken="TOKABC")
    assert calls[0]["params"]["$skiptoken"] == "TOKABC"


@pytest.mark.asyncio
async def test_manage_directory_deleted_groups_forwards_skiptoken(monkeypatch):
    calls = _patch_request(monkeypatch, resp=_FakeResp(200, {"value": []}))
    handler = _handlers()["manage_entra_directory"]
    await handler(action="list_deleted_groups", skiptoken="TOKDEF")
    assert calls[0]["params"]["$skiptoken"] == "TOKDEF"


# ---------------------------------------------------------------------------
# App-only token mint: a non-JSON success body must not crash the caller
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_app_token_handles_non_json_success_body(monkeypatch):
    """A 200 response whose body is not JSON (e.g. a proxy returning HTML) must
    not raise out of get_app_token - it returns None so the caller surfaces the
    standard auth error instead of crashing the tool / connection test."""

    class _NonJsonResp:
        is_success = True
        text = "<html>proxy login</html>"

        def json(self):
            raise ValueError("not json")

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return _NonJsonResp()

    monkeypatch.setattr(entra._common.httpx, "AsyncClient", _FakeAsyncClient)
    # Unique credentials avoid hitting the in-process token cache.
    token = await entra._common.get_app_token(
        "tenant-nonjson", "client-nonjson", "secret-nonjson"
    )
    assert token is None


# ---------------------------------------------------------------------------
# get_json: a non-JSON 200 body is an actionable error, not silent "no data"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_json_non_json_body_is_error(monkeypatch):
    class _BadResp:
        content = b"<html>login</html>"

        def json(self):
            raise ValueError("not json")

    async def _fake_request(self, method, path, action, **kwargs):
        return _BadResp(), None

    monkeypatch.setattr(EntraClient, "request", _fake_request)
    data, err = await EntraClient(_DELEGATED_TOKEN).get_json(
        "/organization", "validate connection"
    )
    assert data is None
    assert err is not None and "non-JSON" in err


@pytest.mark.asyncio
async def test_get_json_empty_body_stays_graceful(monkeypatch):
    class _EmptyResp:
        content = b""

        def json(self):
            raise ValueError("no content to decode")

    async def _fake_request(self, method, path, action, **kwargs):
        return _EmptyResp(), None

    monkeypatch.setattr(EntraClient, "request", _fake_request)
    data, err = await EntraClient(_DELEGATED_TOKEN).get_json("/x", "y")
    # A genuinely empty body is still treated as an empty (but valid) result.
    assert data == {}
    assert err is None


@pytest.mark.asyncio
async def test_list_conditional_access_policies_forwards_skiptoken(monkeypatch):
    calls = _patch_request(monkeypatch, resp=_FakeResp(200, {"value": []}))
    handler = _handlers()["list_entra_conditional_access_policies"]
    await handler(skiptoken="TOKCAP")
    assert calls[0]["params"]["$skiptoken"] == "TOKCAP"


# ---------------------------------------------------------------------------
# safe_json: write/confirm reads never raise on a non-JSON success body
# ---------------------------------------------------------------------------


def test_safe_json_degrades_non_json_and_non_dict_to_empty():
    from app.integrations.providers.entra._common import safe_json

    class _Bad:
        def json(self):
            raise ValueError("not json")

    class _ListBody:
        def json(self):
            return [1, 2, 3]

    class _Ok:
        def json(self):
            return {"id": "1"}

    assert safe_json(None) == {}
    assert safe_json(_Bad()) == {}
    assert safe_json(_ListBody()) == {}
    assert safe_json(_Ok()) == {"id": "1"}


# ---------------------------------------------------------------------------
# Password protection (Smart Lockout threshold/duration + banned-password
# policy): a configured directory-setting object wins; otherwise the preset
# template defaults that are actually in effect are surfaced (and labelled).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_password_protection_reads_configured_object(monkeypatch):
    calls = _patch_request(
        monkeypatch,
        resp=_FakeResp(
            200,
            {
                "value": [
                    {"templateId": "some-other-template", "values": []},
                    {
                        "templateId": PASSWORD_RULE_TEMPLATE_ID,
                        "displayName": "Password Rule Settings",
                        "values": [
                            {"name": "LockoutThreshold", "value": "5"},
                            {"name": "LockoutDurationInSeconds", "value": "120"},
                            {"name": "EnableBannedPasswordCheck", "value": "True"},
                            {
                                "name": "EnableBannedPasswordCheckOnPremises",
                                "value": "False",
                            },
                            {
                                "name": "BannedPasswordCheckOnPremisesMode",
                                "value": "Audit",
                            },
                            {
                                "name": "BannedPasswordList",
                                "value": "password\tletmein",
                            },
                        ],
                    },
                ]
            },
        ),
    )
    handler = _handlers()["get_entra_password_protection"]
    result = await handler()

    # Only the tenant-wide settings collection is read (configured object found,
    # so no template-default fallback request is issued).
    assert calls[0]["path"] == "/groupSettings"
    assert len(calls) == 1
    assert "tenant-customized values" in result
    assert "threshold: 5" in result
    assert "120 seconds" in result
    assert "Cloud banned-password check: enabled" in result
    # The tab-delimited custom banned list is counted, not dumped verbatim.
    assert "Custom banned-password entries: 2" in result
    assert "letmein" not in result


@pytest.mark.asyncio
async def test_password_protection_falls_back_to_template_defaults(monkeypatch):
    calls: list[dict[str, Any]] = []

    async def _fake_request(self, method, path, action, **kwargs):
        calls.append({"method": method, "path": path, "action": action, **kwargs})
        if path == "/groupSettings":
            # No tenant-wide Password Rule Settings object has been created.
            return _FakeResp(200, {"value": []}), None
        if path == f"/groupSettingTemplates/{PASSWORD_RULE_TEMPLATE_ID}":
            return (
                _FakeResp(
                    200,
                    {
                        "values": [
                            {"name": "LockoutThreshold", "defaultValue": "10"},
                            {
                                "name": "LockoutDurationInSeconds",
                                "defaultValue": "60",
                            },
                            {
                                "name": "EnableBannedPasswordCheck",
                                "defaultValue": "True",
                            },
                            {
                                "name": "EnableBannedPasswordCheckOnPremises",
                                "defaultValue": "False",
                            },
                            {
                                "name": "BannedPasswordCheckOnPremisesMode",
                                "defaultValue": "Audit",
                            },
                            {"name": "BannedPasswordList", "defaultValue": ""},
                        ]
                    },
                ),
                None,
            )
        return _FakeResp(404), "Error: not found"

    monkeypatch.setattr(EntraClient, "request", _fake_request)
    handler = _handlers()["get_entra_password_protection"]
    result = await handler()

    assert calls[0]["path"] == "/groupSettings"
    assert calls[1]["path"] == f"/groupSettingTemplates/{PASSWORD_RULE_TEMPLATE_ID}"
    # The preset defaults are surfaced and clearly labelled as defaults, not
    # fabricated tenant values.
    assert "DEFAULTS" in result
    assert "threshold: 10" in result
    assert "60 seconds" in result
    assert "Custom banned-password entries: 0" in result


@pytest.mark.asyncio
async def test_password_protection_falls_back_to_beta_template(monkeypatch):
    # Some tenants only return the Password Rule Settings template from the beta
    # directorySettingTemplates surface; the tool must try beta after v1.0 404s.
    calls: list[dict[str, Any]] = []
    beta_url = (
        "https://graph.microsoft.com/beta/directorySettingTemplates/"
        f"{PASSWORD_RULE_TEMPLATE_ID}"
    )

    async def _fake_request(self, method, path, action, **kwargs):
        calls.append({"path": path})
        if path == "/groupSettings":
            return _FakeResp(200, {"value": []}), None
        if path == f"/groupSettingTemplates/{PASSWORD_RULE_TEMPLATE_ID}":
            return _FakeResp(404), "Error: not found"
        if path == beta_url:
            return (
                _FakeResp(
                    200,
                    {"values": [{"name": "LockoutThreshold", "defaultValue": "10"}]},
                ),
                None,
            )
        return _FakeResp(404), "Error: not found"

    monkeypatch.setattr(EntraClient, "request", _fake_request)
    handler = _handlers()["get_entra_password_protection"]
    result = await handler()

    assert [c["path"] for c in calls] == [
        "/groupSettings",
        f"/groupSettingTemplates/{PASSWORD_RULE_TEMPLATE_ID}",
        beta_url,
    ]
    assert "threshold: 10" in result


@pytest.mark.asyncio
async def test_password_protection_surfaces_settings_read_error(monkeypatch):
    _patch_request(monkeypatch, resp=None, err="Error: Entra access denied")
    handler = _handlers()["get_entra_password_protection"]
    result = await handler()
    assert "access denied" in result


@pytest.mark.asyncio
async def test_create_user_survives_non_json_success_body(monkeypatch):
    # A successful write whose body is not JSON must not raise out of the tool;
    # it degrades to a success message built from the inputs (id unknown).
    class _BadResp:
        content = b"<html>proxy login</html>"

        def json(self):
            raise ValueError("not json")

    async def _fake_request(self, method, path, action, **kwargs):
        return _BadResp(), None

    monkeypatch.setattr(EntraClient, "request", _fake_request)
    handler = _handlers()["create_entra_user"]
    result = await handler(
        display_name="Jane Doe",
        user_principal_name="jane@contoso.com",
        mail_nickname="jane",
        password="P@ssw0rd!",
    )
    assert "Created user" in result
    assert "jane@contoso.com" in result
