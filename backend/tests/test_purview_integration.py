"""Tests for the Microsoft Purview integration provider.

Covers the tri-mode dual-audience client (app-only client credentials, delegated
token_acquirer, raw-Graph-token), the Data Map account-name resolution, the full
tool surface produced by ``build_tools`` (both token shapes), and the request
shape / behaviour of a representative slice of tools across every domain
(eDiscovery, audit log search, information protection, records management,
subject rights requests, and the Data Map catalog), including the delegated-only
guards and the no-account Data Map guard.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.integrations import oauth
from app.integrations.providers import purview
from app.integrations.providers.purview import PurviewClient, build_tools
from app.integrations.providers.purview._common import (
    map_base_url,
    next_skiptoken,
)
from app.tools.base import CallableToolExecutor

# tenant||client_id||client_secret||account triggers app-only mode.
_APP_TOKEN = "tenant-1||client-1||secret-1||acme"
# app-only without an account name (compliance only; Data Map disabled).
_APP_TOKEN_NO_ACCOUNT = "tenant-1||client-1||secret-1"
# A plain access token triggers graph_direct mode (OAuth-connect validation).
_GRAPH_TOKEN = "graph-access-token"


class _FakeResp:
    def __init__(self, status_code: int = 200, payload: Any = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.content = b"{}" if payload is not None else b""

    def json(self) -> Any:
        return self._payload


async def _fake_acquirer(scope: str) -> str:
    return f"token-for-{scope}"


def _handlers(
    token: str = _APP_TOKEN,
    *,
    delegated: bool = False,
    account_name: str = "",
) -> dict:
    executor = CallableToolExecutor()
    if delegated:
        build_tools(
            "__oauth__",
            executor,
            token_acquirer=_fake_acquirer,
            account_name=account_name or "acme",
        )
    else:
        build_tools(token, executor, account_name=account_name)
    return executor._handlers


# ---------------------------------------------------------------------------
# Auth-mode detection + account resolution
# ---------------------------------------------------------------------------


def test_client_detects_app_only_with_account():
    c = PurviewClient(_APP_TOKEN)
    assert c.mode == "app_only"
    assert c.app_only is True
    assert (c.tenant_id, c.client_id, c.client_secret) == (
        "tenant-1",
        "client-1",
        "secret-1",
    )
    assert c.account_name == "acme"
    assert c.has_account is True


def test_client_app_only_without_account_disables_datamap():
    c = PurviewClient(_APP_TOKEN_NO_ACCOUNT)
    assert c.app_only is True
    assert c.account_name == ""
    assert c.has_account is False


def test_client_delegated_acquirer_mode():
    c = PurviewClient("__oauth__", token_acquirer=_fake_acquirer, account_name="acme")
    assert c.mode == "delegated_acquirer"
    assert c.app_only is False
    assert c.account_name == "acme"


def test_client_graph_direct_mode():
    c = PurviewClient(_GRAPH_TOKEN)
    assert c.mode == "graph_direct"
    assert c.app_only is False
    assert c.graph_access_token == _GRAPH_TOKEN


def test_client_strips_whitespace_in_compound_segments():
    c = PurviewClient("  tenant-1\n||\tclient-1 ||secret-1\r\n|| acme ")
    assert (c.tenant_id, c.client_id, c.client_secret, c.account_name) == (
        "tenant-1",
        "client-1",
        "secret-1",
        "acme",
    )


def test_account_name_kwarg_overrides_token_field():
    # The builder injects account_name in both modes; an explicit kwarg wins.
    c = PurviewClient(_APP_TOKEN, account_name="override")
    assert c.account_name == "override"


def test_map_base_url_normalizes_account_forms():
    assert map_base_url("acme") == "https://acme.purview.azure.com"
    assert map_base_url("acme.purview.azure.com") == "https://acme.purview.azure.com"
    assert (
        map_base_url("https://acme.purview.azure.com/") == "https://acme.purview.azure.com"
    )
    assert map_base_url("") == ""


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------


def test_build_tools_full_surface_both_token_shapes():
    app_only = build_tools(_APP_TOKEN, CallableToolExecutor())
    delegated = build_tools(
        "__oauth__", CallableToolExecutor(), token_acquirer=_fake_acquirer
    )
    assert len(app_only) == len(delegated)
    assert len(app_only) >= 33
    names = {t.name for t in app_only}
    for expected in (
        # eDiscovery
        "list_purview_ediscovery_cases",
        "create_purview_ediscovery_case",
        "manage_purview_ediscovery_case",
        "list_purview_ediscovery_custodians",
        "manage_purview_ediscovery_search",
        "list_purview_ediscovery_holds",
        "list_purview_ediscovery_operations",
        # Audit
        "search_purview_audit_log",
        "get_purview_audit_query",
        "get_purview_audit_records",
        # Information protection
        "list_purview_sensitivity_labels",
        "get_purview_sensitivity_label",
        # Records management
        "list_purview_retention_labels",
        "create_purview_retention_label",
        "list_purview_retention_event_types",
        "manage_purview_retention_event",
        # Privacy
        "list_purview_subject_rights_requests",
        # Data Map
        "search_purview_catalog",
        "get_purview_asset",
        "get_purview_asset_lineage",
        "list_purview_glossaries",
        "list_purview_collections",
    ):
        assert expected in names


def test_build_tools_dummy_token_introspection_works():
    defs = build_tools("__dummy__", CallableToolExecutor(), refresh_fn=lambda: None)
    assert len(defs) >= 33
    assert all(t.source == "integration:purview" for t in defs)


def test_tool_priorities_sort_reads_before_writes_before_destructive():
    by_name = {t.name: t for t in build_tools(_APP_TOKEN, CallableToolExecutor())}
    assert by_name["list_purview_ediscovery_cases"].priority == 0
    assert by_name["search_purview_catalog"].priority == 0
    assert by_name["create_purview_ediscovery_case"].priority == 1
    assert by_name["manage_purview_ediscovery_case"].priority == 2
    assert by_name["manage_purview_retention_label"].priority == 2


def test_next_skiptoken_extracts_token_from_nextlink():
    data = {
        "@odata.nextLink": (
            "https://graph.microsoft.com/v1.0/security/cases/"
            "ediscoveryCases?$skiptoken=ABC123"
        )
    }
    assert next_skiptoken(data) == "ABC123"
    assert next_skiptoken({}) == ""


# ---------------------------------------------------------------------------
# Graph request shape (patch PurviewClient.graph_request)
# ---------------------------------------------------------------------------


def _patch_graph(monkeypatch, *, resp: _FakeResp | None = None, err: str | None = None):
    calls: list[dict[str, Any]] = []

    async def _fake(self, method, path, action, **kwargs):
        calls.append({"method": method, "path": path, "action": action, **kwargs})
        return (resp if resp is not None else _FakeResp()), err

    monkeypatch.setattr(PurviewClient, "graph_request", _fake)
    return calls


def _patch_map(monkeypatch, *, resp: _FakeResp | None = None, err: str | None = None):
    calls: list[dict[str, Any]] = []

    async def _fake(self, method, path, action, **kwargs):
        calls.append({"method": method, "path": path, "action": action, **kwargs})
        return (resp if resp is not None else _FakeResp()), err

    monkeypatch.setattr(PurviewClient, "map_request", _fake)
    return calls


@pytest.mark.asyncio
async def test_create_ediscovery_case_posts_display_name(monkeypatch):
    calls = _patch_graph(
        monkeypatch, resp=_FakeResp(201, {"id": "case-1", "displayName": "Matter A"})
    )
    handler = _handlers()["create_purview_ediscovery_case"]
    result = await handler(display_name="Matter A", external_id="EXT-9")

    assert calls[0]["method"] == "post"
    assert calls[0]["path"] == "/security/cases/ediscoveryCases"
    assert calls[0]["json"] == {"displayName": "Matter A", "externalId": "EXT-9"}
    assert "case-1" in result


@pytest.mark.asyncio
async def test_manage_ediscovery_case_close_posts_close(monkeypatch):
    calls = _patch_graph(monkeypatch)
    handler = _handlers()["manage_purview_ediscovery_case"]
    result = await handler(action="close", case_id="case-1")
    assert calls[0]["method"] == "post"
    assert calls[0]["path"] == "/security/cases/ediscoveryCases/case-1/close"
    assert "async" in result.lower()


@pytest.mark.asyncio
async def test_manage_ediscovery_case_unknown_action_rejected(monkeypatch):
    calls = _patch_graph(monkeypatch)
    handler = _handlers()["manage_purview_ediscovery_case"]
    result = await handler(action="bogus", case_id="case-1")
    assert calls == []
    assert "unknown action" in result.lower()


@pytest.mark.asyncio
async def test_manage_custodian_add_requires_email(monkeypatch):
    calls = _patch_graph(monkeypatch)
    handler = _handlers()["manage_purview_ediscovery_custodian"]
    result = await handler(action="add", case_id="case-1", email="")
    assert calls == []
    assert "email is required" in result


@pytest.mark.asyncio
async def test_manage_search_estimate_statistics_posts(monkeypatch):
    calls = _patch_graph(monkeypatch)
    handler = _handlers()["manage_purview_ediscovery_search"]
    result = await handler(
        action="estimate_statistics", case_id="case-1", search_id="s-1"
    )
    assert calls[0]["method"] == "post"
    assert calls[0]["path"].endswith("/searches/s-1/estimateStatistics")
    assert "async" in result.lower()


@pytest.mark.asyncio
async def test_list_ediscovery_cases_formats_and_paginates(monkeypatch):
    calls = _patch_graph(
        monkeypatch,
        resp=_FakeResp(
            200,
            {
                "value": [
                    {"id": "c1", "displayName": "Matter A", "status": "active"}
                ],
                "@odata.nextLink": (
                    "https://graph.microsoft.com/v1.0/security/cases/"
                    "ediscoveryCases?$skiptoken=NEXT"
                ),
            },
        ),
    )
    handler = _handlers()["list_purview_ediscovery_cases"]
    result = await handler()
    assert calls[0]["method"] == "get"
    assert calls[0]["path"] == "/security/cases/ediscoveryCases"
    assert "Matter A" in result
    assert "NEXT" in result


# ---------------------------------------------------------------------------
# Audit log search (async, beta endpoint + ISO date guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_audit_log_posts_to_beta_endpoint(monkeypatch):
    calls = _patch_graph(
        monkeypatch, resp=_FakeResp(201, {"id": "q-1", "displayName": "search"})
    )
    handler = _handlers()["search_purview_audit_log"]
    result = await handler(
        start_date="2026-06-01T00:00:00Z",
        end_date="2026-06-02T00:00:00Z",
        operations="FileDownloaded,UserLoggedIn",
    )
    assert calls[0]["method"] == "post"
    assert calls[0]["path"] == "/security/auditLog/queries"
    assert calls[0]["version"] == "beta"
    assert calls[0]["json"]["operationFilters"] == ["FileDownloaded", "UserLoggedIn"]
    assert calls[0]["json"]["filterStartDateTime"] == "2026-06-01T00:00:00Z"
    assert "q-1" in result


@pytest.mark.asyncio
async def test_search_audit_log_rejects_malformed_date(monkeypatch):
    calls = _patch_graph(monkeypatch)
    handler = _handlers()["search_purview_audit_log"]
    result = await handler(start_date="not-a-date")
    assert calls == []
    assert "ISO 8601" in result


@pytest.mark.asyncio
async def test_get_audit_records_hits_records_subpath(monkeypatch):
    calls = _patch_graph(monkeypatch, resp=_FakeResp(200, {"value": []}))
    handler = _handlers()["get_purview_audit_records"]
    await handler(query_id="q-1")
    assert calls[0]["path"] == "/security/auditLog/queries/q-1/records"
    assert calls[0]["version"] == "beta"


# Shape of a Graph beta auditLogRecord (security/auditLog/queries/{id}/records),
# a SharePoint sharing event with its auditData payload.
def _sharing_record(rid: str, target: str, extra: str = "") -> dict:
    return {
        "id": rid,
        "createdDateTime": "2026-11-05T13:40:12Z",
        "auditLogRecordType": "sharePointSharingOperation",
        "operation": "SharingInvitationCreated",
        "service": "SharePoint",
        "objectId": "https://contoso.sharepoint.com/sites/x/Shared Documents/a.docx",
        "userPrincipalName": "alice@contoso.com",
        "clientIp": "203.0.113.7",
        "auditData": {
            "Id": rid,
            "Operation": "SharingInvitationCreated",
            "TargetUserOrGroupName": target,
            "TargetUserOrGroupType": "Guest",
            "ObjectId": "https://contoso.sharepoint.com/sites/x/Shared Documents/a.docx",
            "SourceFileName": "a.docx",
            "SiteUrl": "https://contoso.sharepoint.com/sites/x/",
            "EventData": "<Type>Edit</Type>" + extra,
        },
    }


@pytest.mark.asyncio
async def test_get_audit_records_returns_id_target_object_site_on_every_record(
    monkeypatch,
):
    records = [
        _sharing_record(f"rec-{i}", f"user{i}@partner.example") for i in range(5)
    ]
    _patch_graph(
        monkeypatch,
        resp=_FakeResp(
            200,
            {
                "value": records,
                "@odata.nextLink": (
                    "https://graph.microsoft.com/beta/security/auditLog/queries/"
                    "q-1/records?$skiptoken=PAGE2"
                ),
            },
        ),
    )
    result = await _handlers()["get_purview_audit_records"](query_id="q-1")
    # Every record of the page, not only the first three.
    for i in range(5):
        assert f"id: rec-{i}" in result
        assert f"TargetUserOrGroupName: user{i}@partner.example" in result
    assert result.count("TargetUserOrGroupType: Guest") == 5
    assert result.count("SiteUrl: https://contoso.sharepoint.com/sites/x/") == 5
    assert result.count("SourceFileName: a.docx") == 5
    assert result.count("object: https://contoso.sharepoint.com/sites/x/") == 5
    assert result.count("auditData: {") == 5
    assert "ip: 203.0.113.7" in result
    assert "PAGE2" in result
    assert "[cut:" not in result and "was cut" not in result


@pytest.mark.asyncio
async def test_get_audit_records_announces_an_audit_data_cut(monkeypatch):
    big = _sharing_record("rec-big", "bob@partner.example", extra="x" * 2000)
    small = _sharing_record("rec-small", "carol@partner.example")
    _patch_graph(monkeypatch, resp=_FakeResp(200, {"value": [big, small]}))
    handler = _handlers()["get_purview_audit_records"]
    result = await handler(query_id="q-1", audit_data_chars=800)
    assert "[cut: 800 of " in result
    assert "auditData was cut to 800 chars on 1 of 2 record(s)" in result
    assert "rec-big" in result.split("Note:")[-1]
    # The key fields stay whole even when the raw payload is cut.
    assert "TargetUserOrGroupName: bob@partner.example" in result

    full = await handler(query_id="q-1", audit_data_chars=20000)
    assert "was cut" not in full
    none = await handler(query_id="q-1", audit_data_chars=0)
    assert "auditData:" not in none
    assert "id: rec-big" in none


@pytest.mark.asyncio
async def test_get_audit_records_parses_audit_data_given_as_json_string(monkeypatch):
    import json as _json

    rec = _sharing_record("rec-s", "dave@partner.example")
    rec["auditData"] = _json.dumps(rec["auditData"])
    _patch_graph(monkeypatch, resp=_FakeResp(200, {"value": [rec]}))
    result = await _handlers()["get_purview_audit_records"](query_id="q-1")
    assert "TargetUserOrGroupName: dave@partner.example" in result


def _timed_record(i: int) -> dict:
    # Minute i of a 6-hour window starting 2026-11-06T00:00:00Z.
    rec = _sharing_record(f"rec-{i:03d}", f"user{i}@partner.example")
    rec["createdDateTime"] = f"2026-11-06T{i // 60:02d}:{i % 60:02d}:00Z"
    return rec


def _patch_pages(monkeypatch, pages: list[list[dict]]):
    """Serve ``pages`` in order, chained by $skiptoken P<n>."""
    calls: list[dict[str, Any]] = []

    async def _fake(self, method, path, action, **kwargs):
        calls.append({"method": method, "path": path, **kwargs})
        token = (kwargs.get("params") or {}).get("$skiptoken", "P0")
        idx = int(token[1:])
        body: dict[str, Any] = {"value": pages[idx]}
        if idx + 1 < len(pages):
            body["@odata.nextLink"] = (
                "https://graph.microsoft.com/beta/security/auditLog/queries/"
                f"q-1/records?$skiptoken=P{idx + 1}"
            )
        return _FakeResp(200, body), None

    monkeypatch.setattr(PurviewClient, "graph_request", _fake)
    return calls


def _shuffled_pages(n: int, size: int) -> list[list[dict]]:
    # A deterministic shuffle: Graph returns records in no defined order.
    order = sorted(range(n), key=lambda i: (i * 7919) % n)
    recs = [_timed_record(i) for i in order]
    return [recs[k : k + size] for k in range(0, n, size)]


@pytest.mark.asyncio
async def test_get_audit_records_without_order_reads_one_page_unchanged(monkeypatch):
    calls = _patch_pages(monkeypatch, _shuffled_pages(360, 150))
    result = await _handlers()["get_purview_audit_records"](query_id="q-1", top=100)
    assert len(calls) == 1
    assert calls[0]["params"] == {"$top": 100}
    assert "Order:" not in result
    assert "P1" in result


@pytest.mark.asyncio
async def test_get_audit_records_newest_first_two_windows_are_the_200_newest(
    monkeypatch,
):
    calls = _patch_pages(monkeypatch, _shuffled_pages(360, 150))
    handler = _handlers()["get_purview_audit_records"]
    first = await handler(query_id="q-1", top=100, order="newest_first")
    assert len(calls) == 3  # every page of the query scanned
    assert "Order: newest_first (createdDateTime)" in first
    assert "Scanned 360 record(s) of the query (complete)." in first
    assert "360 match; showing 1-100." in first
    assert "More remain: call again with offset=100." in first
    ids = [line.split("id: ")[1].split(" ")[0] for line in first.splitlines() if "  id: " in line]
    assert ids[0] == "rec-359" and ids[-1] == "rec-260"
    assert ids == sorted(ids, reverse=True)

    second = await handler(query_id="q-1", top=100, order="newest_first", offset=100)
    ids2 = [line.split("id: ")[1].split(" ")[0] for line in second.splitlines() if "  id: " in line]
    assert ids2[0] == "rec-259" and ids2[-1] == "rec-160"
    assert "2026-11-06T05:59:00" in first and "2026-11-06T02:40:00" in second


@pytest.mark.asyncio
async def test_get_audit_records_time_filter_and_oldest_first(monkeypatch):
    _patch_pages(monkeypatch, _shuffled_pages(360, 150))
    result = await _handlers()["get_purview_audit_records"](
        query_id="q-1",
        order="oldest_first",
        created_after="2026-11-06T05:00:00Z",
        created_before="2026-11-06T05:10:00Z",
    )
    assert "filter: created_after 2026-11-06T05:00:00Z, created_before 2026-11-06T05:10:00Z" in result
    assert "10 match; showing 1-10." in result
    assert "No more records." in result
    ids = [line.split("id: ")[1].split(" ")[0] for line in result.splitlines() if "  id: " in line]
    assert ids == [f"rec-{i:03d}" for i in range(300, 310)]


@pytest.mark.asyncio
async def test_get_audit_records_scan_cap_is_announced(monkeypatch):
    from app.integrations.providers.purview import audit

    monkeypatch.setattr(audit, "_SCAN_MAX_RECORDS", 150)
    calls = _patch_pages(monkeypatch, _shuffled_pages(360, 150))
    result = await _handlers()["get_purview_audit_records"](
        query_id="q-1", order="newest_first", skiptoken="ignored"
    )
    assert len(calls) == 1
    assert "INCOMPLETE: stopped at the 150-record scan cap" in result
    assert "skiptoken is not used with order or a time filter" in result


@pytest.mark.asyncio
async def test_get_audit_records_rejects_bad_order_and_date(monkeypatch):
    calls = _patch_pages(monkeypatch, [[]])
    handler = _handlers()["get_purview_audit_records"]
    assert "order must be" in await handler(query_id="q-1", order="latest")
    assert "ISO 8601" in await handler(query_id="q-1", created_after="yesterday")
    assert calls == []


# ---------------------------------------------------------------------------
# Information protection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_sensitivity_labels_hits_dspm_path(monkeypatch):
    calls = _patch_graph(
        monkeypatch,
        resp=_FakeResp(200, {"value": [{"id": "l1", "name": "Confidential"}]}),
    )
    handler = _handlers()["list_purview_sensitivity_labels"]
    result = await handler()
    assert calls[0]["path"] == "/security/dataSecurityAndGovernance/sensitivityLabels"
    assert "Confidential" in result


# ---------------------------------------------------------------------------
# Records management + privacy: delegated-only guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retention_labels_blocked_in_app_only_mode(monkeypatch):
    calls = _patch_graph(monkeypatch)
    handler = _handlers(_APP_TOKEN)["list_purview_retention_labels"]
    result = await handler()
    # App-only short-circuits before any Graph call.
    assert calls == []
    assert "delegated" in result.lower()


@pytest.mark.asyncio
async def test_retention_labels_work_in_delegated_mode(monkeypatch):
    calls = _patch_graph(monkeypatch, resp=_FakeResp(200, {"value": []}))
    handler = _handlers(delegated=True)["list_purview_retention_labels"]
    await handler()
    assert calls[0]["path"] == "/security/labels/retentionLabels"


@pytest.mark.asyncio
async def test_create_retention_label_builds_duration(monkeypatch):
    calls = _patch_graph(
        monkeypatch, resp=_FakeResp(201, {"id": "r1", "displayName": "5yr"})
    )
    handler = _handlers(delegated=True)["create_purview_retention_label"]
    await handler(display_name="5yr", retention_days=1825)
    body = calls[0]["json"]
    assert body["displayName"] == "5yr"
    assert body["retentionDuration"]["days"] == 1825
    assert "retentionDurationInDays" in body["retentionDuration"]["@odata.type"]


@pytest.mark.asyncio
async def test_subject_rights_requests_blocked_in_app_only(monkeypatch):
    calls = _patch_graph(monkeypatch)
    handler = _handlers(_APP_TOKEN)["list_purview_subject_rights_requests"]
    result = await handler()
    assert calls == []
    assert "delegated" in result.lower()


@pytest.mark.asyncio
async def test_subject_rights_requests_list_in_delegated(monkeypatch):
    calls = _patch_graph(monkeypatch, resp=_FakeResp(200, {"value": []}))
    handler = _handlers(delegated=True)["list_purview_subject_rights_requests"]
    await handler(status="active")
    assert calls[0]["path"] == "/security/subjectRightsRequests"
    assert "status eq 'active'" in calls[0]["params"]["$filter"]


@pytest.mark.asyncio
async def test_subject_rights_requests_filter_escapes_single_quotes(monkeypatch):
    # A value with a single quote must be escaped (OData doubles it), never
    # interpolated raw, to prevent $filter injection.
    calls = _patch_graph(monkeypatch, resp=_FakeResp(200, {"value": []}))
    handler = _handlers(delegated=True)["list_purview_subject_rights_requests"]
    await handler(status="act've", type="exp'ort")
    assert calls[0]["params"]["$filter"] == "status eq 'act''ve' and type eq 'exp''ort'"


@pytest.mark.asyncio
async def test_ediscovery_cases_filter_escapes_single_quotes(monkeypatch):
    calls = _patch_graph(monkeypatch, resp=_FakeResp(200, {"value": []}))
    handler = _handlers()["list_purview_ediscovery_cases"]
    await handler(status="ope'n")
    assert calls[0]["params"]["$filter"] == "status eq 'ope''n'"


# ---------------------------------------------------------------------------
# Data Map (catalog) — search body, lineage formatting, glossary array,
# no-account guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_catalog_posts_query_body(monkeypatch):
    calls = _patch_map(
        monkeypatch,
        resp=_FakeResp(
            200,
            {
                "@search.count": 1,
                "value": [
                    {
                        "id": "g-1",
                        "name": "customers",
                        "entityType": "azure_sql_table",
                        "qualifiedName": "mssql://srv/db/customers",
                    }
                ],
                "continuationToken": "CT-2",
            },
        ),
    )
    handler = _handlers(account_name="acme")["search_purview_catalog"]
    result = await handler(keywords="customer", entity_type="azure_sql_table", limit=10)
    assert calls[0]["method"] == "post"
    assert calls[0]["path"] == "/datamap/api/search/query"
    assert calls[0]["json"]["keywords"] == "customer"
    assert calls[0]["json"]["filter"] == {"and": [{"entityType": "azure_sql_table"}]}
    assert calls[0]["json"]["limit"] == 10
    assert "customers" in result
    assert "CT-2" in result


@pytest.mark.asyncio
async def test_asset_lineage_formats_relations_by_name(monkeypatch):
    _patch_map(
        monkeypatch,
        resp=_FakeResp(
            200,
            {
                "baseEntityGuid": "g-mid",
                "relations": [
                    {"fromEntityId": "g-src", "toEntityId": "g-mid"},
                    {"fromEntityId": "g-mid", "toEntityId": "g-dst"},
                ],
                "guidEntityMap": {
                    "g-src": {"attributes": {"name": "raw_table"}},
                    "g-mid": {"attributes": {"name": "staging"}},
                    "g-dst": {"attributes": {"name": "mart"}},
                },
            },
        ),
    )
    handler = _handlers(account_name="acme")["get_purview_asset_lineage"]
    result = await handler(guid="g-mid")
    assert "raw_table -> staging" in result
    assert "staging -> mart" in result


@pytest.mark.asyncio
async def test_list_glossaries_handles_bare_array(monkeypatch):
    # The Atlas glossary endpoint returns a bare JSON array, not {value: []}.
    _patch_map(
        monkeypatch,
        resp=_FakeResp(
            200, [{"guid": "gl-1", "name": "Business Glossary", "terms": [{}, {}]}]
        ),
    )
    handler = _handlers(account_name="acme")["list_purview_glossaries"]
    result = await handler()
    assert "Business Glossary" in result
    assert "2 term" in result


@pytest.mark.asyncio
async def test_collections_use_account_api_version(monkeypatch):
    calls = _patch_map(monkeypatch, resp=_FakeResp(200, {"value": []}))
    handler = _handlers(account_name="acme")["list_purview_collections"]
    await handler()
    assert calls[0]["path"] == "/account/collections"
    assert calls[0]["api_version"] == "2019-11-01-preview"


@pytest.mark.asyncio
async def test_datamap_tools_error_without_account(monkeypatch):
    # No account configured: the real map_request short-circuits with an
    # actionable error before any token mint or network call.
    handler = _handlers(_APP_TOKEN_NO_ACCOUNT)["search_purview_catalog"]
    result = await handler(keywords="x")
    assert "no governance account name" in result.lower()


# ---------------------------------------------------------------------------
# Auth failure + connection validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_app_only_token_mint_failure_surfaces_auth_error(monkeypatch):
    async def _no_token(*args, **kwargs):
        return None

    monkeypatch.setattr(purview._common, "mint_app_token", _no_token)
    handler = _handlers(_APP_TOKEN)["list_purview_ediscovery_cases"]
    result = await handler()
    assert "authenticate with Microsoft Purview" in result


@pytest.mark.asyncio
async def test_validate_connection_true_on_success(monkeypatch):
    _patch_graph(monkeypatch, resp=_FakeResp(200, {"id": "me"}))
    integ = purview.PurviewIntegration()
    assert await integ.validate_connection(_GRAPH_TOKEN) is True


@pytest.mark.asyncio
async def test_validate_connection_false_on_error(monkeypatch):
    _patch_graph(monkeypatch, resp=None, err="Error: Purview access denied")
    integ = purview.PurviewIntegration()
    assert await integ.validate_connection(_GRAPH_TOKEN) is False


# ---------------------------------------------------------------------------
# OAuth /common -> tenant-specific endpoint rewrite (single-tenant apps)
#
# The Purview catalog ships its authorize/token URLs on /common. Single-tenant
# Entra app registrations reject /common with AADSTS50194, so purview must be in
# the set that rewrites /common/ to /{tenant_id}/ (the same gate that makes
# microsoft / intune / defender / sentinel / entra work).
# ---------------------------------------------------------------------------


def test_purview_is_in_common_endpoint_rewrite_set():
    assert "purview" in oauth._ENTRA_ID_PROVIDERS


async def test_resolve_tenant_urls_rewrites_common_for_purview(monkeypatch):
    monkeypatch.setattr(oauth, "reveal_json_field", AsyncMock(return_value={"tenant_id": "TENANT-XYZ"}))
    integ = SimpleNamespace(provider="purview", extra_config_enc=b"enc")
    authorize, token = await oauth._resolve_tenant_urls(
        integ,
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
    )
    assert "/TENANT-XYZ/" in authorize
    assert "/TENANT-XYZ/" in token
    assert "/common/" not in authorize
    assert "/common/" not in token


async def test_resolve_tenant_urls_noop_for_purview_without_tenant_id(monkeypatch):
    # No tenant_id configured -> leave /common/ untouched (multi-tenant apps).
    monkeypatch.setattr(oauth, "reveal_json_field", AsyncMock(return_value={}))
    integ = SimpleNamespace(provider="purview", extra_config_enc=b"enc")
    authorize, _ = await oauth._resolve_tenant_urls(
        integ,
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        None,
    )
    assert "/common/" in authorize
