# SPDX-License-Identifier: Apache-2.0

"""TheHive integration - Agentic security incident response (SIRP).

Full-fledged TheHive 5 (StrangeBee) integration, designed as a best-in-class
building block for an agentic SOC / incident-response team:

  - Cases (*the IR work unit*): search via the Query API (structured filters
    composed into ``_and`` clauses), get with comments + stats, create,
    update, close (status + impact + summary), merge
  - Alerts (*triage inbox*): search, get, create, update, promote to case
    (with case template), merge into an existing case
  - Observables (*IOC management*): list per case/alert, add (single or
    bulk values), update flags (ioc / sighted), delete
  - Tasks (*investigation checklist*): list per case or org-wide, create,
    update/complete, task logs (list + add) to document findings
  - Comments: list + add on cases and alerts
  - Cortex analyzers (*enrichment*): list available analyzers, run an
    analyzer on an observable, poll the job report
  - Reference data discovery: case/alert statuses, observable data types
  - Consolidated ``manage_thehive_item`` for the long tail (follow/unfollow
    alert, update/delete comment, delete task/observable/alert/case)

Killer agent use case: autonomous alert triage - the agent searches new
alerts, enriches observables with analyzers, promotes real incidents to
cases, creates investigation tasks, and documents findings in task logs.

Authentication
--------------
TheHive authenticates with a per-user **API key** sent as ``Authorization:
Bearer <key>``. ``integration_tools._get_token`` assembles the compound
``base_url||api_key`` token this builder parses (base URL = the TheHive
instance host - the API lives at ``{base_url}/api/v1`` on the same host as
the web UI). Compatible with TheHive 5 (primary target); most endpoints
(query API, cases, alerts, tasks, observables) also work on TheHive 4.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.http import (
    format_entity_list,
    format_pagination_hint,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

_PROVIDER = "TheHive"
_SOURCE = "integration:thehive"

# Upper bound on the number of items requested in a single paginated call.
_MAX_LIMIT = 100

# TheHive enum value maps - rendered in tool output so the agent never has to
# guess what a numeric ID means, and accepted as friendly inputs on writes.
_SEVERITIES = {1: "Low", 2: "Medium", 3: "High", 4: "Critical"}
_SEVERITY_INPUT = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
}
# TheHive 5 uses TLP 2.0: CLEAR(0) GREEN(1) AMBER(2) AMBER+STRICT(3) RED(4).
_TLP_LABELS = {0: "CLEAR", 1: "GREEN", 2: "AMBER", 3: "AMBER+STRICT", 4: "RED"}
_TLP_INPUT = {
    "clear": 0,
    "white": 0,
    "green": 1,
    "amber": 2,
    "amber+strict": 3,
    "amber_strict": 3,
    "red": 4,
    "0": 0,
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
}
_PAP_LABELS = {0: "CLEAR", 1: "GREEN", 2: "AMBER", 3: "RED"}
_PAP_INPUT = {
    "clear": 0,
    "white": 0,
    "green": 1,
    "amber": 2,
    "red": 3,
    "0": 0,
    "1": 1,
    "2": 2,
    "3": 3,
}
_TASK_STATUSES = frozenset({"Waiting", "InProgress", "Completed", "Cancel"})
# Close statuses are deliberately NOT validated against a fixed list:
# organisations can define custom closed statuses (list_thehive_statuses
# discovers them), so the server is the source of truth.
_IMPACT_STATUSES = frozenset({"WithImpact", "NoImpact", "NotApplicable"})
_MANAGE_ACTIONS = frozenset(
    {
        "follow_alert",
        "unfollow_alert",
        "update_comment",
        "delete_comment",
        "delete_task",
        "delete_observable",
        "delete_alert",
        "delete_case",
    }
)


def thehive_api_base(base_url: str) -> str:
    """Normalise the TheHive instance base URL (strip whitespace + trailing '/').

    TheHive serves the REST API at ``{base_url}/api/v1`` on the same host as
    the web UI. Returns ``""`` when no base URL is configured.
    """
    return (base_url or "").strip().rstrip("/")


def build_thehive_headers(api_key: str | None) -> dict[str, str]:
    """Build TheHive auth headers (``Authorization: Bearer <key>``).

    A blank/whitespace-only key omits the header entirely so the request falls
    through to a clean 401 instead of sending an illegal empty header value.
    """
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    key = (api_key or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


class TheHiveIntegration(BaseIntegration):
    provider_name = "thehive"
    display_name = "TheHive"
    icon = "ShieldAlert"
    description = (
        "Connect TheHive (StrangeBee security incident response platform) for "
        "case management, alert triage, observables, investigation tasks, and "
        "Cortex analyzer enrichment."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        base_raw, api_key = parse_compound_token(token, 2, greedy_last=True)
        base_url = thehive_api_base(base_raw)
        if not base_url or not (api_key or "").strip():
            return False
        async with egress_client(timeout=15) as client:
            resp, _err = await safe_request(
                client,
                "get",
                f"{base_url}/api/v1/user/current",
                _PROVIDER,
                "validate connection",
                headers=build_thehive_headers(api_key),
            )
            if resp is None:
                return False
            # A 2xx HTML login/SSO/proxy page must not count as connected -
            # /api/v1/user/current always returns a JSON object.
            data, jerr = _json_dict(resp)
            return jerr is None and bool(data)

    async def get_user_info(self, token: str) -> dict:
        base_raw, api_key = parse_compound_token(token, 2, greedy_last=True)
        base_url = thehive_api_base(base_raw)
        # Same guards as validate_connection: a misconfigured integration
        # (missing base_url yields a ``||key`` compound token) must surface a
        # clear configuration error, not a relative-URL request failure.
        if not base_url:
            return {
                "error": (
                    "TheHive base URL is not configured - set base_url in the "
                    "integration's extra config."
                )
            }
        if not (api_key or "").strip():
            return {"error": "TheHive API key is not configured."}
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/api/v1/user/current",
                _PROVIDER,
                "get user info",
                headers=build_thehive_headers(api_key),
            )
            if resp is None:
                return {"error": err}
            data, jerr = _json_dict(resp)
            if jerr:
                return {"error": jerr}
            return {
                "login": data.get("login"),
                "name": data.get("name"),
                "organisation": data.get("organisation"),
                "profile": data.get("profile"),
            }


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


# A 2xx response that is not JSON usually means base_url points at a login
# page, an SSO redirect target, or a misconfigured reverse proxy rather than
# the TheHive API itself - surface that instead of crashing the tool.
_NON_JSON_ERROR = (
    "Error: TheHive returned a response that is not valid JSON. Check that "
    "base_url points at the TheHive instance itself (the API lives under "
    "/api/v1 on the same host) and not at a login page or misconfigured proxy."
)


def _unexpected_shape_error(expected: str) -> str:
    """Build the error for a valid-JSON body of the wrong shape."""
    return (
        f"Error: TheHive returned an unexpected response shape (expected a "
        f"JSON {expected}). Verify that base_url points at the TheHive "
        "instance and that the API is not behind a rewriting proxy."
    )


def _parse_json(resp: httpx.Response) -> tuple[Any, str | None]:
    """Parse a response body once; returns ``(data, error)``."""
    try:
        return resp.json(), None
    except ValueError:
        return None, _NON_JSON_ERROR


def _json_dict(resp: httpx.Response) -> tuple[dict, str | None]:
    """Parse a response body as a JSON object; returns ``({}, error)`` otherwise.

    Valid JSON that is not an object (e.g. a list) is also an error - the
    endpoints parsed through this helper always return objects, so a
    different shape must not be masked as an empty success.
    """
    data, err = _parse_json(resp)
    if err:
        return {}, err
    if not isinstance(data, dict):
        return {}, _unexpected_shape_error("object")
    return data, None


def _sid(value: Any) -> str:
    """Coerce a tool argument to a trimmed string.

    Tool arguments arrive from the LLM without runtime type coercion, so an
    id declared as ``string`` in the schema may still show up as ``42`` (int).
    Every guard/normalisation goes through this helper instead of calling
    ``.strip()`` on the raw value.
    """
    if value is None:
        return ""
    return str(value).strip()


def _pid(value: Any) -> str:
    """Percent-encode a user-supplied id for use as a single URL path segment.

    TheHive ids (``~40964152``, case numbers, alert ``type;source;sourceRef``
    triples) never need a literal ``/`` - encoding everything (including
    slashes) keeps a crafted id such as ``../case/42`` from escaping its path
    position and reaching an unintended endpoint. Non-string JSON values are
    coerced via ``_sid``.
    """
    return quote(_sid(value), safe="")


def _trunc(text: Any, max_len: int = 300) -> str:
    if text is None:
        return ""
    text = str(text).replace("\r", " ").replace("\n", " ").strip()
    return text[:max_len] + ("..." if len(text) > max_len else "")


def _clamp(limit: Any, default: int = 25) -> int:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = default
    return max(1, min(limit, _MAX_LIMIT))


def _ms_to_str(ms: Any) -> str:
    """Convert an epoch-milliseconds timestamp to a readable UTC date string."""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return str(ms)


def _date_to_ms(date_str: str, *, end_of_day: bool = False) -> int | None:
    """Parse a ``YYYY-MM-DD`` date into epoch milliseconds (UTC).

    Returns midnight by default; with ``end_of_day=True`` returns the last
    millisecond of that date, so ``_lte`` bounds stay genuinely inclusive.
    """
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
    ms = int(dt.timestamp() * 1000)
    return ms + 86_400_000 - 1 if end_of_day else ms


def _split_csv(value: Any) -> list[str]:
    """Split a comma-separated string into a trimmed, non-empty list."""
    return [v.strip() for v in _sid(value).split(",") if v.strip()]


def _split_values(value: Any) -> list[str]:
    """Split observable data on newlines AND commas (bulk IOC input)."""
    parts: list[str] = []
    for line in _sid(value).splitlines():
        parts.extend(p.strip() for p in line.split(","))
    return [p for p in parts if p]


def _severity_label(sev: Any) -> str:
    try:
        return _SEVERITIES.get(int(sev), str(sev))
    except (TypeError, ValueError):
        return str(sev)


def _parse_severity(severity: str) -> int | None:
    """Map a friendly severity name/number to the TheHive integer, or None."""
    return _SEVERITY_INPUT.get((severity or "").strip().lower())


def _parse_tlp(tlp: str) -> int | None:
    return _TLP_INPUT.get((tlp or "").strip().lower())


def _parse_pap(pap: str) -> int | None:
    return _PAP_INPUT.get((pap or "").strip().lower())


def _fmt_case(case: dict, *, detail: bool = False) -> str:
    """Format a TheHive case dict readably."""
    number = case.get("number", "?")
    title = _trunc(case.get("title", "No title"), 160)
    status = case.get("status", "?")
    stage = case.get("stage", "?")
    severity = _severity_label(case.get("severity"))
    assignee = case.get("assignee") or "Unassigned"
    tlp = case.get("tlpLabel") or _TLP_LABELS.get(case.get("tlp", -1), "?")

    lines = [f"Case #{number}: {title}"]
    lines.append(
        f"  Status: {status} ({stage}) | Severity: {severity} | TLP: {tlp}"
        f" | Assignee: {assignee}"
    )
    lines.append(f"  ID: {case.get('_id', '?')}")
    tags = case.get("tags") or []
    if tags:
        lines.append(f"  Tags: {', '.join(str(t) for t in tags[:15])}")
    if case.get("flag"):
        lines.append("  Flagged")
    created = case.get("_createdAt") or case.get("startDate")
    if created:
        lines.append(f"  Created: {_ms_to_str(created)} by {case.get('_createdBy', '?')}")
    if detail:
        if case.get("impactStatus"):
            lines.append(f"  Impact: {case['impactStatus']}")
        desc = case.get("description", "")
        if desc:
            preview = desc[:1500] + ("..." if len(desc) > 1500 else "")
            lines.append(f"\n  Description:\n  {preview}")
        summary = case.get("summary", "")
        if summary:
            lines.append(f"\n  Summary: {_trunc(summary, 600)}")
        custom = case.get("customFields") or []
        if isinstance(custom, list) and custom:
            lines.append("\n  Custom Fields:")
            for cf in custom[:20]:
                if isinstance(cf, dict):
                    lines.append(f"    {cf.get('name', '?')}: {cf.get('value')}")
    return "\n".join(lines)


def _fmt_alert(alert: dict, *, detail: bool = False) -> str:
    """Format a TheHive alert dict readably."""
    title = _trunc(alert.get("title", "No title"), 160)
    status = alert.get("status", "?")
    stage = alert.get("stage", "?")
    severity = _severity_label(alert.get("severity"))
    src = f"{alert.get('type', '?')}/{alert.get('source', '?')}/{alert.get('sourceRef', '?')}"

    lines = [f"Alert: {title}"]
    lines.append(f"  Status: {status} ({stage}) | Severity: {severity} | Ref: {src}")
    lines.append(f"  ID: {alert.get('_id', '?')}")
    tags = alert.get("tags") or []
    if tags:
        lines.append(f"  Tags: {', '.join(str(t) for t in tags[:15])}")
    if alert.get("caseId"):
        lines.append(f"  Imported into case: {alert['caseId']}")
    obs_count = alert.get("observableCount")
    if obs_count is not None:
        lines.append(f"  Observables: {obs_count}")
    created = alert.get("_createdAt") or alert.get("date")
    if created:
        lines.append(f"  Created: {_ms_to_str(created)}")
    if detail:
        desc = alert.get("description", "")
        if desc:
            preview = desc[:1500] + ("..." if len(desc) > 1500 else "")
            lines.append(f"\n  Description:\n  {preview}")
        if alert.get("externalLink"):
            lines.append(f"  External link: {alert['externalLink']}")
        custom = alert.get("customFields") or []
        if isinstance(custom, list) and custom:
            lines.append("\n  Custom Fields:")
            for cf in custom[:20]:
                if isinstance(cf, dict):
                    lines.append(f"    {cf.get('name', '?')}: {cf.get('value')}")
    return "\n".join(lines)


def _fmt_observable(obs: dict) -> str:
    """Format a TheHive observable dict readably."""
    data_type = obs.get("dataType", "?")
    data = obs.get("data")
    if not data and isinstance(obs.get("attachment"), dict):
        data = f"file: {obs['attachment'].get('name', '?')}"
    flags = []
    if obs.get("ioc"):
        flags.append("IOC")
    if obs.get("sighted"):
        flags.append("sighted")
    flag_str = f" [{', '.join(flags)}]" if flags else ""
    lines = [f"{data_type}: {_trunc(data, 200)}{flag_str} (id: {obs.get('_id', '?')})"]
    tags = obs.get("tags") or []
    if tags:
        lines.append(f"  Tags: {', '.join(str(t) for t in tags[:10])}")
    if obs.get("message"):
        lines.append(f"  Note: {_trunc(obs['message'], 200)}")
    return "\n".join(lines)


def _fmt_task(task: dict) -> str:
    """Format a TheHive task dict readably."""
    title = _trunc(task.get("title", "No title"), 120)
    status = task.get("status", "?")
    group = task.get("group", "")
    assignee = task.get("assignee") or "Unassigned"
    lines = [f"Task: {title} (id: {task.get('_id', '?')})"]
    meta = f"  Status: {status} | Assignee: {assignee}"
    if group:
        meta += f" | Group: {group}"
    lines.append(meta)
    if task.get("dueDate"):
        lines.append(f"  Due: {_ms_to_str(task['dueDate'])}")
    if task.get("flag"):
        lines.append("  Flagged")
    return "\n".join(lines)


def _fmt_comment(comment: dict) -> str:
    author = comment.get("createdBy") or comment.get("_createdBy", "?")
    created = comment.get("createdAt") or comment.get("_createdAt")
    when = _ms_to_str(created) if created else "?"
    return (
        f"--- {author} ({when}, id: {comment.get('_id', '?')}) ---\n"
        f"{_trunc(comment.get('message', ''), 600)}"
    )


def _fmt_log(log: dict) -> str:
    author = log.get("owner") or log.get("_createdBy", "?")
    created = log.get("date") or log.get("_createdAt")
    when = _ms_to_str(created) if created else "?"
    return (
        f"--- {author} ({when}, id: {log.get('_id', '?')}) ---\n"
        f"{_trunc(log.get('message', ''), 600)}"
    )


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    base_raw, api_key = parse_compound_token(token, 2, greedy_last=True)
    base_url = thehive_api_base(base_raw)

    if not base_url or not (api_key or "").strip():
        return []  # Misconfigured - no API calls possible

    api = f"{base_url}/api/v1"
    headers = build_thehive_headers(api_key)

    async def _query(
        client: httpx.AsyncClient,
        operations: list[dict[str, Any]],
        action: str,
        *,
        exclude_fields: list[str] | None = None,
    ) -> tuple[list[Any], str | None]:
        """Run a TheHive Query API request; returns (items, error).

        ``/api/v1/query`` always answers with a JSON array, so any other
        shape (e.g. a JSON object from a rewriting proxy) is surfaced as an
        error instead of being silently treated as "no results".
        """
        body: dict[str, Any] = {"query": operations}
        if exclude_fields:
            body["excludeFields"] = exclude_fields
        resp, err = await safe_request(
            client,
            "post",
            f"{api}/query",
            _PROVIDER,
            action,
            headers=headers,
            json=body,
        )
        if err:
            return [], err
        data, jerr = _parse_json(resp)
        if jerr:
            return [], jerr
        if not isinstance(data, list):
            return [], _unexpected_shape_error("array")
        return data, None

    def _page_op(limit: int, skip: int) -> dict[str, Any]:
        return {"_name": "page", "from": skip, "to": skip + limit}

    def _sort_desc(field: str = "_createdAt") -> dict[str, Any]:
        return {"_name": "sort", "_fields": [{field: "desc"}]}

    def _case_link(case: dict) -> str:
        case_id = case.get("_id")
        return f"\n\nCase link: {base_url}/cases/{case_id}/details" if case_id else ""

    def _hint(count: int, limit: int, skip: int) -> str:
        return format_pagination_hint(
            str(skip + count) if count >= limit else None, param_name="skip"
        )

    # ══════════════════════════════════════════════════════════════════════
    # 0. CURRENT USER
    # ══════════════════════════════════════════════════════════════════════

    async def get_thehive_myself() -> str:
        """Get the authenticated TheHive user profile (login, org, profile).

        Useful to understand which account and organisation the integration is
        connected as, and to know the login usable as an assignee value.
        """
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/user/current",
                _PROVIDER,
                "get current user profile",
                headers=headers,
            )
            if err:
                return err
            me, jerr = _json_dict(resp)
            if jerr:
                return jerr
            lines = [
                f"Authenticated as: {me.get('name', me.get('login', '?'))}",
                f"  Login: {me.get('login', '?')}",
                f"  Organisation: {me.get('organisation', '?')}",
                f"  Profile: {me.get('profile', '?')}",
            ]
            perms = me.get("permissions") or []
            if perms:
                lines.append(f"  Permissions: {', '.join(str(p) for p in perms[:15])}")
            lines.append(
                "\nUse the login value as 'assignee' when assigning cases or tasks."
            )
            return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 1. CASES
    # ══════════════════════════════════════════════════════════════════════

    def _build_case_filters(
        keyword: str,
        status: str,
        stage: str,
        severity: str,
        assignee: str,
        tags: str,
        flagged_only: bool,
        created_after: str,
        created_before: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Compose Query API filter clauses; returns (clauses, error)."""
        clauses: list[dict[str, Any]] = []
        if keyword:
            clauses.append(
                {
                    "_or": [
                        {"_like": {"_field": "title", "_value": keyword}},
                        {"_like": {"_field": "description", "_value": keyword}},
                    ]
                }
            )
        if status:
            clauses.append({"_eq": {"_field": "status", "_value": status}})
        if stage:
            clauses.append({"_eq": {"_field": "stage", "_value": stage}})
        if severity:
            sev = _parse_severity(severity)
            if sev is None:
                return [], (
                    "Error: severity must be one of low, medium, high, critical "
                    "(or 1-4)."
                )
            clauses.append({"_eq": {"_field": "severity", "_value": sev}})
        if assignee:
            clauses.append({"_eq": {"_field": "assignee", "_value": assignee}})
        for tag in _split_csv(tags):
            clauses.append({"_eq": {"_field": "tags", "_value": tag}})
        if flagged_only:
            clauses.append({"_eq": {"_field": "flag", "_value": True}})
        for label, date_str, op in (
            ("created_after", created_after, "_gte"),
            ("created_before", created_before, "_lte"),
        ):
            if date_str:
                try:
                    ms = _date_to_ms(date_str, end_of_day=op == "_lte")
                except ValueError:
                    return [], f"Error: {label} must be in YYYY-MM-DD format, got '{date_str}'."
                clauses.append({op: {"_field": "_createdAt", "_value": ms}})
        return clauses, None

    async def search_thehive_cases(
        keyword: str = "",
        status: str = "",
        stage: str = "",
        severity: str = "",
        assignee: str = "",
        tags: str = "",
        flagged_only: bool = False,
        created_after: str = "",
        created_before: str = "",
        limit: int = 25,
        skip: int = 0,
    ) -> str:
        """Search TheHive cases with structured filters via the Query API.

        All filters are optional and combined with AND. stage is one of
        New / InProgress / Closed; severity accepts low/medium/high/critical.
        Use skip for pagination (offset-based).
        """
        clauses, ferr = _build_case_filters(
            keyword,
            status,
            stage,
            severity,
            assignee,
            tags,
            flagged_only,
            created_after,
            created_before,
        )
        if ferr:
            return ferr
        limit = _clamp(limit)
        operations: list[dict[str, Any]] = [{"_name": "listCase"}]
        if clauses:
            filter_op: dict[str, Any] = {"_name": "filter"}
            if len(clauses) == 1:
                filter_op.update(clauses[0])
            else:
                filter_op["_and"] = clauses
            operations.append(filter_op)
        operations.append(_sort_desc())
        operations.append(_page_op(limit, skip))
        async with egress_client(timeout=30) as client:
            data, err = await _query(
                client,
                operations,
                "search cases",
                exclude_fields=["description", "summary"],
            )
            if err:
                return err
            cases = data
            result = format_entity_list(cases, _fmt_case, noun="case")
            return result + _hint(len(cases), limit, skip)

    async def get_thehive_case(case_id: str) -> str:
        """Get full details of a TheHive case, including recent comments.

        case_id accepts the case number (e.g. '42' or '#42') or the internal
        _id (e.g. '~40964152'). Returns description, custom fields, stats,
        comments, and a deep link.
        """
        case_ref = _sid(case_id).lstrip("#")
        if not case_ref:
            return "Error: case_id is required."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/case/{_pid(case_ref)}",
                _PROVIDER,
                f"get case {case_ref}",
                headers=headers,
            )
            if err:
                return err
            case, jerr = _json_dict(resp)
            if jerr:
                return jerr
            result = _fmt_case(case, detail=True)

            # Fetch recent comments (non-critical - ignore failures)
            comments, cerr = await _query(
                client,
                [
                    {"_name": "getCase", "idOrName": case_ref},
                    {"_name": "comments"},
                    _sort_desc("_createdAt"),
                    _page_op(10, 0),
                ],
                f"get comments for case {case_ref}",
            )
            if not cerr and isinstance(comments, list) and comments:
                result += f"\n\nRecent comments ({len(comments)}):"
                for c in comments:
                    result += f"\n\n{_fmt_comment(c)}"
            return result + _case_link(case)

    async def create_thehive_case(
        title: str,
        description: str,
        severity: str = "medium",
        tags: str = "",
        tlp: str = "",
        pap: str = "",
        assignee: str = "",
        case_template: str = "",
        flag: bool = False,
    ) -> str:
        """Create a new TheHive case.

        severity: low/medium/high/critical. tlp: clear/green/amber/
        amber+strict/red. A case template (name or id) can seed tasks and
        custom fields.
        """
        sev = _parse_severity(severity or "medium")
        if sev is None:
            return "Error: severity must be one of low, medium, high, critical (or 1-4)."
        payload: dict[str, Any] = {
            "title": title,
            "description": description,
            "severity": sev,
        }
        if tags:
            payload["tags"] = _split_csv(tags)
        if tlp:
            tlp_val = _parse_tlp(tlp)
            if tlp_val is None:
                return "Error: tlp must be one of clear, green, amber, amber+strict, red."
            payload["tlp"] = tlp_val
        if pap:
            pap_val = _parse_pap(pap)
            if pap_val is None:
                return "Error: pap must be one of clear, green, amber, red."
            payload["pap"] = pap_val
        if assignee:
            payload["assignee"] = assignee
        if case_template:
            payload["caseTemplate"] = case_template
        if flag:
            payload["flag"] = True

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/case",
                _PROVIDER,
                "create case",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            case, jerr = _json_dict(resp)
            if jerr:
                return jerr
            return (
                f"Case created: #{case.get('number', '?')} - "
                f"{case.get('title', title)} (id: {case.get('_id', '?')})"
                + _case_link(case)
            )

    async def update_thehive_case(
        case_id: str,
        title: str = "",
        description: str = "",
        severity: str = "",
        status: str = "",
        assignee: str = "",
        summary: str = "",
        tlp: str = "",
        pap: str = "",
        add_tags: str = "",
        remove_tags: str = "",
    ) -> str:
        """Update fields on an existing TheHive case. Only provided fields change.

        Use status to transition the case (list_thehive_statuses shows the
        org's statuses). To close a case with a resolution, prefer
        close_thehive_case.
        """
        case_ref = _sid(case_id).lstrip("#")
        if not case_ref:
            return "Error: case_id is required."
        payload: dict[str, Any] = {}
        if title:
            payload["title"] = title
        if description:
            payload["description"] = description
        if severity:
            sev = _parse_severity(severity)
            if sev is None:
                return "Error: severity must be one of low, medium, high, critical (or 1-4)."
            payload["severity"] = sev
        if status:
            payload["status"] = status
        if assignee:
            payload["assignee"] = assignee
        if summary:
            payload["summary"] = summary
        if tlp:
            tlp_val = _parse_tlp(tlp)
            if tlp_val is None:
                return "Error: tlp must be one of clear, green, amber, amber+strict, red."
            payload["tlp"] = tlp_val
        if pap:
            pap_val = _parse_pap(pap)
            if pap_val is None:
                return "Error: pap must be one of clear, green, amber, red."
            payload["pap"] = pap_val
        if add_tags:
            payload["addTags"] = _split_csv(add_tags)
        if remove_tags:
            payload["removeTags"] = _split_csv(remove_tags)
        if not payload:
            return "No fields to update."

        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "patch",
                f"{api}/case/{_pid(case_ref)}",
                _PROVIDER,
                f"update case {case_ref}",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            return f"Case {case_ref} updated successfully."

    async def close_thehive_case(
        case_id: str,
        summary: str,
        status: str = "TruePositive",
        impact_status: str = "",
    ) -> str:
        """Close a TheHive case with a resolution status and summary.

        status: TruePositive, FalsePositive, Indeterminate, or Duplicated by
        default (organisations may define custom closed statuses -
        list_thehive_statuses shows them). impact_status: WithImpact,
        NoImpact, or NotApplicable.
        """
        case_ref = _sid(case_id).lstrip("#")
        if not case_ref:
            return "Error: case_id is required."
        if not (summary or "").strip():
            return "Error: summary is required to close a case - describe the resolution."
        payload: dict[str, Any] = {
            "status": status or "TruePositive",
            "summary": summary,
        }
        if impact_status:
            if impact_status not in _IMPACT_STATUSES:
                return (
                    "Error: impact_status must be one of "
                    f"{', '.join(sorted(_IMPACT_STATUSES))}."
                )
            payload["impactStatus"] = impact_status

        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "patch",
                f"{api}/case/{_pid(case_ref)}",
                _PROVIDER,
                f"close case {case_ref}",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            return f"Case {case_ref} closed as {payload['status']}."

    async def merge_thehive_cases(case_ids: str) -> str:
        """Merge two or more TheHive cases into a single case.

        case_ids: comma-separated case ids or numbers (at least 2). The merged
        case combines observables, tasks, and descriptions.
        """
        ids = [c.lstrip("#") for c in _split_csv(case_ids)]
        if len(ids) < 2:
            return "Error: provide at least two case ids to merge (comma-separated)."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/case/_merge/{','.join(_pid(i) for i in ids)}",
                _PROVIDER,
                f"merge cases {case_ids}",
                headers=headers,
            )
            if err:
                return err
            case, jerr = _json_dict(resp)
            if jerr:
                return jerr
            return (
                f"Cases merged into case #{case.get('number', '?')} "
                f"(id: {case.get('_id', '?')})" + _case_link(case)
            )

    # ══════════════════════════════════════════════════════════════════════
    # 2. ALERTS
    # ══════════════════════════════════════════════════════════════════════

    async def search_thehive_alerts(
        keyword: str = "",
        alert_type: str = "",
        source: str = "",
        status: str = "",
        stage: str = "",
        severity: str = "",
        tags: str = "",
        created_after: str = "",
        created_before: str = "",
        limit: int = 25,
        skip: int = 0,
    ) -> str:
        """Search TheHive alerts with structured filters via the Query API.

        The triage entry point: filter by status (e.g. 'New'), stage,
        severity, type, source, tags, or creation date. All filters combined
        with AND. Use skip for pagination (offset-based).
        """
        clauses: list[dict[str, Any]] = []
        if keyword:
            clauses.append(
                {
                    "_or": [
                        {"_like": {"_field": "title", "_value": keyword}},
                        {"_like": {"_field": "description", "_value": keyword}},
                    ]
                }
            )
        if alert_type:
            clauses.append({"_eq": {"_field": "type", "_value": alert_type}})
        if source:
            clauses.append({"_eq": {"_field": "source", "_value": source}})
        if status:
            clauses.append({"_eq": {"_field": "status", "_value": status}})
        if stage:
            clauses.append({"_eq": {"_field": "stage", "_value": stage}})
        if severity:
            sev = _parse_severity(severity)
            if sev is None:
                return "Error: severity must be one of low, medium, high, critical (or 1-4)."
            clauses.append({"_eq": {"_field": "severity", "_value": sev}})
        for tag in _split_csv(tags):
            clauses.append({"_eq": {"_field": "tags", "_value": tag}})
        for label, date_str, op in (
            ("created_after", created_after, "_gte"),
            ("created_before", created_before, "_lte"),
        ):
            if date_str:
                try:
                    ms = _date_to_ms(date_str, end_of_day=op == "_lte")
                except ValueError:
                    return f"Error: {label} must be in YYYY-MM-DD format, got '{date_str}'."
                clauses.append({op: {"_field": "_createdAt", "_value": ms}})

        limit = _clamp(limit)
        operations: list[dict[str, Any]] = [{"_name": "listAlert"}]
        if clauses:
            filter_op: dict[str, Any] = {"_name": "filter"}
            if len(clauses) == 1:
                filter_op.update(clauses[0])
            else:
                filter_op["_and"] = clauses
            operations.append(filter_op)
        operations.append(_sort_desc())
        operations.append(_page_op(limit, skip))
        async with egress_client(timeout=30) as client:
            data, err = await _query(
                client,
                operations,
                "search alerts",
                exclude_fields=["description"],
            )
            if err:
                return err
            alerts = data
            result = format_entity_list(alerts, _fmt_alert, noun="alert")
            return result + _hint(len(alerts), limit, skip)

    async def get_thehive_alert(alert_id: str) -> str:
        """Get full details of a TheHive alert, including its observables.

        alert_id is the alert _id (e.g. '~40964152') from search results.
        Returns description, custom fields, observables, and related case.
        """
        if not _sid(alert_id):
            return "Error: alert_id is required."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/alert/{_pid(alert_id)}",
                _PROVIDER,
                f"get alert {alert_id}",
                headers=headers,
            )
            if err:
                return err
            alert, jerr = _json_dict(resp)
            if jerr:
                return jerr
            result = _fmt_alert(alert, detail=True)

            # Fetch observables (non-critical - ignore failures)
            observables, oerr = await _query(
                client,
                [
                    {"_name": "getAlert", "idOrName": alert_id},
                    {"_name": "observables"},
                    _page_op(25, 0),
                ],
                f"get observables for alert {alert_id}",
            )
            if not oerr and isinstance(observables, list) and observables:
                result += f"\n\nObservables ({len(observables)}):"
                for obs in observables:
                    result += f"\n{_fmt_observable(obs)}"
            link = f"\n\nAlert link: {base_url}/alerts/{alert.get('_id', alert_id)}/details"
            return result + link

    async def create_thehive_alert(
        alert_type: str,
        source: str,
        source_ref: str,
        title: str,
        description: str,
        severity: str = "medium",
        tags: str = "",
        tlp: str = "",
        pap: str = "",
        external_link: str = "",
    ) -> str:
        """Create a new TheHive alert.

        alert_type/source/source_ref uniquely identify the alert (e.g.
        'siem'/'splunk'/'evt-123'). Add observables afterwards with
        add_thehive_observable (alert_id).
        """
        sev = _parse_severity(severity or "medium")
        if sev is None:
            return "Error: severity must be one of low, medium, high, critical (or 1-4)."
        payload: dict[str, Any] = {
            "type": alert_type,
            "source": source,
            "sourceRef": source_ref,
            "title": title,
            "description": description,
            "severity": sev,
        }
        if tags:
            payload["tags"] = _split_csv(tags)
        if tlp:
            tlp_val = _parse_tlp(tlp)
            if tlp_val is None:
                return "Error: tlp must be one of clear, green, amber, amber+strict, red."
            payload["tlp"] = tlp_val
        if pap:
            pap_val = _parse_pap(pap)
            if pap_val is None:
                return "Error: pap must be one of clear, green, amber, red."
            payload["pap"] = pap_val
        if external_link:
            payload["externalLink"] = external_link

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/alert",
                _PROVIDER,
                "create alert",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            alert, jerr = _json_dict(resp)
            if jerr:
                return jerr
            return (
                f"Alert created: {alert.get('title', title)} "
                f"(id: {alert.get('_id', '?')})"
            )

    async def update_thehive_alert(
        alert_id: str,
        title: str = "",
        description: str = "",
        severity: str = "",
        status: str = "",
        tags: str = "",
        tlp: str = "",
    ) -> str:
        """Update fields on an existing TheHive alert. Only provided fields change.

        Use status to transition the alert (e.g. 'InProgress', or a closed
        status like 'FalsePositive' - list_thehive_statuses shows the org's
        alert statuses). tags REPLACES the tag list when provided.
        """
        if not _sid(alert_id):
            return "Error: alert_id is required."
        payload: dict[str, Any] = {}
        if title:
            payload["title"] = title
        if description:
            payload["description"] = description
        if severity:
            sev = _parse_severity(severity)
            if sev is None:
                return "Error: severity must be one of low, medium, high, critical (or 1-4)."
            payload["severity"] = sev
        if status:
            payload["status"] = status
        if tags:
            payload["tags"] = _split_csv(tags)
        if tlp:
            tlp_val = _parse_tlp(tlp)
            if tlp_val is None:
                return "Error: tlp must be one of clear, green, amber, amber+strict, red."
            payload["tlp"] = tlp_val
        if not payload:
            return "No fields to update."

        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "patch",
                f"{api}/alert/{_pid(alert_id)}",
                _PROVIDER,
                f"update alert {alert_id}",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            return f"Alert {alert_id} updated successfully."

    async def promote_thehive_alert(
        alert_id: str,
        case_template: str = "",
        title: str = "",
        severity: str = "",
    ) -> str:
        """Promote a TheHive alert to a new case (alert triage escalation).

        Creates a case from the alert, importing its observables and
        description. A case template (name or id) can seed investigation
        tasks. To add the alert to an EXISTING case instead, use
        merge_thehive_alert_into_case.
        """
        if not _sid(alert_id):
            return "Error: alert_id is required."
        payload: dict[str, Any] = {}
        if case_template:
            payload["caseTemplate"] = case_template
        if title:
            payload["title"] = title
        if severity:
            sev = _parse_severity(severity)
            if sev is None:
                return "Error: severity must be one of low, medium, high, critical (or 1-4)."
            payload["severity"] = sev

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/alert/{_pid(alert_id)}/case",
                _PROVIDER,
                f"promote alert {alert_id} to case",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            case, jerr = _json_dict(resp)
            if jerr:
                return jerr
            return (
                f"Alert promoted to case #{case.get('number', '?')} - "
                f"{case.get('title', '?')} (id: {case.get('_id', '?')})"
                + _case_link(case)
            )

    async def merge_thehive_alert_into_case(alert_id: str, case_id: str) -> str:
        """Merge a TheHive alert into an EXISTING case.

        Imports the alert's observables and procedures into the case and marks
        the alert as imported. Use promote_thehive_alert to create a NEW case
        instead.
        """
        if not _sid(alert_id) or not _sid(case_id):
            return "Error: alert_id and case_id are both required."
        case_ref = _sid(case_id).lstrip("#")
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/alert/{_pid(alert_id)}/merge/{_pid(case_ref)}",
                _PROVIDER,
                f"merge alert {alert_id} into case {case_ref}",
                headers=headers,
            )
            if err:
                return err
            case, jerr = _json_dict(resp)
            if jerr:
                return jerr
            return (
                f"Alert merged into case #{case.get('number', case_ref)}"
                + _case_link(case)
            )

    # ══════════════════════════════════════════════════════════════════════
    # 3. OBSERVABLES
    # ══════════════════════════════════════════════════════════════════════

    async def list_thehive_observables(
        case_id: str = "",
        alert_id: str = "",
        data_type: str = "",
        ioc_only: bool = False,
        limit: int = 50,
        skip: int = 0,
    ) -> str:
        """List observables (IOCs/artifacts) of a TheHive case or alert.

        Provide either case_id (case number or _id) or alert_id. Optionally
        filter by data_type (e.g. 'ip', 'domain', 'hash', 'url' - see
        list_thehive_observable_types) or restrict to IOC-flagged entries.
        Use skip for pagination.
        """
        if bool(case_id) == bool(alert_id):
            return "Error: provide exactly one of case_id or alert_id."
        limit = _clamp(limit, default=50)
        if case_id:
            root = {"_name": "getCase", "idOrName": _sid(case_id).lstrip("#")}
            target_desc = f"case {case_id}"
        else:
            root = {"_name": "getAlert", "idOrName": _sid(alert_id)}
            target_desc = f"alert {alert_id}"
        operations: list[dict[str, Any]] = [root, {"_name": "observables"}]
        clauses: list[dict[str, Any]] = []
        if data_type:
            clauses.append({"_eq": {"_field": "dataType", "_value": data_type}})
        if ioc_only:
            clauses.append({"_eq": {"_field": "ioc", "_value": True}})
        if clauses:
            filter_op: dict[str, Any] = {"_name": "filter"}
            if len(clauses) == 1:
                filter_op.update(clauses[0])
            else:
                filter_op["_and"] = clauses
            operations.append(filter_op)
        operations.append(_page_op(limit, skip))
        async with egress_client(timeout=30) as client:
            data, err = await _query(
                client, operations, f"list observables for {target_desc}"
            )
            if err:
                return err
            observables = data
            result = format_entity_list(
                observables,
                _fmt_observable,
                noun="observable",
                empty_msg=f"No observables found for {target_desc}.",
            )
            return result + _hint(len(observables), limit, skip)

    async def add_thehive_observable(
        data: str,
        data_type: str,
        case_id: str = "",
        alert_id: str = "",
        message: str = "",
        ioc: bool = False,
        sighted: bool = False,
        tags: str = "",
        tlp: str = "",
    ) -> str:
        """Add one or more observables to a TheHive case or alert.

        Provide either case_id or alert_id. data accepts multiple values
        separated by commas or newlines (all created with the same
        data_type). data_type examples: 'ip', 'domain', 'url', 'hash',
        'mail', 'filename' (see list_thehive_observable_types).
        """
        if bool(case_id) == bool(alert_id):
            return "Error: provide exactly one of case_id or alert_id."
        values = _split_values(data)
        if not values:
            return "Error: data is required (one or more observable values)."
        if not _sid(data_type):
            return "Error: data_type is required (e.g. 'ip', 'domain', 'hash')."
        payload: dict[str, Any] = {
            "dataType": _sid(data_type),
            "data": values if len(values) > 1 else values[0],
        }
        if message:
            payload["message"] = message
        if ioc:
            payload["ioc"] = True
        if sighted:
            payload["sighted"] = True
        if tags:
            payload["tags"] = _split_csv(tags)
        if tlp:
            tlp_val = _parse_tlp(tlp)
            if tlp_val is None:
                return "Error: tlp must be one of clear, green, amber, amber+strict, red."
            payload["tlp"] = tlp_val
        if case_id:
            url = f"{api}/case/{_pid(_sid(case_id).lstrip('#'))}/observable"
            target_desc = f"case {case_id}"
        else:
            url = f"{api}/alert/{_pid(alert_id)}/observable"
            target_desc = f"alert {alert_id}"

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                url,
                _PROVIDER,
                f"add observable to {target_desc}",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            created, jerr = _parse_json(resp)
            if jerr:
                return jerr
            created_list = created if isinstance(created, list) else [created]
            ids = [
                o.get("_id", "?") for o in created_list if isinstance(o, dict)
            ]
            return (
                f"Added {len(values)} {data_type} observable"
                f"{'s' if len(values) != 1 else ''} to {target_desc}"
                f" (ids: {', '.join(ids) if ids else '?'})"
            )

    async def update_thehive_observable(
        observable_id: str,
        ioc: str = "",
        sighted: str = "",
        message: str = "",
        tags: str = "",
        tlp: str = "",
    ) -> str:
        """Update flags/metadata on a TheHive observable.

        ioc and sighted accept 'true' or 'false' (empty = unchanged) - use
        them to flag confirmed IOCs or record sightings during triage. tags
        REPLACES the tag list when provided.
        """
        if not _sid(observable_id):
            return "Error: observable_id is required."
        payload: dict[str, Any] = {}
        for name, value in (("ioc", ioc), ("sighted", sighted)):
            flag = (value or "").strip().lower()
            if flag:
                if flag not in ("true", "false"):
                    return f"Error: {name} must be 'true' or 'false'."
                payload[name] = flag == "true"
        if message:
            payload["message"] = message
        if tags:
            payload["tags"] = _split_csv(tags)
        if tlp:
            tlp_val = _parse_tlp(tlp)
            if tlp_val is None:
                return "Error: tlp must be one of clear, green, amber, amber+strict, red."
            payload["tlp"] = tlp_val
        if not payload:
            return "No fields to update."

        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "patch",
                f"{api}/observable/{_pid(observable_id)}",
                _PROVIDER,
                f"update observable {observable_id}",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            return f"Observable {observable_id} updated successfully."

    # ══════════════════════════════════════════════════════════════════════
    # 4. TASKS + TASK LOGS
    # ══════════════════════════════════════════════════════════════════════

    async def list_thehive_tasks(
        case_id: str = "",
        status: str = "",
        assignee: str = "",
        limit: int = 50,
        skip: int = 0,
    ) -> str:
        """List investigation tasks of a TheHive case (or org-wide).

        With case_id: tasks of that case. Without: all tasks visible in the
        organisation. Optionally filter by status (Waiting, InProgress,
        Completed, Cancel) or assignee login. Use skip for pagination.
        """
        limit = _clamp(limit, default=50)
        if case_id:
            operations: list[dict[str, Any]] = [
                {"_name": "getCase", "idOrName": _sid(case_id).lstrip("#")},
                {"_name": "tasks"},
            ]
            target_desc = f"case {case_id}"
        else:
            operations = [{"_name": "listTask"}]
            target_desc = "the organisation"
        clauses: list[dict[str, Any]] = []
        if status:
            clauses.append({"_eq": {"_field": "status", "_value": status}})
        if assignee:
            clauses.append({"_eq": {"_field": "assignee", "_value": assignee}})
        if clauses:
            filter_op: dict[str, Any] = {"_name": "filter"}
            if len(clauses) == 1:
                filter_op.update(clauses[0])
            else:
                filter_op["_and"] = clauses
            operations.append(filter_op)
        operations.append(_page_op(limit, skip))
        async with egress_client(timeout=30) as client:
            data, err = await _query(
                client, operations, f"list tasks for {target_desc}"
            )
            if err:
                return err
            tasks = data
            result = format_entity_list(
                tasks,
                _fmt_task,
                noun="task",
                empty_msg=f"No tasks found for {target_desc}.",
            )
            return result + _hint(len(tasks), limit, skip)

    async def create_thehive_task(
        case_id: str,
        title: str,
        description: str = "",
        group: str = "",
        assignee: str = "",
        due_date: str = "",
    ) -> str:
        """Create an investigation task in a TheHive case.

        group organises tasks into sections (e.g. 'identification',
        'containment', 'remediation'). due_date is YYYY-MM-DD.
        """
        case_ref = _sid(case_id).lstrip("#")
        if not case_ref:
            return "Error: case_id is required."
        payload: dict[str, Any] = {"title": title}
        if description:
            payload["description"] = description
        if group:
            payload["group"] = group
        if assignee:
            payload["assignee"] = assignee
        if due_date:
            try:
                payload["dueDate"] = _date_to_ms(due_date)
            except ValueError:
                return f"Error: due_date must be in YYYY-MM-DD format, got '{due_date}'."

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/case/{_pid(case_ref)}/task",
                _PROVIDER,
                f"create task in case {case_ref}",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            task, jerr = _json_dict(resp)
            if jerr:
                return jerr
            return (
                f"Task created in case {case_ref}: {task.get('title', title)} "
                f"(id: {task.get('_id', '?')})"
            )

    async def update_thehive_task(
        task_id: str,
        status: str = "",
        title: str = "",
        description: str = "",
        assignee: str = "",
        group: str = "",
        due_date: str = "",
    ) -> str:
        """Update a TheHive task. Set status='Completed' to complete it.

        status: Waiting, InProgress, Completed, or Cancel. Only provided
        fields change.
        """
        if not _sid(task_id):
            return "Error: task_id is required."
        payload: dict[str, Any] = {}
        if status:
            if status not in _TASK_STATUSES:
                return (
                    "Error: status must be one of "
                    f"{', '.join(sorted(_TASK_STATUSES))}."
                )
            payload["status"] = status
        if title:
            payload["title"] = title
        if description:
            payload["description"] = description
        if assignee:
            payload["assignee"] = assignee
        if group:
            payload["group"] = group
        if due_date:
            try:
                payload["dueDate"] = _date_to_ms(due_date)
            except ValueError:
                return f"Error: due_date must be in YYYY-MM-DD format, got '{due_date}'."
        if not payload:
            return "No fields to update."

        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "patch",
                f"{api}/task/{_pid(task_id)}",
                _PROVIDER,
                f"update task {task_id}",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            done = " Task completed." if payload.get("status") == "Completed" else ""
            return f"Task {task_id} updated successfully.{done}"

    async def list_thehive_task_logs(
        task_id: str, limit: int = 25, skip: int = 0
    ) -> str:
        """List the work logs of a TheHive task (analyst findings/progress).

        Use skip for pagination.
        """
        if not _sid(task_id):
            return "Error: task_id is required."
        limit = _clamp(limit)
        async with egress_client(timeout=30) as client:
            data, err = await _query(
                client,
                [
                    {"_name": "getTask", "idOrName": task_id},
                    {"_name": "logs"},
                    _sort_desc("_createdAt"),
                    _page_op(limit, skip),
                ],
                f"list logs for task {task_id}",
            )
            if err:
                return err
            logs = data
            result = format_entity_list(
                logs,
                _fmt_log,
                noun="task log",
                empty_msg=f"No logs found for task {task_id}.",
            )
            return result + _hint(len(logs), limit, skip)

    async def add_thehive_task_log(
        task_id: str, message: str, include_in_timeline: bool = False
    ) -> str:
        """Add a work log to a TheHive task to document findings or progress.

        message supports markdown. Set include_in_timeline=true to surface the
        log on the case timeline.
        """
        if not _sid(task_id):
            return "Error: task_id is required."
        if not (message or "").strip():
            return "Error: message is required."
        payload: dict[str, Any] = {"message": message}
        if include_in_timeline:
            payload["includeInTimeline"] = int(datetime.now(tz=UTC).timestamp() * 1000)

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/task/{_pid(task_id)}/log",
                _PROVIDER,
                f"add log to task {task_id}",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            log, jerr = _json_dict(resp)
            if jerr:
                return jerr
            return f"Log added to task {task_id} (id: {log.get('_id', '?')})"

    # ══════════════════════════════════════════════════════════════════════
    # 5. COMMENTS
    # ══════════════════════════════════════════════════════════════════════

    async def list_thehive_comments(
        case_id: str = "", alert_id: str = "", limit: int = 25, skip: int = 0
    ) -> str:
        """List comments on a TheHive case or alert (newest first).

        Provide either case_id or alert_id. Use skip for pagination.
        """
        if bool(case_id) == bool(alert_id):
            return "Error: provide exactly one of case_id or alert_id."
        limit = _clamp(limit)
        if case_id:
            root = {"_name": "getCase", "idOrName": _sid(case_id).lstrip("#")}
            target_desc = f"case {case_id}"
        else:
            root = {"_name": "getAlert", "idOrName": _sid(alert_id)}
            target_desc = f"alert {alert_id}"
        async with egress_client(timeout=30) as client:
            data, err = await _query(
                client,
                [
                    root,
                    {"_name": "comments"},
                    _sort_desc("_createdAt"),
                    _page_op(limit, skip),
                ],
                f"list comments for {target_desc}",
            )
            if err:
                return err
            comments = data
            result = format_entity_list(
                comments,
                _fmt_comment,
                noun="comment",
                empty_msg=f"No comments found for {target_desc}.",
            )
            return result + _hint(len(comments), limit, skip)

    async def add_thehive_comment(
        message: str, case_id: str = "", alert_id: str = ""
    ) -> str:
        """Add a comment to a TheHive case or alert.

        Provide either case_id or alert_id. message supports markdown.
        """
        if bool(case_id) == bool(alert_id):
            return "Error: provide exactly one of case_id or alert_id."
        if not (message or "").strip():
            return "Error: message is required."
        if case_id:
            url = f"{api}/case/{_pid(_sid(case_id).lstrip('#'))}/comment"
            target_desc = f"case {case_id}"
        else:
            url = f"{api}/alert/{_pid(alert_id)}/comment"
            target_desc = f"alert {alert_id}"
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                url,
                _PROVIDER,
                f"add comment to {target_desc}",
                headers=headers,
                json={"message": message},
            )
            if err:
                return err
            comment, jerr = _json_dict(resp)
            if jerr:
                return jerr
            return f"Comment added to {target_desc} (id: {comment.get('_id', '?')})"

    # ══════════════════════════════════════════════════════════════════════
    # 6. CORTEX ANALYZERS (enrichment)
    # ══════════════════════════════════════════════════════════════════════

    async def list_thehive_analyzers(data_type: str = "") -> str:
        """List Cortex analyzers available for enrichment in TheHive.

        Optionally filter by observable data_type (e.g. 'ip', 'domain',
        'hash') to see which analyzers can run on that type. Requires a
        Cortex connector configured in TheHive.
        """
        url = (
            f"{api}/connector/cortex/analyzer/type/{_pid(data_type)}"
            if _sid(data_type)
            else f"{api}/connector/cortex/analyzer"
        )
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                url,
                _PROVIDER,
                "list Cortex analyzers",
                headers=headers,
            )
            if err:
                return err
            analyzers, jerr = _parse_json(resp)
            if jerr:
                return jerr
            if not isinstance(analyzers, list) or not analyzers:
                return (
                    "No Cortex analyzers available. Check that a Cortex "
                    "connector is configured in TheHive."
                )
            lines = [f"Available analyzers ({len(analyzers)}):"]
            for a in analyzers:
                if not isinstance(a, dict):
                    continue
                types = ", ".join(a.get("dataTypeList") or [])
                cortex_ids = ", ".join(a.get("cortexIds") or [])
                lines.append(
                    f"  {a.get('name', '?')} v{a.get('version', '?')} "
                    f"(id: {a.get('id', '?')})"
                )
                if a.get("description"):
                    lines.append(f"    {_trunc(a['description'], 140)}")
                if types:
                    lines.append(f"    Data types: {types}")
                if cortex_ids:
                    lines.append(f"    Cortex server: {cortex_ids}")
            lines.append(
                "\nRun one with run_thehive_analyzer(analyzer_id, observable_id)."
            )
            return "\n".join(lines)

    async def run_thehive_analyzer(
        analyzer_id: str, observable_id: str, cortex_id: str = ""
    ) -> str:
        """Run a Cortex analyzer on a TheHive observable (enrichment).

        analyzer_id and observable_id come from list_thehive_analyzers and
        list_thehive_observables. cortex_id (the Cortex server name) is
        auto-resolved from the analyzer when omitted. The job runs
        asynchronously - poll it with get_thehive_analyzer_job.
        """
        if not _sid(analyzer_id) or not _sid(observable_id):
            return "Error: analyzer_id and observable_id are both required."
        async with egress_client(timeout=30) as client:
            resolved_cortex = _sid(cortex_id)
            if not resolved_cortex:
                # Auto-resolve the Cortex server from the analyzer definition.
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{api}/connector/cortex/analyzer/{_pid(analyzer_id)}",
                    _PROVIDER,
                    f"resolve Cortex server for analyzer {analyzer_id}",
                    headers=headers,
                )
                if err:
                    return err
                analyzer, jerr = _json_dict(resp)
                if jerr:
                    return jerr
                cortex_ids = analyzer.get("cortexIds") or []
                if not cortex_ids:
                    return (
                        f"Error: analyzer {analyzer_id} has no Cortex server - "
                        "pass cortex_id explicitly."
                    )
                resolved_cortex = str(cortex_ids[0])
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/connector/cortex/job",
                _PROVIDER,
                f"run analyzer {analyzer_id} on observable {observable_id}",
                headers=headers,
                json={
                    "analyzerId": analyzer_id,
                    "cortexId": resolved_cortex,
                    "artifactId": observable_id,
                },
            )
            if err:
                return err
            job, jerr = _json_dict(resp)
            if jerr:
                return jerr
            return (
                f"Analyzer job started: {job.get('analyzerName', analyzer_id)} "
                f"on observable {observable_id} (job id: {job.get('_id', '?')}, "
                f"status: {job.get('status', '?')}). "
                "Poll with get_thehive_analyzer_job."
            )

    async def get_thehive_analyzer_job(job_id: str) -> str:
        """Get the status and report of a Cortex analyzer job.

        job_id comes from run_thehive_analyzer. When the job succeeds, the
        report summary is included.
        """
        if not _sid(job_id):
            return "Error: job_id is required."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/connector/cortex/job/{_pid(job_id)}",
                _PROVIDER,
                f"get analyzer job {job_id}",
                headers=headers,
            )
            if err:
                return err
            job, jerr = _json_dict(resp)
            if jerr:
                return jerr
            lines = [
                f"Analyzer job {job.get('_id', job_id)}: "
                f"{job.get('analyzerName', '?')} - status: {job.get('status', '?')}"
            ]
            if job.get("startDate"):
                lines.append(f"  Started: {_ms_to_str(job['startDate'])}")
            if job.get("endDate"):
                lines.append(f"  Finished: {_ms_to_str(job['endDate'])}")
            report = job.get("report")
            if isinstance(report, dict):
                summary = report.get("summary")
                if summary:
                    lines.append(f"  Summary: {_trunc(summary, 800)}")
                full = report.get("full")
                if full is not None:
                    lines.append(f"  Report: {_trunc(full, 1500)}")
            return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 7. REFERENCE DATA DISCOVERY
    # ══════════════════════════════════════════════════════════════════════

    async def list_thehive_statuses(target: str = "case") -> str:
        """List the case or alert statuses configured in the organisation.

        target: 'case' or 'alert'. Use before update_thehive_case /
        close_thehive_case / update_thehive_alert to discover valid status
        values (organisations can define custom statuses).
        """
        target = (target or "case").strip().lower()
        if target not in ("case", "alert"):
            return "Error: target must be 'case' or 'alert'."
        query_name = "listCaseStatus" if target == "case" else "listAlertStatus"
        async with egress_client(timeout=30) as client:
            data, err = await _query(
                client,
                [{"_name": query_name}, _page_op(_MAX_LIMIT, 0)],
                f"list {target} statuses",
            )
            if err:
                return err
            statuses = data
            if not statuses:
                return f"No {target} statuses found."
            lines = [f"{target.capitalize()} statuses ({len(statuses)}):"]
            for s in statuses:
                if not isinstance(s, dict):
                    continue
                value = s.get("value", "?")
                stage = s.get("stage", "?")
                desc = s.get("description", "")
                desc_str = f" - {_trunc(desc, 80)}" if desc else ""
                lines.append(f"  {value} (stage: {stage}){desc_str}")
            return "\n".join(lines)

    async def list_thehive_observable_types() -> str:
        """List the observable data types configured in TheHive.

        Use before add_thehive_observable to discover valid data_type values
        (e.g. 'ip', 'domain', 'url', 'hash', 'mail', 'filename').
        """
        async with egress_client(timeout=30) as client:
            data, err = await _query(
                client,
                [{"_name": "listObservableType"}, _page_op(_MAX_LIMIT, 0)],
                "list observable types",
            )
            if err:
                return err
            types = data
            if not types:
                return "No observable types found."
            names = []
            for t in types:
                if isinstance(t, dict) and t.get("name"):
                    suffix = " (file attachment)" if t.get("isAttachment") else ""
                    names.append(f"  {t['name']}{suffix}")
            return f"Observable data types ({len(names)}):\n" + "\n".join(names)

    # ══════════════════════════════════════════════════════════════════════
    # 8. CONSOLIDATED MANAGE (long tail incl. destructive)
    # ══════════════════════════════════════════════════════════════════════

    async def manage_thehive_item(
        action: str, item_id: str, message: str = ""
    ) -> str:
        """Manage TheHive items - follow/unfollow alerts, edit/delete entities.

        action: follow_alert, unfollow_alert, update_comment (requires
        message), delete_comment, delete_task, delete_observable,
        delete_alert, delete_case. Deletions are PERMANENT and cannot be
        undone.
        """
        action = (action or "").strip().lower()
        if action not in _MANAGE_ACTIONS:
            return f"Error: action must be one of {', '.join(sorted(_MANAGE_ACTIONS))}."
        if not _sid(item_id):
            return "Error: item_id is required."
        item_ref = _sid(item_id)
        if action == "delete_case":
            item_ref = item_ref.lstrip("#")
        # Encoded form for URL interpolation - keeps a crafted item_id (e.g.
        # '../case/42') from escaping its path segment; item_ref stays
        # human-readable for the success messages.
        ref = _pid(item_ref)

        method = "post"
        payload: dict[str, Any] | None = None
        if action == "follow_alert":
            url = f"{api}/alert/{ref}/follow"
            success = f"Now following alert {item_ref}."
        elif action == "unfollow_alert":
            url = f"{api}/alert/{ref}/unfollow"
            success = f"Stopped following alert {item_ref}."
        elif action == "update_comment":
            if not (message or "").strip():
                return "Error: message is required for update_comment."
            method = "patch"
            url = f"{api}/comment/{ref}"
            payload = {"message": message}
            success = f"Comment {item_ref} updated."
        elif action == "delete_comment":
            method = "delete"
            url = f"{api}/comment/{ref}"
            success = f"Comment {item_ref} deleted (permanent)."
        elif action == "delete_task":
            method = "delete"
            url = f"{api}/task/{ref}"
            success = f"Task {item_ref} deleted (permanent)."
        elif action == "delete_observable":
            method = "delete"
            url = f"{api}/observable/{ref}"
            success = f"Observable {item_ref} deleted (permanent)."
        elif action == "delete_alert":
            method = "delete"
            url = f"{api}/alert/{ref}"
            success = f"Alert {item_ref} deleted (permanent)."
        else:  # delete_case
            method = "delete"
            url = f"{api}/case/{ref}"
            success = f"Case {item_ref} deleted (permanent)."

        async with egress_client(timeout=30) as client:
            kwargs: dict[str, Any] = {"headers": headers}
            if payload is not None:
                kwargs["json"] = payload
            _, err = await safe_request(
                client,
                method,
                url,
                _PROVIDER,
                f"{action.replace('_', ' ')} {item_ref}",
                **kwargs,
            )
            if err:
                return err
            return success

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    # 0. Current user
    executor.register("get_thehive_myself", get_thehive_myself)
    # 1. Cases
    executor.register("search_thehive_cases", search_thehive_cases)
    executor.register("get_thehive_case", get_thehive_case)
    executor.register("create_thehive_case", create_thehive_case)
    executor.register("update_thehive_case", update_thehive_case)
    executor.register("close_thehive_case", close_thehive_case)
    executor.register("merge_thehive_cases", merge_thehive_cases)
    # 2. Alerts
    executor.register("search_thehive_alerts", search_thehive_alerts)
    executor.register("get_thehive_alert", get_thehive_alert)
    executor.register("create_thehive_alert", create_thehive_alert)
    executor.register("update_thehive_alert", update_thehive_alert)
    executor.register("promote_thehive_alert", promote_thehive_alert)
    executor.register("merge_thehive_alert_into_case", merge_thehive_alert_into_case)
    # 3. Observables
    executor.register("list_thehive_observables", list_thehive_observables)
    executor.register("add_thehive_observable", add_thehive_observable)
    executor.register("update_thehive_observable", update_thehive_observable)
    # 4. Tasks + logs
    executor.register("list_thehive_tasks", list_thehive_tasks)
    executor.register("create_thehive_task", create_thehive_task)
    executor.register("update_thehive_task", update_thehive_task)
    executor.register("list_thehive_task_logs", list_thehive_task_logs)
    executor.register("add_thehive_task_log", add_thehive_task_log)
    # 5. Comments
    executor.register("list_thehive_comments", list_thehive_comments)
    executor.register("add_thehive_comment", add_thehive_comment)
    # 6. Analyzers
    executor.register("list_thehive_analyzers", list_thehive_analyzers)
    executor.register("run_thehive_analyzer", run_thehive_analyzer)
    executor.register("get_thehive_analyzer_job", get_thehive_analyzer_job)
    # 7. Reference data
    executor.register("list_thehive_statuses", list_thehive_statuses)
    executor.register("list_thehive_observable_types", list_thehive_observable_types)
    # 8. Manage
    executor.register("manage_thehive_item", manage_thehive_item)

    _severity_schema = {
        "type": "string",
        "description": "Severity: low, medium, high, or critical (or 1-4)",
        "default": "",
    }
    _tlp_schema = {
        "type": "string",
        "description": "TLP marking: clear, green, amber, amber+strict, or red",
        "default": "",
    }
    _limit_schema = {
        "type": "integer",
        "description": f"Max results to return (max {_MAX_LIMIT})",
        "default": 25,
    }
    _skip_schema = {
        "type": "integer",
        "description": "Offset for pagination",
        "default": 0,
    }

    return [
        # ── 0. Current user ───────────────────────────────────────────────
        ToolDefinition(
            name="get_thehive_myself",
            description=(
                "Get the authenticated TheHive user's profile - login, organisation, "
                "and profile/permissions. Use to discover the login usable as an "
                "assignee value on cases and tasks."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=0,
        ),
        # ── 1. Cases ──────────────────────────────────────────────────────
        ToolDefinition(
            name="search_thehive_cases",
            description=(
                "Search TheHive incident-response cases with structured filters "
                "(keyword in title/description, status, stage New/InProgress/Closed, "
                "severity, assignee, tags, flagged, creation date range). All filters "
                "combined with AND; newest first. Returns case numbers and ids for "
                "follow-up calls. Use skip for pagination."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "Keyword matched against title and description",
                        "default": "",
                    },
                    "status": {
                        "type": "string",
                        "description": "Case status (e.g. 'New', 'InProgress', 'TruePositive' - see list_thehive_statuses)",
                        "default": "",
                    },
                    "stage": {
                        "type": "string",
                        "description": "Case stage",
                        "enum": ["", "New", "InProgress", "Closed"],
                        "default": "",
                    },
                    "severity": _severity_schema,
                    "assignee": {
                        "type": "string",
                        "description": "Assignee login (e.g. 'analyst@company.org')",
                        "default": "",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tags - each must be present",
                        "default": "",
                    },
                    "flagged_only": {
                        "type": "boolean",
                        "description": "Only flagged cases",
                        "default": False,
                    },
                    "created_after": {
                        "type": "string",
                        "description": "Inclusive lower bound, YYYY-MM-DD",
                        "default": "",
                    },
                    "created_before": {
                        "type": "string",
                        "description": "Inclusive upper bound, YYYY-MM-DD",
                        "default": "",
                    },
                    "limit": _limit_schema,
                    "skip": _skip_schema,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_thehive_case",
            description=(
                "Get full details of a TheHive case by number (e.g. '42') or id "
                "(e.g. '~40964152'): description, custom fields, recent comments, "
                "and a deep link. Use list_thehive_tasks / list_thehive_observables "
                "for the case's tasks and observables."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {
                        "type": "string",
                        "description": "Case number (e.g. '42') or _id (e.g. '~40964152')",
                    },
                },
                "required": ["case_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_thehive_case",
            description=(
                "Create a new TheHive incident-response case. A case template (name "
                "or id) can seed investigation tasks and custom fields. To create a "
                "case FROM an alert, use promote_thehive_alert instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Case title"},
                    "description": {
                        "type": "string",
                        "description": "Case description (markdown supported)",
                    },
                    "severity": {**_severity_schema, "default": "medium"},
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tags",
                        "default": "",
                    },
                    "tlp": _tlp_schema,
                    "pap": {
                        "type": "string",
                        "description": "PAP marking: clear, green, amber, or red",
                        "default": "",
                    },
                    "assignee": {
                        "type": "string",
                        "description": "Assignee login",
                        "default": "",
                    },
                    "case_template": {
                        "type": "string",
                        "description": "Case template name or id to seed tasks",
                        "default": "",
                    },
                    "flag": {
                        "type": "boolean",
                        "description": "Flag the case for attention",
                        "default": False,
                    },
                },
                "required": ["title", "description"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="update_thehive_case",
            description=(
                "Update fields on an existing TheHive case (title, description, "
                "severity, status, assignee, summary, TLP/PAP, add/remove tags). "
                "Only provided fields change. To close a case with a resolution, "
                "prefer close_thehive_case."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {
                        "type": "string",
                        "description": "Case number or _id",
                    },
                    "title": {"type": "string", "default": ""},
                    "description": {"type": "string", "default": ""},
                    "severity": _severity_schema,
                    "status": {
                        "type": "string",
                        "description": "New status (see list_thehive_statuses)",
                        "default": "",
                    },
                    "assignee": {
                        "type": "string",
                        "description": "New assignee login",
                        "default": "",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Case summary/conclusion text",
                        "default": "",
                    },
                    "tlp": _tlp_schema,
                    "pap": {
                        "type": "string",
                        "description": "PAP marking: clear, green, amber, or red",
                        "default": "",
                    },
                    "add_tags": {
                        "type": "string",
                        "description": "Comma-separated tags to ADD",
                        "default": "",
                    },
                    "remove_tags": {
                        "type": "string",
                        "description": "Comma-separated tags to REMOVE",
                        "default": "",
                    },
                },
                "required": ["case_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="close_thehive_case",
            description=(
                "Close a TheHive case with a resolution status and a mandatory "
                "summary. Default closed statuses: TruePositive, FalsePositive, "
                "Indeterminate, Duplicated (orgs may define custom ones - see "
                "list_thehive_statuses)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {
                        "type": "string",
                        "description": "Case number or _id",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Resolution summary describing the outcome",
                    },
                    "status": {
                        "type": "string",
                        "description": "Closed status (e.g. 'TruePositive', 'FalsePositive')",
                        "default": "TruePositive",
                    },
                    "impact_status": {
                        "type": "string",
                        "description": "Impact assessment",
                        "enum": ["", "WithImpact", "NoImpact", "NotApplicable"],
                        "default": "",
                    },
                },
                "required": ["case_id", "summary"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="merge_thehive_cases",
            description=(
                "Merge two or more TheHive cases into one (combines observables, "
                "tasks, and descriptions). Provide at least two comma-separated "
                "case ids or numbers."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_ids": {
                        "type": "string",
                        "description": "Comma-separated case ids/numbers (at least 2, e.g. '42,57')",
                    },
                },
                "required": ["case_ids"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 2. Alerts ─────────────────────────────────────────────────────
        ToolDefinition(
            name="search_thehive_alerts",
            description=(
                "Search TheHive alerts (the triage inbox) with structured filters "
                "(keyword, type, source, status e.g. 'New', stage, severity, tags, "
                "creation date range). All filters combined with AND; newest first. "
                "Returns alert ids for get/promote/merge follow-ups. Use skip for "
                "pagination."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "Keyword matched against title and description",
                        "default": "",
                    },
                    "alert_type": {
                        "type": "string",
                        "description": "Alert type (e.g. 'siem', 'phishing')",
                        "default": "",
                    },
                    "source": {
                        "type": "string",
                        "description": "Alert source (e.g. 'splunk', 'misp')",
                        "default": "",
                    },
                    "status": {
                        "type": "string",
                        "description": "Alert status (e.g. 'New', 'InProgress', 'Imported' - see list_thehive_statuses)",
                        "default": "",
                    },
                    "stage": {
                        "type": "string",
                        "description": "Alert stage",
                        "default": "",
                    },
                    "severity": _severity_schema,
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tags - each must be present",
                        "default": "",
                    },
                    "created_after": {
                        "type": "string",
                        "description": "Inclusive lower bound, YYYY-MM-DD",
                        "default": "",
                    },
                    "created_before": {
                        "type": "string",
                        "description": "Inclusive upper bound, YYYY-MM-DD",
                        "default": "",
                    },
                    "limit": _limit_schema,
                    "skip": _skip_schema,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_thehive_alert",
            description=(
                "Get full details of a TheHive alert by id, including its "
                "observables, custom fields, related case, and a deep link. "
                "Call search_thehive_alerts first to find alert ids."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {
                        "type": "string",
                        "description": "Alert _id (e.g. '~40964152')",
                    },
                },
                "required": ["alert_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_thehive_alert",
            description=(
                "Create a new TheHive alert. type/source/source_ref uniquely "
                "identify it (e.g. 'siem'/'splunk'/'evt-123'). Add observables "
                "afterwards with add_thehive_observable (alert_id)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_type": {
                        "type": "string",
                        "description": "Alert type (e.g. 'siem', 'phishing', 'external')",
                    },
                    "source": {
                        "type": "string",
                        "description": "Source system (e.g. 'splunk', 'email')",
                    },
                    "source_ref": {
                        "type": "string",
                        "description": "Unique reference in the source (e.g. event id)",
                    },
                    "title": {"type": "string", "description": "Alert title"},
                    "description": {
                        "type": "string",
                        "description": "Alert description (markdown supported)",
                    },
                    "severity": {**_severity_schema, "default": "medium"},
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tags",
                        "default": "",
                    },
                    "tlp": _tlp_schema,
                    "pap": {
                        "type": "string",
                        "description": "PAP marking: clear, green, amber, or red",
                        "default": "",
                    },
                    "external_link": {
                        "type": "string",
                        "description": "URL back to the alert in the source system",
                        "default": "",
                    },
                },
                "required": ["alert_type", "source", "source_ref", "title", "description"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="update_thehive_alert",
            description=(
                "Update fields on an existing TheHive alert (title, description, "
                "severity, status, tags, TLP). Only provided fields change; tags "
                "REPLACES the tag list. Use status to close an alert without a "
                "case (e.g. 'FalsePositive')."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {
                        "type": "string",
                        "description": "Alert _id",
                    },
                    "title": {"type": "string", "default": ""},
                    "description": {"type": "string", "default": ""},
                    "severity": _severity_schema,
                    "status": {
                        "type": "string",
                        "description": "New status (see list_thehive_statuses with target='alert')",
                        "default": "",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tags (replaces existing)",
                        "default": "",
                    },
                    "tlp": _tlp_schema,
                },
                "required": ["alert_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="promote_thehive_alert",
            description=(
                "Promote a TheHive alert to a NEW case (triage escalation) - "
                "imports observables and description. A case template can seed "
                "investigation tasks. To add the alert to an EXISTING case, use "
                "merge_thehive_alert_into_case instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {
                        "type": "string",
                        "description": "Alert _id to promote",
                    },
                    "case_template": {
                        "type": "string",
                        "description": "Case template name or id to seed tasks",
                        "default": "",
                    },
                    "title": {
                        "type": "string",
                        "description": "Override the case title (defaults to alert title)",
                        "default": "",
                    },
                    "severity": _severity_schema,
                },
                "required": ["alert_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="merge_thehive_alert_into_case",
            description=(
                "Merge a TheHive alert into an EXISTING case - imports the alert's "
                "observables and procedures and marks the alert as imported. Use "
                "promote_thehive_alert to create a new case instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {
                        "type": "string",
                        "description": "Alert _id to merge",
                    },
                    "case_id": {
                        "type": "string",
                        "description": "Target case number or _id",
                    },
                },
                "required": ["alert_id", "case_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 3. Observables ────────────────────────────────────────────────
        ToolDefinition(
            name="list_thehive_observables",
            description=(
                "List the observables (IOCs/artifacts) of a TheHive case OR alert. "
                "Optionally filter by data type (e.g. 'ip', 'domain', 'hash') or "
                "IOC-flagged only. Returns observable ids for update/analyzer "
                "follow-ups. Use skip for pagination."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {
                        "type": "string",
                        "description": "Case number or _id (provide this OR alert_id)",
                        "default": "",
                    },
                    "alert_id": {
                        "type": "string",
                        "description": "Alert _id (provide this OR case_id)",
                        "default": "",
                    },
                    "data_type": {
                        "type": "string",
                        "description": "Filter by data type (see list_thehive_observable_types)",
                        "default": "",
                    },
                    "ioc_only": {
                        "type": "boolean",
                        "description": "Only IOC-flagged observables",
                        "default": False,
                    },
                    "limit": {**_limit_schema, "default": 50},
                    "skip": _skip_schema,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="add_thehive_observable",
            description=(
                "Add one or more observables (IOCs) to a TheHive case OR alert. "
                "data accepts multiple values separated by commas or newlines "
                "(same data_type for all). Set ioc=true for confirmed indicators "
                "and sighted=true when observed in the environment."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "data": {
                        "type": "string",
                        "description": "Observable value(s) - comma or newline separated for bulk",
                    },
                    "data_type": {
                        "type": "string",
                        "description": "Data type (e.g. 'ip', 'domain', 'url', 'hash', 'mail')",
                    },
                    "case_id": {
                        "type": "string",
                        "description": "Case number or _id (provide this OR alert_id)",
                        "default": "",
                    },
                    "alert_id": {
                        "type": "string",
                        "description": "Alert _id (provide this OR case_id)",
                        "default": "",
                    },
                    "message": {
                        "type": "string",
                        "description": "Analyst note attached to the observable(s)",
                        "default": "",
                    },
                    "ioc": {
                        "type": "boolean",
                        "description": "Mark as confirmed IOC",
                        "default": False,
                    },
                    "sighted": {
                        "type": "boolean",
                        "description": "Mark as sighted in the environment",
                        "default": False,
                    },
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tags",
                        "default": "",
                    },
                    "tlp": _tlp_schema,
                },
                "required": ["data", "data_type"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="update_thehive_observable",
            description=(
                "Update flags/metadata on a TheHive observable - flag as IOC, "
                "record a sighting, change note, tags (replaces), or TLP. ioc and "
                "sighted accept 'true'/'false' (empty = unchanged)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "observable_id": {
                        "type": "string",
                        "description": "Observable _id",
                    },
                    "ioc": {
                        "type": "string",
                        "description": "Set IOC flag",
                        "enum": ["", "true", "false"],
                        "default": "",
                    },
                    "sighted": {
                        "type": "string",
                        "description": "Set sighted flag",
                        "enum": ["", "true", "false"],
                        "default": "",
                    },
                    "message": {
                        "type": "string",
                        "description": "New analyst note",
                        "default": "",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tags (replaces existing)",
                        "default": "",
                    },
                    "tlp": _tlp_schema,
                },
                "required": ["observable_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 4. Tasks + logs ───────────────────────────────────────────────
        ToolDefinition(
            name="list_thehive_tasks",
            description=(
                "List investigation tasks of a TheHive case (case_id) or org-wide "
                "(no case_id). Filter by status (Waiting, InProgress, Completed, "
                "Cancel) or assignee. Returns task ids for update/log follow-ups. "
                "Use skip for pagination."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {
                        "type": "string",
                        "description": "Case number or _id (empty = all visible tasks)",
                        "default": "",
                    },
                    "status": {
                        "type": "string",
                        "description": "Task status filter",
                        "enum": ["", "Waiting", "InProgress", "Completed", "Cancel"],
                        "default": "",
                    },
                    "assignee": {
                        "type": "string",
                        "description": "Assignee login filter",
                        "default": "",
                    },
                    "limit": {**_limit_schema, "default": 50},
                    "skip": _skip_schema,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_thehive_task",
            description=(
                "Create an investigation task in a TheHive case. group organises "
                "tasks into sections (e.g. 'identification', 'containment', "
                "'remediation'); due_date is YYYY-MM-DD."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {
                        "type": "string",
                        "description": "Case number or _id",
                    },
                    "title": {"type": "string", "description": "Task title"},
                    "description": {
                        "type": "string",
                        "description": "Task description (markdown supported)",
                        "default": "",
                    },
                    "group": {
                        "type": "string",
                        "description": "Task group/section (e.g. 'containment')",
                        "default": "",
                    },
                    "assignee": {
                        "type": "string",
                        "description": "Assignee login",
                        "default": "",
                    },
                    "due_date": {
                        "type": "string",
                        "description": "Due date, YYYY-MM-DD",
                        "default": "",
                    },
                },
                "required": ["case_id", "title"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="update_thehive_task",
            description=(
                "Update a TheHive investigation task - set status='Completed' to "
                "complete it, 'InProgress' to start it, or change title/assignee/"
                "group/due date. Only provided fields change."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task _id",
                    },
                    "status": {
                        "type": "string",
                        "description": "New status",
                        "enum": ["", "Waiting", "InProgress", "Completed", "Cancel"],
                        "default": "",
                    },
                    "title": {"type": "string", "default": ""},
                    "description": {"type": "string", "default": ""},
                    "assignee": {
                        "type": "string",
                        "description": "New assignee login",
                        "default": "",
                    },
                    "group": {
                        "type": "string",
                        "description": "New task group/section",
                        "default": "",
                    },
                    "due_date": {
                        "type": "string",
                        "description": "New due date, YYYY-MM-DD",
                        "default": "",
                    },
                },
                "required": ["task_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_thehive_task_logs",
            description=(
                "List the work logs of a TheHive task (analyst findings and "
                "progress notes, newest first). Use skip for pagination."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task _id",
                    },
                    "limit": _limit_schema,
                    "skip": _skip_schema,
                },
                "required": ["task_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="add_thehive_task_log",
            description=(
                "Add a work log to a TheHive task to document findings, evidence, "
                "or progress (markdown supported). Set include_in_timeline=true to "
                "surface it on the case timeline."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task _id",
                    },
                    "message": {
                        "type": "string",
                        "description": "Log content (markdown supported)",
                    },
                    "include_in_timeline": {
                        "type": "boolean",
                        "description": "Also show the log on the case timeline",
                        "default": False,
                    },
                },
                "required": ["task_id", "message"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 5. Comments ───────────────────────────────────────────────────
        ToolDefinition(
            name="list_thehive_comments",
            description=(
                "List comments on a TheHive case OR alert (newest first). Returns "
                "comment ids usable with manage_thehive_item for edit/delete. Use "
                "skip for pagination."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {
                        "type": "string",
                        "description": "Case number or _id (provide this OR alert_id)",
                        "default": "",
                    },
                    "alert_id": {
                        "type": "string",
                        "description": "Alert _id (provide this OR case_id)",
                        "default": "",
                    },
                    "limit": _limit_schema,
                    "skip": _skip_schema,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="add_thehive_comment",
            description=(
                "Add a comment to a TheHive case OR alert (markdown supported). "
                "Use for triage notes and collaboration; use add_thehive_task_log "
                "for task-scoped findings."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Comment text (markdown supported)",
                    },
                    "case_id": {
                        "type": "string",
                        "description": "Case number or _id (provide this OR alert_id)",
                        "default": "",
                    },
                    "alert_id": {
                        "type": "string",
                        "description": "Alert _id (provide this OR case_id)",
                        "default": "",
                    },
                },
                "required": ["message"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 6. Cortex analyzers ───────────────────────────────────────────
        ToolDefinition(
            name="list_thehive_analyzers",
            description=(
                "List Cortex analyzers available in TheHive for observable "
                "enrichment (e.g. VirusTotal, AbuseIPDB). Optionally filter by "
                "observable data type. Requires a Cortex connector configured in "
                "TheHive. Returns analyzer ids for run_thehive_analyzer."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "data_type": {
                        "type": "string",
                        "description": "Filter analyzers by observable data type (e.g. 'ip')",
                        "default": "",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="run_thehive_analyzer",
            description=(
                "Run a Cortex analyzer on a TheHive observable for enrichment "
                "(reputation, sandbox, whois...). The Cortex server is "
                "auto-resolved when cortex_id is omitted. Returns a job id - poll "
                "with get_thehive_analyzer_job for the report."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "analyzer_id": {
                        "type": "string",
                        "description": "Analyzer id (from list_thehive_analyzers)",
                    },
                    "observable_id": {
                        "type": "string",
                        "description": "Observable _id (from list_thehive_observables)",
                    },
                    "cortex_id": {
                        "type": "string",
                        "description": "Cortex server name (auto-resolved when empty)",
                        "default": "",
                    },
                },
                "required": ["analyzer_id", "observable_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="get_thehive_analyzer_job",
            description=(
                "Get the status and report of a Cortex analyzer job started with "
                "run_thehive_analyzer. Includes the report summary when the job "
                "has finished."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Analyzer job _id",
                    },
                },
                "required": ["job_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        # ── 7. Reference data ─────────────────────────────────────────────
        ToolDefinition(
            name="list_thehive_statuses",
            description=(
                "List the case or alert statuses configured in the organisation "
                "(including custom ones) with their stage. Use before setting a "
                "status on update_thehive_case / close_thehive_case / "
                "update_thehive_alert."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Which statuses to list",
                        "enum": ["case", "alert"],
                        "default": "case",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_thehive_observable_types",
            description=(
                "List the observable data types configured in TheHive (e.g. 'ip', "
                "'domain', 'url', 'hash', 'mail'). Use before "
                "add_thehive_observable to pick a valid data_type."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=0,
        ),
        # ── 8. Consolidated manage ────────────────────────────────────────
        ToolDefinition(
            name="manage_thehive_item",
            description=(
                "Manage TheHive items - long-tail operations: follow_alert / "
                "unfollow_alert (track alert updates), update_comment (needs "
                "message), delete_comment, delete_task, delete_observable, "
                "delete_alert, delete_case. Deletions are PERMANENT and cannot "
                "be undone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Operation to perform",
                        "enum": sorted(_MANAGE_ACTIONS),
                    },
                    "item_id": {
                        "type": "string",
                        "description": "Target item _id (alert/comment/task/observable/case)",
                    },
                    "message": {
                        "type": "string",
                        "description": "New comment text (update_comment only)",
                        "default": "",
                    },
                },
                "required": ["action", "item_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]
