"""Microsoft Entra ID - sign-in and directory audit logs (the investigation trail).

Both surfaces require Entra ID P1/P2 + the ``AuditLog.Read.All`` permission.
Graph times out on unbounded log queries, so every call applies a date range
(defaulting to the last 7 days for sign-ins, 30 for directory audits). The
range is built on the timestamp property of the resource queried:
``createdDateTime`` on ``signIn``, ``activityDateTime`` on ``directoryAudit``
(Graph answers 400 "Could not find a property named 'createdDateTime' on type
'microsoft.graph.directoryAudit'" otherwise).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import EntraClient, dt, fmt_list, odata_quote, trunc

# Timestamp property of each log resource, used in the $filter date range.
_SIGNIN_DATE_FIELD = "createdDateTime"
_AUDIT_DATE_FIELD = "activityDateTime"


def _normalize_iso8601(value: str, field: str) -> str:
    """Validate *value* as an ISO 8601 timestamp and return a canonical form.

    ``start_date`` / ``end_date`` are tool inputs that get embedded *unquoted*
    into the OData ``$filter`` as date literals, so a malformed or crafted value
    could break the query or inject extra filter clauses. Parsing the value and
    re-emitting it as ``YYYY-MM-DDTHH:MM:SSZ`` both validates it and strips any
    injection (the returned string is reconstructed from parsed components).
    Raises :class:`ValueError` if *value* is not a valid ISO 8601 datetime.
    """
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


def _date_range_filter(
    start_date: str,
    end_date: str,
    default_days: int = 7,
    *,
    field: str = _SIGNIN_DATE_FIELD,
) -> str:
    """Build an OData range on *field*, defaulting to the last N days.

    *field* is the resource's timestamp property: ``createdDateTime`` for
    sign-ins, ``activityDateTime`` for directory audits.

    Raises :class:`ValueError` (via :func:`_normalize_iso8601`) if a supplied
    ``start_date`` / ``end_date`` is not a valid ISO 8601 timestamp.
    """
    if start_date:
        start_iso = _normalize_iso8601(start_date, "start_date")
    else:
        start = datetime.now(UTC) - timedelta(days=default_days)
        start_iso = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    clause = f"{field} ge {start_iso}"
    if end_date:
        clause += f" and {field} le {_normalize_iso8601(end_date, 'end_date')}"
    return clause


def _fmt_signin(s: dict[str, Any]) -> str:
    status = s.get("status", {}) or {}
    code = status.get("errorCode", 0)
    outcome = "success" if code == 0 else f"failure ({code})"
    reason = status.get("failureReason") or ""
    loc = s.get("location", {}) or {}
    city = loc.get("city") or loc.get("countryOrRegion") or "?"
    ca = s.get("conditionalAccessStatus", "?")
    return (
        f"- {dt(s.get('createdDateTime'))} | {s.get('userPrincipalName', '?')} -> "
        f"{s.get('appDisplayName', '?')}\n"
        f"  {outcome} | IP: {s.get('ipAddress', '?')} ({city}) | CA: {ca}"
        f"{f' | {trunc(reason, 120)}' if reason else ''}"
    )


def _target_name(t: dict[str, Any]) -> str:
    """A target resource's name: a user target carries its UPN, often no display name."""
    return t.get("displayName") or t.get("userPrincipalName") or t.get("id") or "?"


def _fmt_audit(a: dict[str, Any]) -> str:
    initiator = a.get("initiatedBy", {}) or {}
    actor = initiator.get("user", {}) or initiator.get("app", {}) or {}
    who = (
        actor.get("userPrincipalName")
        or actor.get("displayName")
        or "system"
    )
    targets = [t for t in (a.get("targetResources", []) or []) if isinstance(t, dict)]
    target = _target_name(targets[0]) if targets else "?"
    if len(targets) > 1:
        target += f" (+{len(targets) - 1} more)"
    return (
        f"- {dt(a.get('activityDateTime'))} | {a.get('activityDisplayName', '?')} "
        f"({a.get('result', '?')})\n"
        f"  by: {who} | target: {target} | category: {a.get('category', '?')}"
    )


