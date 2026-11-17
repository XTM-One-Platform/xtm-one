"""Microsoft Purview Audit - search the unified audit log (the #1 compliance ask).

The Purview Audit Search Graph API (``/security/auditLog/queries``, currently
beta-only) is the asynchronous, supported replacement for the
``Search-UnifiedAuditLog`` PowerShell cmdlet. The flow is:

1. ``search_purview_audit_log`` creates a query (returns a query id).
2. ``get_purview_audit_query`` polls its status (notStarted -> running ->
   succeeded).
3. ``get_purview_audit_records`` reads the matched records once succeeded.

Searches span Exchange, SharePoint, OneDrive, Microsoft Entra, Teams, and more.
Requires ``AuditLogsQuery.Read.All`` (or a workload-scoped variant).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import PurviewClient, dt, fmt_list, next_skiptoken, safe_json, trunc

SRC = "integration:purview"
# The audit query API is only available under the Graph beta endpoint.
_BETA = "beta"
_QUERIES = "/security/auditLog/queries"


def _iso(value: str, field: str) -> str:
    """Validate an ISO 8601 timestamp and return a canonical ``...Z`` form."""
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(
            f"{field} must be an ISO 8601 timestamp like 2026-06-01T00:00:00Z."
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _csv(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _fmt_query(q: dict[str, Any]) -> str:
    return (
        f"- {q.get('displayName', 'audit query')} | status: {q.get('status', '?')}\n"
        f"  id: {q.get('id', '?')} | window: {dt(q.get('filterStartDateTime'))} -> "
        f"{dt(q.get('filterEndDateTime'))}"
    )


# auditData keys an audit consumer acts on (recipient classification, object
# identification, deduplication), surfaced on their own line for every record.
_AUDIT_DATA_KEYS = (
    "TargetUserOrGroupName",
    "TargetUserOrGroupType",
    "ObjectId",
    "SourceFileName",
    "SiteUrl",
    "ClientIP",
)
# EventData is free-form and can be large: it travels inside the bounded auditData.
_AUDIT_DATA_DEFAULT_CHARS = 600
_AUDIT_DATA_MAX_CHARS = 20000

# Graph's auditLogQuery records collection has no $orderby and no $filter, and
# returns records in an undefined order. A sorted or time-filtered read scans
# the query's pages server-side (bounded) and serves the sorted view by offset.
_ORDERS = {"newest_first": True, "oldest_first": False}
_SCAN_PAGE_SIZE = 150
_SCAN_MAX_RECORDS = 3000


def _record_time(r: dict[str, Any]) -> datetime | None:
    raw = r.get("createdDateTime")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _select_records(
    records: list[dict[str, Any]],
    *,
    order: str,
    after: datetime | None,
    before: datetime | None,
) -> list[dict[str, Any]]:
    """Filter by createdDateTime (after inclusive, before exclusive) then sort.

    A record without a readable time is kept only when no time filter is set,
    and sorts last whatever the order.
    """
    kept: list[tuple[datetime | None, dict[str, Any]]] = []
    for r in records:
        t = _record_time(r)
        if (after or before) and t is None:
            continue
        if after and t is not None and t < after:
            continue
        if before and t is not None and t >= before:
            continue
        kept.append((t, r))
    if order in _ORDERS:
        dated = [p for p in kept if p[0] is not None]
        undated = [p for p in kept if p[0] is None]
        dated.sort(key=lambda p: (p[0], str(p[1].get("id", ""))), reverse=_ORDERS[order])
        kept = dated + undated
    return [r for _, r in kept]


def _audit_data(r: dict[str, Any]) -> dict[str, Any]:
    data = r.get("auditData")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return {"raw": data}
    return data if isinstance(data, dict) else {}


def _fmt_record(r: dict[str, Any], data_chars: int) -> tuple[str, bool]:
    """One record: identity, actor, object, key auditData fields, bounded raw payload.

    Returns the text and whether the raw auditData was cut to ``data_chars``.
    """
    data = _audit_data(r)
    record_type = r.get("auditLogRecordType") or r.get("recordType") or "?"
    object_id = r.get("objectId") or data.get("ObjectId") or "-"
    lines = [
        f"- {dt(r.get('createdDateTime'))} | {r.get('operation', '?')} "
        f"({r.get('service', '?')})",
        f"  id: {r.get('id', '?')} | recordType: {record_type}",
        f"  user: {r.get('userPrincipalName') or r.get('userId') or '?'} | "
        f"ip: {r.get('clientIp') or data.get('ClientIP') or '-'} | object: {object_id}",
    ]
    fields = [
        f"{key}: {trunc(str(data[key]), 500)}"
        for key in _AUDIT_DATA_KEYS
        if key not in ("ObjectId", "ClientIP") and data.get(key) not in (None, "")
    ]
    if fields:
        lines.append("  " + " | ".join(fields))
    cut = False
    if data and data_chars > 0:
        raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str)
        if len(raw) > data_chars:
            cut = True
            raw = f"{raw[:data_chars]} [cut: {data_chars} of {len(raw)} chars]"
        lines.append(f"  auditData: {raw}")
    return "\n".join(lines), cut


def register(
    client: PurviewClient, executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def search_purview_audit_log(
        display_name: str = "",
        start_date: str = "",
        end_date: str = "",
        operations: str = "",
        services: str = "",
        keyword: str = "",
        user_principal_names: str = "",
        ip_addresses: str = "",
    ) -> str:
        try:
            start_iso = (
                _iso(start_date, "start_date")
                if start_date
                else (datetime.now(UTC) - timedelta(days=7)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
            )
            end_iso = (
                _iso(end_date, "end_date")
                if end_date
                else datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            )
        except ValueError as exc:
            return f"Error: {exc}"

        body: dict[str, Any] = {
            "displayName": display_name or f"XTM One audit search {start_iso}",
            "filterStartDateTime": start_iso,
            "filterEndDateTime": end_iso,
        }
        if operations:
            body["operationFilters"] = _csv(operations)
        if services:
            body["serviceFilter"] = services.strip()
        if keyword:
            body["keywordFilter"] = keyword.strip()
        if user_principal_names:
            body["userPrincipalNameFilters"] = _csv(user_principal_names)
        if ip_addresses:
            body["ipAddressFilters"] = _csv(ip_addresses)

        resp, err = await client.graph_request(
            "post", _QUERIES, "create audit log query", version=_BETA, json=body
        )
        if err:
            return err
        q = safe_json(resp)
        return (
            f"Started audit log search '{q.get('displayName')}' "
            f"(id: {q.get('id', '?')}, status: {q.get('status', 'notStarted')}). "
            f"Audit searches run asynchronously - poll get_purview_audit_query "
            f"with this id until status is 'succeeded', then read matches with "
            f"get_purview_audit_records."
        )

    async def get_purview_audit_query(query_id: str) -> str:
        data, err = await client.graph_get_json(
            f"{_QUERIES}/{query_id.strip()}", "get audit query", version=_BETA
        )
        if err:
            return err
        q = data or {}
        status = q.get("status", "?")
        hint = (
            " The search is complete - read matches with get_purview_audit_records."
            if status == "succeeded"
            else " Still running - poll again shortly."
            if status in ("notStarted", "running")
            else ""
        )
        return _fmt_query(q) + hint

    async def get_purview_audit_records(
        query_id: str,
        top: int = 50,
        skiptoken: str = "",
        audit_data_chars: int = _AUDIT_DATA_DEFAULT_CHARS,
        order: str = "",
        created_after: str = "",
        created_before: str = "",
        offset: int = 0,
    ) -> str:
        data_chars = max(0, min(int(audit_data_chars), _AUDIT_DATA_MAX_CHARS))
        order = (order or "").strip()
        if order and order not in _ORDERS:
            return "Error: order must be 'newest_first' or 'oldest_first'."
        if order or created_after or created_before:
            try:
                after = (
                    datetime.fromisoformat(
                        _iso(created_after, "created_after").replace("Z", "+00:00")
                    )
                    if created_after
                    else None
                )
                before = (
                    datetime.fromisoformat(
                        _iso(created_before, "created_before").replace("Z", "+00:00")
                    )
                    if created_before
                    else None
                )
            except ValueError as exc:
                return f"Error: {exc}"
            return await _sorted_records(
                query_id.strip(),
                top=max(1, min(int(top), _SCAN_PAGE_SIZE)),
                offset=max(0, int(offset)),
                order=order,
                after=after,
                before=before,
                data_chars=data_chars,
                skiptoken_ignored=bool(skiptoken),
            )
        params: dict[str, Any] = {"$top": min(top, 150)}
        if skiptoken:
            params["$skiptoken"] = skiptoken
        data, err = await client.graph_get_json(
            f"{_QUERIES}/{query_id.strip()}/records",
            "get audit records",
            version=_BETA,
            params=params,
        )
        if err:
            return err
        records = (data or {}).get("value", []) or []
        return _format_records(records, data_chars, skiptoken=next_skiptoken(data or {}))

    def _format_records(
        records: list[dict[str, Any]],
        data_chars: int,
        *,
        skiptoken: str = "",
        total: int | None = None,
        empty_msg: str = (
            "No audit records for this query yet. If the query status is not "
            "'succeeded', poll get_purview_audit_query first."
        ),
    ) -> str:
        cut_ids: list[str] = []

        def _fmt(r: dict[str, Any]) -> str:
            text, cut = _fmt_record(r, data_chars)
            if cut:
                cut_ids.append(str(r.get("id", "?")))
            return text

        out = fmt_list(
            records,
            _fmt,
            noun="audit record",
            skiptoken=skiptoken,
            total=total,
            empty_msg=empty_msg,
        )
        if cut_ids:
            out += (
                f"\nNote: auditData was cut to {data_chars} chars on "
                f"{len(cut_ids)} of {len(records)} record(s) (ids: "
                f"{trunc(', '.join(cut_ids), 400)}). The id, user, ip, object and "
                f"target fields above are complete; call again with a larger "
                f"audit_data_chars (max {_AUDIT_DATA_MAX_CHARS}) and a smaller top "
                f"for the full payload."
            )
        return out

    async def _sorted_records(
        query_id: str,
        *,
        top: int,
        offset: int,
        order: str,
        after: datetime | None,
        before: datetime | None,
        data_chars: int,
        skiptoken_ignored: bool,
    ) -> str:
        """Scan the query's pages (bounded), filter and sort, serve one window."""
        scanned: list[dict[str, Any]] = []
        token = ""
        complete = True
        while True:
            params: dict[str, Any] = {"$top": _SCAN_PAGE_SIZE}
            if token:
                params["$skiptoken"] = token
            data, err = await client.graph_get_json(
                f"{_QUERIES}/{query_id}/records",
                "get audit records",
                version=_BETA,
                params=params,
            )
            if err:
                return err
            scanned.extend((data or {}).get("value", []) or [])
            token = next_skiptoken(data or {})
            if not token:
                break
            if len(scanned) >= _SCAN_MAX_RECORDS:
                complete = False
                break
        selected = _select_records(scanned, order=order, after=after, before=before)
        window = selected[offset : offset + top]
        filters = []
        if after:
            filters.append(f"created_after {after.strftime('%Y-%m-%dT%H:%M:%SZ')}")
        if before:
            filters.append(f"created_before {before.strftime('%Y-%m-%dT%H:%M:%SZ')}")
        head = (
            f"Order: {order or 'as returned by Graph'} (createdDateTime)"
            f"{'; filter: ' + ', '.join(filters) if filters else ''}. "
            f"Scanned {len(scanned)} record(s) of the query "
            + (
                "(complete)."
                if complete
                else f"(INCOMPLETE: stopped at the {_SCAN_MAX_RECORDS}-record scan cap; "
                "narrow the window with created_after / created_before or a "
                "narrower search)."
            )
            + f" {len(selected)} match; showing {offset + 1 if window else 0}"
            f"-{offset + len(window)}."
        )
        if skiptoken_ignored:
            head += " skiptoken is not used with order or a time filter: page with offset."
        if offset + len(window) < len(selected):
            head += f" More remain: call again with offset={offset + len(window)}."
        else:
            head += " No more records."
        body = _format_records(
            window,
            data_chars,
            total=len(selected),
            empty_msg="No audit records in this window.",
        )
        return head + "\n" + body

    async def list_purview_audit_queries(top: int = 25, skiptoken: str = "") -> str:
        params: dict[str, Any] = {"$top": min(top, 100)}
        if skiptoken:
            params["$skiptoken"] = skiptoken
        data, err = await client.graph_get_json(
            _QUERIES, "list audit queries", version=_BETA, params=params
        )
        if err:
            return err
        return fmt_list(
            (data or {}).get("value", []) or [],
            _fmt_query,
            noun="audit query",
            skiptoken=next_skiptoken(data or {}),
            empty_msg="No audit log queries found.",
        )

    handlers = {
        "search_purview_audit_log": search_purview_audit_log,
        "get_purview_audit_query": get_purview_audit_query,
        "get_purview_audit_records": get_purview_audit_records,
        "list_purview_audit_queries": list_purview_audit_queries,
    }
    for name, fn in handlers.items():
        executor.register(name, fn)

    return [
        ToolDefinition(
            name="search_purview_audit_log",
            description=(
                "Start an asynchronous search of the Microsoft Purview unified "
                "audit log across Microsoft 365 (Exchange, SharePoint, OneDrive, "
                "Entra, Teams, etc.) - the supported replacement for "
                "Search-UnifiedAuditLog. Filter by date window (defaults to the "
                "last 7 days), operations (comma-separated, e.g. "
                "'FileDownloaded,UserLoggedIn'), service, keyword, user UPNs, and "
                "IP addresses. Returns a query id; the search runs in the "
                "background, so poll get_purview_audit_query until it succeeds, "
                "then read get_purview_audit_records."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "display_name": {"type": "string", "description": "Optional label for the search."},
                    "start_date": {
                        "type": "string",
                        "description": "ISO 8601 start (default: 7 days ago), e.g. 2026-06-01T00:00:00Z.",
                    },
                    "end_date": {"type": "string", "description": "ISO 8601 end (default: now)."},
                    "operations": {
                        "type": "string",
                        "description": "Comma-separated activity names, e.g. 'FileDownloaded,Add member to role'.",
                    },
                    "services": {
                        "type": "string",
                        "description": "Workload/service filter, e.g. 'Exchange' or 'SharePoint'.",
                    },
                    "keyword": {
                        "type": "string",
                        "description": "Free-text keyword over non-indexed audit properties.",
                    },
                    "user_principal_names": {
                        "type": "string",
                        "description": "Comma-separated UPNs to scope to specific users.",
                    },
                    "ip_addresses": {
                        "type": "string",
                        "description": "Comma-separated client IPs to filter on.",
                    },
                },
            },
            source=SRC,
            priority=0,
        ),
        ToolDefinition(
            name="get_purview_audit_query",
            description=(
                "Poll the status of an audit log search created by "
                "search_purview_audit_log. Returns the query's status "
                "(notStarted, running, succeeded, failed, cancelled) and window. "
                "When status is 'succeeded', read the matches with "
                "get_purview_audit_records."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query_id": {"type": "string", "description": "The audit query id."}
                },
                "required": ["query_id"],
            },
            source=SRC,
            priority=0,
        ),
        ToolDefinition(
            name="get_purview_audit_records",
            description=(
                "Read the audit records matched by a completed audit log search. "
                "Returns for every record its id (stable, for deduplication), "
                "time, operation, service, user, client IP, object, the target "
                "user or group (name, type), file and site, and the auditData "
                "payload bounded by audit_data_chars (a cut is announced). Run "
                "get_purview_audit_query first and ensure the query status is "
                "'succeeded'. Graph returns records in no defined order: pass "
                "order and/or created_after / created_before to scan the query "
                "(up to 3000 records), filter and sort by createdDateTime, then "
                "page the sorted view with offset; the result states the order, "
                "whether the scan was complete and the next offset."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query_id": {"type": "string"},
                    "top": {"type": "integer", "default": 50},
                    "skiptoken": {"type": "string"},
                    "audit_data_chars": {
                        "type": "integer",
                        "default": _AUDIT_DATA_DEFAULT_CHARS,
                        "description": "auditData chars per record (0 omits, max 20000).",
                    },
                    "order": {
                        "type": "string",
                        "enum": ["newest_first", "oldest_first"],
                        "description": "Sort by createdDateTime (default: Graph order).",
                    },
                    "created_after": {
                        "type": "string",
                        "description": "ISO 8601, keep records created at or after.",
                    },
                    "created_before": {
                        "type": "string",
                        "description": "ISO 8601, keep records created before.",
                    },
                    "offset": {
                        "type": "integer",
                        "default": 0,
                        "description": "Start of the window in the sorted view (with order or a time filter).",
                    },
                },
                "required": ["query_id"],
            },
            source=SRC,
            priority=0,
        ),
        ToolDefinition(
            name="list_purview_audit_queries",
            description=(
                "List recent Purview audit log search queries and their statuses, "
                "so you can resume reading the records of an earlier search instead "
                "of starting a new one."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "top": {"type": "integer", "default": 25},
                    "skiptoken": {"type": "string"},
                },
            },
            source=SRC,
            priority=0,
        ),
    ]
