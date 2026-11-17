"""Shared helpers for the ServiceNow integration package.

Holds the constants, credential parsing, HTTP wrapper, encoded-query builder,
deep-link helper, and the record formatters used by every ServiceNow domain
module. Mirrors the ``jira/_common.py`` and ``opencti/_common.py`` split.

ServiceNow exposes a single, uniform REST surface:

* **Table API** (``/api/now/table/{table}``) - universal CRUD over every table
  (incident, change_request, problem, task, cmdb_ci, sys_user, kb_knowledge, ...).
* **Aggregate API** (``/api/now/stats/{table}``) - server-side counts.
* **Attachment API** (``/api/now/attachment``) - list / upload / download / delete.
* **Service Catalog API** (``/api/sn_sc/servicecatalog``) - browse + order items.

Authentication
--------------
ServiceNow authenticates with **HTTP Basic** (a username + password, ideally a
dedicated integration user). ``integration_tools._get_token`` assembles the
compound ``base_url||username||password`` token this package parses. ``base_url``
is the instance URL (e.g. ``https://acme.service-now.com``) - which is also the
web UI host, so it doubles as the deep-link target surfaced to the agent.

Read tools request ``sysparm_display_value=true`` so reference/choice fields come
back as human-readable labels (e.g. state "In Progress" instead of "2"); write
tools deliberately send raw values - reference fields take a sys_id (resolved via
the ``search_*`` tools) and choice fields take their numeric value. We do NOT set
``sysparm_input_display_value=true``, which would force EVERY field to be a
display label and break passing a just-resolved sys_id (see ``_WRITE_PARAMS``).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx

from app.core.egress import egress_client
from app.integrations.http import (
    format_entity_list,
    parse_compound_token,
    safe_request,
)

_PROVIDER = "ServiceNow"
_SOURCE = "integration:servicenow"

# ServiceNow's Table API default page size is 10; cap our pages so a single
# tool call never pulls an unbounded result set into the LLM context.
_MAX_PAGE = 100
_DEFAULT_LIMIT = 20
# Truncate large rich-text bodies (descriptions, work notes) for LLM context.
_MAX_BODY_CHARS = 4_000
# Per-field cap when rendering arbitrary field values.
_MAX_FIELD_CHARS = 500


# ---------------------------------------------------------------------------
# Credentials + URL builders
# ---------------------------------------------------------------------------


def parse_servicenow_token(token: str) -> tuple[str, str, str]:
    """Split ``base_url||username||password`` into its three parts.

    The instance URL is stripped of trailing slashes; all three values have
    surrounding whitespace removed so the Basic-auth header and request URLs
    never carry stray whitespace/control characters (which httpx rejects).
    """
    base_url, username, password = parse_compound_token(token, 3, greedy_last=True)
    return (base_url or "").strip().rstrip("/"), (username or "").strip(), (
        password or ""
    ).strip()


def servicenow_api_base(base_url: str) -> str:
    """Return the ServiceNow instance base URL (no trailing slash).

    There is no default - the instance host is customer-specific
    (``https://<instance>.service-now.com``), so a blank base resolves to ``""``
    and the caller surfaces a clear "not configured" error.
    """
    return (base_url or "").strip().rstrip("/")


def build_servicenow_headers(username: str, password: str) -> dict[str, str]:
    """Build ServiceNow request headers (HTTP Basic with username/password)."""
    import base64

    raw = f"{username}:{password}".encode()
    token = base64.b64encode(raw).decode("ascii")
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def table_url(base: str, table: str, sys_id: str = "") -> str:
    """Build a Table API URL (``/api/now/table/{table}[/{sys_id}]``)."""
    url = f"{base}/api/now/table/{quote(table, safe='')}"
    if sys_id:
        url = f"{url}/{quote(sys_id, safe='')}"
    return url


def stats_url(base: str, table: str) -> str:
    """Build an Aggregate API URL (``/api/now/stats/{table}``)."""
    return f"{base}/api/now/stats/{quote(table, safe='')}"


def attachment_url(base: str, path: str = "") -> str:
    """Build an Attachment API URL (``/api/now/attachment[/{path}]``)."""
    base_path = f"{base}/api/now/attachment"
    return f"{base_path}/{path}" if path else base_path


def sc_url(base: str, path: str) -> str:
    """Build a Service Catalog API URL (``/api/sn_sc/servicecatalog/{path}``)."""
    return f"{base}/api/sn_sc/servicecatalog/{path}"


def record_url(base: str, table: str, sys_id: str) -> str:
    """Build a user-facing deep link to a record's form in the ServiceNow UI.

    ServiceNow's instance host is also the web UI host, so ``{base}/{table}.do?
    sys_id={sys_id}`` opens the record form directly. Only emitted from the
    already-validated, stored instance ``base`` - never from model input.
    """
    if not base or not table or not sys_id:
        return ""
    return f"{base}/{quote(table, safe='')}.do?sys_id={quote(sys_id, safe='')}"


# ---------------------------------------------------------------------------
# HTTP wrapper
# ---------------------------------------------------------------------------


async def sn_request(
    method: str,
    url: str,
    action: str,
    *,
    timeout: float = 30.0,
    expected_statuses: set[int] | None = None,
    follow_redirects: bool = False,
    **kwargs: Any,
) -> tuple[httpx.Response | None, str | None]:
    """Execute a ServiceNow HTTP request via the shared ``safe_request`` helper.

    Opens a short-lived async client (the tool surface is one request per call),
    applies standardised error handling + 429 retry, and returns
    ``(response, None)`` on success or ``(None, error_string)`` on failure (or
    ``(response, error_string)`` when ``expected_statuses`` is set, so callers
    can branch on a benign 404). ``follow_redirects`` is needed by the attachment
    download endpoint, which can 302 to a streamed file URL.
    """
    async with egress_client(
        timeout=timeout, follow_redirects=follow_redirects
    ) as client:
        return await safe_request(
            client,
            method,
            url,
            _PROVIDER,
            action,
            expected_statuses=expected_statuses,
            **kwargs,
        )


def result_of(resp: httpx.Response) -> Any:
    """Return the ``result`` payload from a ServiceNow response, guarding JSON.

    ServiceNow wraps every Table/Aggregate/Catalog response in
    ``{"result": ...}``. Returns ``None`` when the body is not decodable so a
    2xx response with a malformed body never crashes a tool.
    """
    try:
        body = resp.json()
    except Exception:
        return None
    if isinstance(body, dict):
        return body.get("result")
    return body


def clamp(count: Any, default: int = _DEFAULT_LIMIT, ceiling: int = _MAX_PAGE) -> int:
    """Coerce a model-supplied count into a sane positive int <= ceiling."""
    try:
        value = int(count)
    except (TypeError, ValueError):
        return default
    if value < 1:
        return default
    return min(value, ceiling)


# ---------------------------------------------------------------------------
# Encoded-query builder
# ---------------------------------------------------------------------------


def build_encoded_query(
    filters: dict[str, Any] | None = None,
    *,
    raw_query: str = "",
    text: str = "",
    text_fields: tuple[str, ...] = ("short_descriptionLIKE",),
    order_by: str = "",
    order_desc: bool = True,
) -> str:
    """Compose a ServiceNow encoded query (``^``-joined GlideRecord syntax).

    * ``filters`` - ``{field: value}`` becomes ``field=value`` clauses (a list/
      tuple value becomes an ``INvalue,value`` clause). ``None``/empty values
      are skipped.
    * ``raw_query`` - a caller-supplied encoded query, appended verbatim so the
      agent can express anything the structured params cannot.
    * ``text`` - free-text matched against ``text_fields`` (default
      ``short_descriptionLIKE``); multiple fields are OR-ed.
    * ``order_by`` - appended as ``ORDERBYDESC{field}`` (or ``ORDERBY{field}``).
    """
    clauses: list[str] = []
    for field, value in (filters or {}).items():
        if value is None or value == "":
            continue
        if isinstance(value, (list, tuple)):
            joined = ",".join(str(v) for v in value if v not in (None, ""))
            if joined:
                clauses.append(f"{field}IN{joined}")
        else:
            clauses.append(f"{field}={value}")
    if text and text.strip():
        cleaned = text.strip()
        text_clause = "^OR".join(f"{tf}{cleaned}" for tf in text_fields)
        if text_clause:
            clauses.append(text_clause)
    if raw_query and raw_query.strip():
        clauses.append(raw_query.strip())
    query = "^".join(clauses)
    if order_by:
        prefix = "ORDERBYDESC" if order_desc else "ORDERBY"
        query = f"{query}^{prefix}{order_by}" if query else f"{prefix}{order_by}"
    return query


# ---------------------------------------------------------------------------
# Field-value rendering + record formatting
# ---------------------------------------------------------------------------


def disp(value: Any) -> str:
    """Render a ServiceNow field value as readable text.

    With ``sysparm_display_value=true`` most fields come back as plain strings,
    but reference/choice fields can still be objects (``{"display_value": ...,
    "value": ...}`` or the default ``{"link": ..., "value": ...}``). This
    normalises every shape to a single string.
    """
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(
            value.get("display_value")
            or value.get("value")
            or value.get("name")
            or value.get("link")
            or ""
        )
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def truncate(text: Any, limit: int = _MAX_FIELD_CHARS) -> str:
    """Trim *text* to *limit* characters with an ellipsis marker."""
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[:limit] + "..."


def cell(value: Any, max_len: int = 160) -> str:
    """Render a value safe for a single-line summary (strips newlines/tabs)."""
    text = disp(value)
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return text[:max_len]


def field_lines(
    record: dict[str, Any], fields: list[tuple[str, str]]
) -> list[str]:
    """Render ``(label, field_key)`` pairs as ``  Label: value`` lines.

    Skips empty values so the summary stays compact.
    """
    lines: list[str] = []
    for label, key in fields:
        rendered = disp(record.get(key))
        if rendered:
            lines.append(f"  {label}: {rendered}")
    return lines


# ---------------------------------------------------------------------------
# Generic Table API operations - shared by every domain module
# ---------------------------------------------------------------------------

# A ServiceNow sys_id is a 32-character hex GUID. Used to tell a sys_id apart
# from a human record number (INC0010001, CHG0030002, ...) so callers can pass
# either to get/update tools.
_SYS_ID_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)

# Read params: human-readable display values, no bulky reference link objects.
_READ_PARAMS: dict[str, str] = {
    "sysparm_display_value": "true",
    "sysparm_exclude_reference_link": "true",
}
# Write params: the RESPONSE is rendered with display values (so the agent sees
# readable labels back), but INPUT stays raw - reference fields take a sys_id
# (resolved via the search_* tools) and choice fields take their numeric value.
# We deliberately do NOT set ``sysparm_input_display_value=true``: it would force
# EVERY field to be a display label, breaking the common case where the agent
# passes a sys_id it just resolved (a sys_id is not a valid "display value").
_WRITE_PARAMS: dict[str, str] = dict(_READ_PARAMS)


def is_sys_id(identifier: str) -> bool:
    """Return ``True`` when *identifier* looks like a 32-char ServiceNow sys_id."""
    return bool(_SYS_ID_RE.match((identifier or "").strip()))


def total_count(resp: httpx.Response) -> int | None:
    """Read the ``X-Total-Count`` header (total rows matching the query)."""
    try:
        return int(resp.headers.get("X-Total-Count"))
    except (TypeError, ValueError):
        return None


async def list_table(
    base: str,
    headers: dict[str, str],
    table: str,
    action: str,
    formatter: Any,
    *,
    noun: str,
    query: str = "",
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
    fields: str = "",
) -> str:
    """GET a Table API list, format it, and append an offset pagination hint."""
    off = max(0, int(offset or 0))
    params: dict[str, Any] = {
        **_READ_PARAMS,
        "sysparm_query": query,
        "sysparm_limit": clamp(limit),
        "sysparm_offset": off,
    }
    if fields:
        params["sysparm_fields"] = fields
    resp, err = await sn_request(
        "get", table_url(base, table), action, headers=headers, params=params
    )
    if err:
        return err
    rows = result_of(resp)
    rows = rows if isinstance(rows, list) else []
    out = format_entity_list(rows, formatter, noun=noun)
    total = total_count(resp)
    shown = off + len(rows)
    if total is not None and shown < total:
        # ``shown`` is the cumulative count seen so far (offset + this page), so
        # the hint stays accurate when paging past a non-zero offset.
        out += f"\n[Showing {shown} of {total}; pass offset={shown} for more]"
    return out


async def resolve_sys_id(
    base: str,
    headers: dict[str, str],
    table: str,
    identifier: str,
    *,
    number_field: str = "number",
) -> tuple[str, str | None]:
    """Resolve a record number (or sys_id) to a sys_id. Returns ``(sys_id, err)``."""
    identifier = (identifier or "").strip()
    if not identifier:
        return "", f"Error: a sys_id or {number_field} is required."
    if is_sys_id(identifier):
        return identifier, None
    resp, err = await sn_request(
        "get",
        table_url(base, table),
        f"resolve {table} {identifier}",
        headers=headers,
        params={
            "sysparm_query": f"{number_field}={identifier}",
            "sysparm_limit": 1,
            "sysparm_fields": "sys_id",
        },
    )
    if err:
        return "", err
    rows = result_of(resp)
    if not isinstance(rows, list) or not rows:
        return "", f"No {table} record found with {number_field} '{identifier}'."
    return str((rows[0] or {}).get("sys_id") or ""), None


async def get_record(
    base: str,
    headers: dict[str, str],
    table: str,
    identifier: str,
    action: str,
    formatter: Any,
    *,
    number_field: str = "number",
    fields: str = "",
) -> str:
    """GET a single record by sys_id or human number and format it."""
    identifier = (identifier or "").strip()
    if not identifier:
        return f"Error: a sys_id or {number_field} is required."
    params = dict(_READ_PARAMS)
    if fields:
        params["sysparm_fields"] = fields
    if is_sys_id(identifier):
        resp, err = await sn_request(
            "get",
            table_url(base, table, identifier),
            action,
            headers=headers,
            params=params,
            expected_statuses={404},
        )
        if err:
            if resp is not None and resp.status_code == 404:
                return f"{table} record {identifier} not found."
            return err
        rec = result_of(resp)
    else:
        params["sysparm_query"] = f"{number_field}={identifier}"
        params["sysparm_limit"] = 1
        resp, err = await sn_request(
            "get", table_url(base, table), action, headers=headers, params=params
        )
        if err:
            return err
        rows = result_of(resp)
        if not isinstance(rows, list) or not rows:
            return f"No {table} record found with {number_field} '{identifier}'."
        rec = rows[0]
    if not isinstance(rec, dict):
        return f"{table} record {identifier} not found."
    return formatter(rec)


async def create_record(
    base: str,
    headers: dict[str, str],
    table: str,
    action: str,
    body: dict[str, Any],
    formatter: Any,
) -> str:
    """POST a new record (raw field values; see ``_WRITE_PARAMS``) and format it."""
    resp, err = await sn_request(
        "post",
        table_url(base, table),
        action,
        headers=headers,
        params=_WRITE_PARAMS,
        json=body,
    )
    if err:
        return err
    rec = result_of(resp)
    if not isinstance(rec, dict):
        return f"Created the {table} record, but ServiceNow returned no detail."
    return formatter(rec)


async def update_record(
    base: str,
    headers: dict[str, str],
    table: str,
    identifier: str,
    action: str,
    body: dict[str, Any],
    formatter: Any,
    *,
    number_field: str = "number",
) -> str:
    """Resolve *identifier* to a sys_id, PATCH the record, and format the result."""
    if not body:
        return "Error: no fields to update were provided."
    sys_id, err = await resolve_sys_id(
        base, headers, table, identifier, number_field=number_field
    )
    if err:
        return err
    resp, err = await sn_request(
        "patch",
        table_url(base, table, sys_id),
        action,
        headers=headers,
        params=_WRITE_PARAMS,
        json=body,
    )
    if err:
        return err
    rec = result_of(resp)
    if not isinstance(rec, dict):
        return f"Updated the {table} record."
    return formatter(rec)


async def count_records(
    base: str, headers: dict[str, str], table: str, query: str, action: str
) -> tuple[int | None, str | None]:
    """Return ``(count, err)`` from the Aggregate API for *table* + *query*."""
    resp, err = await sn_request(
        "get",
        stats_url(base, table),
        action,
        headers=headers,
        params={"sysparm_count": "true", "sysparm_query": query},
    )
    if err:
        return None, err
    res = result_of(resp)
    stats = (res or {}).get("stats") if isinstance(res, dict) else None
    raw = (stats or {}).get("count") if isinstance(stats, dict) else None
    try:
        return int(raw), None
    except (TypeError, ValueError):
        return None, None


__all__ = [
    "_MAX_BODY_CHARS",
    "_MAX_PAGE",
    "_PROVIDER",
    "_SOURCE",
    "attachment_url",
    "build_encoded_query",
    "build_servicenow_headers",
    "cell",
    "clamp",
    "count_records",
    "create_record",
    "disp",
    "field_lines",
    "format_entity_list",
    "get_record",
    "is_sys_id",
    "list_table",
    "parse_servicenow_token",
    "record_url",
    "resolve_sys_id",
    "result_of",
    "sc_url",
    "servicenow_api_base",
    "sn_request",
    "stats_url",
    "table_url",
    "total_count",
    "truncate",
    "update_record",
]