def register(
    client: EntraClient, executor: CallableToolExecutor
) -> list[ToolDefinition]:
    src = "integration:entra"

    async def list_entra_signin_logs(
        user_principal_name: str = "",
        app_display_name: str = "",
        status: str = "",
        start_date: str = "",
        end_date: str = "",
        top: int = 25,
        skiptoken: str = "",
    ) -> str:
        try:
            filters = [
                _date_range_filter(start_date, end_date, field=_SIGNIN_DATE_FIELD)
            ]
        except ValueError as exc:
            return f"Error: {exc}"
        if user_principal_name:
            filters.append(
                f"userPrincipalName eq '{odata_quote(user_principal_name)}'"
            )
        if app_display_name:
            filters.append(f"appDisplayName eq '{odata_quote(app_display_name)}'")
        if status == "failure":
            filters.append("status/errorCode ne 0")
        elif status == "success":
            filters.append("status/errorCode eq 0")
        params: dict[str, Any] = {"$filter": " and ".join(filters)}
        if skiptoken:
            params["$skiptoken"] = skiptoken
        items, err, next_tok = await client.paged(
            "/auditLogs/signIns",
            "list sign-in logs",
            top=min(top, 100),
            params=params,
        )
        if err:
            return err
        return fmt_list(
            items,
            _fmt_signin,
            noun="sign-in",
            skiptoken=next_tok,
            empty_msg="No sign-ins match the filter (requires Entra ID P1/P2 + AuditLog.Read.All).",
        )

    async def list_entra_audit_logs(
        activity: str = "",
        initiated_by: str = "",
        start_date: str = "",
        end_date: str = "",
        top: int = 25,
        skiptoken: str = "",
    ) -> str:
        try:
            filters = [
                _date_range_filter(
                    start_date, end_date, default_days=30, field=_AUDIT_DATE_FIELD
                )
            ]
        except ValueError as exc:
            return f"Error: {exc}"
        if activity:
            filters.append(f"activityDisplayName eq '{odata_quote(activity)}'")
        if initiated_by:
            filters.append(
                f"initiatedBy/user/userPrincipalName eq '{odata_quote(initiated_by)}'"
            )
        params: dict[str, Any] = {"$filter": " and ".join(filters)}
        if skiptoken:
            params["$skiptoken"] = skiptoken
        items, err, next_tok = await client.paged(
            "/auditLogs/directoryAudits",
            "list directory audit logs",
            top=min(top, 100),
            params=params,
        )
        if err:
            return err
        return fmt_list(
            items,
            _fmt_audit,
            noun="audit event",
            skiptoken=next_tok,
            empty_msg="No directory audit events match the filter.",
        )

    executor.register("list_entra_signin_logs", list_entra_signin_logs)
    executor.register("list_entra_audit_logs", list_entra_audit_logs)

    return [
        ToolDefinition(
            name="list_entra_signin_logs",
            description=(
                "Query Entra ID sign-in logs for an identity investigation. Filter by "
                "user (UPN), application, and outcome (status='failure' surfaces "
                "failed sign-ins / brute-force or impossible-travel patterns). "
                "Defaults to the last 7 days; pass ISO start_date/end_date (e.g. "
                "2026-06-01T00:00:00Z) to widen or narrow. Returns time, user, app, "
                "outcome, IP, location, and conditional-access result. Requires Entra "
                "ID P1/P2 + AuditLog.Read.All."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "user_principal_name": {
                        "type": "string",
                        "description": "Filter to one user's sign-ins by UPN.",
                    },
                    "app_display_name": {
                        "type": "string",
                        "description": "Filter to one application by display name.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["success", "failure"],
                        "description": "Filter by sign-in outcome.",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "ISO 8601 start (default: 7 days ago), e.g. 2026-06-01T00:00:00Z.",
                    },
                    "end_date": {"type": "string", "description": "ISO 8601 end (optional)."},
                    "top": {"type": "integer", "default": 25},
                    "skiptoken": {"type": "string"},
                },
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="list_entra_audit_logs",
            description=(
                "Query the Entra ID directory audit log - the change history of the "
                "tenant (user/group/role/app changes, who did what). Filter by "
                "activity name or initiating user (UPN). Defaults to the last 30 "
                "days. Returns time, activity, result, actor, target, and category. "
                "Requires AuditLog.Read.All."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "activity": {
                        "type": "string",
                        "description": "Filter by exact activityDisplayName (e.g. 'Add member to role').",
                    },
                    "initiated_by": {
                        "type": "string",
                        "description": "Filter by the initiating user's UPN.",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "ISO 8601 start (default: 30 days ago).",
                    },
                    "end_date": {"type": "string", "description": "ISO 8601 end (optional)."},
                    "top": {"type": "integer", "default": 25},
                    "skiptoken": {"type": "string"},
                },
            },
            source=src,
            priority=0,
        ),
    ]
