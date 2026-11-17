"""Shared helpers for the Splunk integration provider package.

Splunk Enterprise and Splunk Cloud expose a single management REST API on port
``8089``. Splunk Enterprise Security (ES) layers its own endpoints on top of the
same API (``/services/notable_update``, ``/services/data/threat_intel/...``), so
one credential + base URL drives both the core SIEM and the ES premium app.

Authentication
--------------
Two interchangeable modes, selected by whether a ``username`` is configured:

* **Bearer token** (recommended, works for Splunk Cloud + ES) -
  ``Authorization: Bearer <token>`` where ``<token>`` is a Splunk authentication
  token minted in *Settings > Tokens*.
* **HTTP Basic** (on-prem Splunk Enterprise) - ``username`` + the secret as the
  password.

``integration_tools._get_token`` assembles the compound
``base_url||username||token`` this package parses: ``base_url`` is the management
host (e.g. ``https://splunk.example.com:8089``), ``username`` is blank for token
auth, and the secret (token or password) is the integration's API-key field.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

from app.core.egress import egress_client
from app.integrations.http import safe_request

logger = logging.getLogger(__name__)

PROVIDER = "Splunk"
SOURCE = "integration:splunk"

# Default request timeout (seconds); the async search flow gets a longer ceiling.
DEFAULT_TIMEOUT = 30
SEARCH_TIMEOUT = 60

# Search-job polling budget (create job -> poll status -> fetch results). Bounded
# so a slow/large hunt can't block the agentic loop indefinitely.
SEARCH_POLL_INTERVAL = 3.0
SEARCH_MAX_POLLS = 20
DEFAULT_SEARCH_EARLIEST = "-24h"
DEFAULT_SEARCH_LATEST = "now"

# Upper bound on rows fetched / events returned in a single call, so an
# unbounded ``limit`` can't request an enormous response or stress the
# search head.
MAX_RESULTS = 1000
# Upper bound on a generic REST listing page (saved searches, indexes, ...).
MAX_PAGE = 200

# User-facing message (not a secret) shown when the credential is blank/missing.
CREDS_ERROR = (
    "Error: Splunk credentials are not configured. Set the authentication "
    "token (API Key field) and the base URL (management endpoint, e.g. "
    "https://splunk.example.com:8089) on the integration."
)

# Default Enterprise Security notable review statuses (the out-of-the-box
# ``reviewstatuses.conf`` ids). The live list is fetched by
# ``list_splunk_es_notable_statuses``; this is the documented fallback.
DEFAULT_NOTABLE_STATUSES: dict[str, str] = {
    "0": "Unassigned",
    "1": "New",
    "2": "In Progress",
    "3": "Pending",
    "4": "Resolved",
    "5": "Closed",
}

# Valid Enterprise Security notable urgencies.
NOTABLE_URGENCIES: tuple[str, ...] = (
    "informational",
    "low",
    "medium",
    "high",
    "critical",
)

# Enterprise Security threat-intel collections and their key indicator fields
# (``/services/data/threat_intel/item/{collection}``). Surfaced to the agent so
# it knows which fields a given collection accepts.
THREAT_INTEL_COLLECTIONS: dict[str, str] = {
    "ip_intel": "ip, domain",
    "file_intel": "file_name, file_hash",
    "user_intel": "user",
    "http_intel": "http_user_agent, ip, http_referrer, url, domain",
    "email_intel": "src_user, subject, file_name, file_hash, embedded_ip, embedded_domain",
    "service_intel": "service, service_file_name, service_file_hash",
    "process_intel": "process, process_file_name, dest, src",
    "registry_intel": "registry_path, registry_value_name, registry_value_text, user",
    "certificate_intel": "certificate_serial, certificate_subject, certificate_issuer, ip, domain",
}

_URGENCY_ICONS: dict[str, str] = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🔵",
    "informational": "⚪",
    "info": "⚪",
    "unknown": "⚪",
}

# A request helper closure: (method, path, action, **kwargs) -> (json, error).
ApiFn = Callable[..., Awaitable[tuple[Any, str | None]]]


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def safe_int(value: Any, default: int, *, lo: int, hi: int | None = None) -> int:
    """Coerce a (possibly string/None/float) tool argument to a clamped int.

    Tool arguments are not schema-validated at runtime - an LLM may pass a
    numeric string, a float, or ``None`` - so any ``int()`` applied to a
    caller-supplied value must never raise. Non-numeric and non-positive values
    fall back to *default*; the result is floored at *lo* and (when given)
    capped at *hi*.
    """
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if result <= 0:
        result = default
    result = max(lo, result)
    if hi is not None:
        result = min(result, hi)
    return result


def trunc(text: Any, max_len: int = 300) -> str:
    if text is None:
        return ""
    text = str(text)
    return text[:max_len] + ("…" if len(text) > max_len else "")


def dt(value: Any) -> str:
    """Trim an ISO-8601 timestamp to second precision for compact display."""
    return str(value or "")[:19]


def cell(value: Any, max_len: int = 120) -> str:
    """Render a value safe for a single-line table row (strips newlines/tabs)."""
    if value is None:
        return "null"
    text = str(value).replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return text[:max_len]


def urgency_icon(value: Any) -> str:
    return _URGENCY_ICONS.get(str(value or "unknown").strip().lower(), "⚪")


def entries(payload: Any) -> list[dict]:
    """Return the ``entry`` list from a Splunk REST JSON response.

    Splunk wraps collection responses in ``{"entry": [{...}], "paging": {...}}``.
    Returns ``[]`` for any other shape so callers never crash on an unexpected
    body.
    """
    if isinstance(payload, dict):
        items = payload.get("entry")
        if isinstance(items, list):
            return [e for e in items if isinstance(e, dict)]
    return []


def content(entry: dict) -> dict:
    """Return the ``content`` dict from a single Splunk REST ``entry``."""
    if isinstance(entry, dict):
        c = entry.get("content")
        if isinstance(c, dict):
            return c
    return {}


def format_results_table(results: list[dict], *, max_rows: int = 50) -> str:
    """Render Splunk search ``results`` rows as a compact pipe-delimited table."""
    rows = [r for r in (results or []) if isinstance(r, dict)]
    if not rows:
        return "Search completed: 0 results."
    # Stable column order: union of keys in first-seen order, with internal
    # (_underscore) fields moved to the end. Build a first-seen index map and
    # sort on it instead of calling ``columns.index(k)`` inside the key: that
    # lookup would be O(n) per element, and the key must not read the list being
    # sorted (CPython temporarily blanks it for the duration of ``list.sort``,
    # so an in-key ``columns.index`` would raise "not in list").
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    order = {name: i for i, name in enumerate(columns)}
    columns.sort(key=lambda k: (k.startswith("_"), order[k]))
    shown = rows[:max_rows]
    header = " | ".join(columns)
    lines = [f"Search returned {len(rows)} result(s) (showing {len(shown)}):", header]
    lines.append("-" * min(len(header), 120))
    for row in shown:
        lines.append(" | ".join(cell(row.get(c)) for c in columns))
    if len(rows) > len(shown):
        lines.append(f"\n... [{len(rows) - len(shown)} more rows not shown; raise limit]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Auth + URL helpers (shared with integration_testing.py)
# ---------------------------------------------------------------------------


def splunk_api_base(base_url: str) -> str:
    """Return the Splunk management REST base URL (host, no trailing slash).

    Surrounding whitespace is trimmed (a pasted URL often carries trailing
    spaces) so a request can never carry an illegal leading/trailing space.
    """
    return (base_url or "").strip().rstrip("/")


def build_splunk_headers(username: str, secret: str) -> dict[str, str]:
    """Build Splunk request headers.

    HTTP Basic (``username:secret``) when a username is configured (on-prem
    Splunk Enterprise), otherwise ``Authorization: Bearer <secret>`` using a
    Splunk authentication token (recommended; Splunk Cloud + ES).
    """
    username = (username or "").strip()
    secret = (secret or "").strip()
    headers = {"Accept": "application/json"}
    if username:
        raw = f"{username}:{secret}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
    else:
        headers["Authorization"] = f"Bearer {secret}"
    return headers


# ---------------------------------------------------------------------------
# Authenticated request helper factory
# ---------------------------------------------------------------------------


def make_api(base_url: str, username: str, secret: str) -> ApiFn:
    """Return an authenticated request helper bound to one Splunk integration.

    The returned coroutine ``_api(method, path, action, *, params, data,
    json_body, timeout)`` routes every call through the shared ``safe_request``
    (standardized errors + automatic 429 retry), always requests
    ``output_mode=json``, and returns ``(parsed_json, error)``. It short-circuits
    with :data:`CREDS_ERROR` when no secret is configured, so a missing
    credential can never build an illegal ``Authorization`` header.

    ``data`` sends a form-encoded body (Splunk's search/notable/threat-intel
    endpoints), ``json_body`` sends a JSON body (the KV Store data endpoints).
    """
    username = (username or "").strip()
    secret = (secret or "").strip()

    async def _api(
        method: str,
        path: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        json_body: Any | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> tuple[Any, str | None]:
        if not secret:
            return None, CREDS_ERROR
        headers = build_splunk_headers(username, secret)
        # output_mode=json is authoritative (we parse the response as JSON), so it
        # is merged LAST and a caller-supplied params can never override it.
        merged = {**(params or {}), "output_mode": "json"}
        async with egress_client(timeout=timeout) as client:
            resp, err = await safe_request(
                client,
                method,
                f"{base_url}{path}",
                PROVIDER,
                action,
                headers=headers,
                params=merged,
                data=data,
                json=json_body,
            )
            if err:
                return None, err
            try:
                return resp.json(), None
            except Exception:
                # Empty body (e.g. 204, or an empty 200 from a mutation) is a
                # legitimate "no JSON" success. A non-empty body that won't parse
                # (e.g. an HTML auth page behind a 200) is a real failure.
                if not (resp.content or b"").strip():
                    return {}, None
                return None, (
                    f"Error: {PROVIDER} returned a non-JSON response while trying "
                    f"to {action} (HTTP {resp.status_code}). The base URL may point "
                    f"at a non-API endpoint (use the management port, e.g. :8089)."
                )

    return _api


# ---------------------------------------------------------------------------
# Search Job lifecycle (create -> poll -> fetch), shared by the search and
# notable-event tools. ES notable events are read by running SPL over the
# ``notable`` index, so the notable module reuses the same engine.
# ---------------------------------------------------------------------------


def normalize_spl(query: str) -> str:
    """Prefix a bare search with ``search `` (matches Splunk CLI semantics).

    SPL must start with a generating command. A query that already starts with a
    pipe (``| tstats ...``), a macro (`` `notable` ``), or the ``search`` command
    is left untouched; anything else (e.g. ``index=main error``) is prefixed so
    the agent can pass natural search fragments.
    """
    q = (query or "").strip()
    if q.startswith("|") or q.startswith("`") or q.lower().startswith("search "):
        return q
    return f"search {q}"


async def fetch_job_results(
    api: ApiFn, sid: str, limit: int
) -> tuple[list[dict] | None, str | None]:
    """Fetch the result rows of a finished search job (v2 results endpoint)."""
    data, err = await api(
        "get",
        f"/services/search/v2/jobs/{quote(sid, safe='')}/results",
        f"fetch search results {sid}",
        params={"count": limit},
        timeout=SEARCH_TIMEOUT,
    )
    if err:
        return None, err
    results = (data or {}).get("results") if isinstance(data, dict) else None
    return ([r for r in (results or []) if isinstance(r, dict)], None)


async def wait_for_job(
    api: ApiFn, sid: str, limit: int
) -> tuple[list[dict] | None, str | None]:
    """Poll a dispatched search job until done, then return its result rows.

    Returns ``(results, None)`` on completion, or ``(None, message)`` when the
    job failed or is still running after the bounded polling window (the message
    carries the sid so the caller can retry with ``get_splunk_search_results``).
    """
    if not sid:
        return None, "Error: Splunk did not return a search job id (sid)."
    state = ""
    done = False
    for _ in range(SEARCH_MAX_POLLS):
        await asyncio.sleep(SEARCH_POLL_INTERVAL)
        poll, perr = await api(
            "get",
            f"/services/search/jobs/{quote(sid, safe='')}",
            f"poll search job {sid}",
            timeout=SEARCH_TIMEOUT,
        )
        if perr:
            return None, perr
        ents = entries(poll)
        c = content(ents[0]) if ents else {}
        state = str(c.get("dispatchState", ""))
        done = bool(c.get("isDone"))
        if done or state in ("FAILED", "DONE"):
            break
    if state == "FAILED":
        return None, f"Error: Splunk search job {sid} failed (dispatchState FAILED)."
    if not done and state != "DONE":
        return None, (
            f"Splunk search job {sid} is still '{state or 'running'}' after the "
            f"polling window. Narrow the time range or simplify the SPL, then fetch "
            f"later with get_splunk_search_results (sid={sid})."
        )
    return await fetch_job_results(api, sid, limit)


async def execute_search(
    api: ApiFn,
    spl: str,
    earliest_time: str,
    latest_time: str,
    limit: int,
) -> tuple[list[dict] | None, str | None]:
    """Create a search job for *spl*, wait for it, and return its result rows."""
    create, err = await api(
        "post",
        "/services/search/jobs",
        "create search job",
        data={
            "search": normalize_spl(spl),
            "earliest_time": (earliest_time or DEFAULT_SEARCH_EARLIEST).strip(),
            "latest_time": (latest_time or DEFAULT_SEARCH_LATEST).strip(),
        },
        timeout=SEARCH_TIMEOUT,
    )
    if err:
        return None, err
    sid = create.get("sid") if isinstance(create, dict) else None
    if not sid:
        return None, "Error: Splunk did not return a search job id (sid)."
    return await wait_for_job(api, sid, limit)


def spl_quote(value: str) -> str:
    """Escape a value for safe embedding inside an SPL double-quoted string.

    Backslashes and double quotes are the only characters that can break out of
    a ``field="..."`` term, so escaping them lets caller-supplied filters (owner
    names, search names) be embedded without enabling SPL injection.
    """
    return str(value).replace("\\", "\\\\").replace('"', '\\"')
