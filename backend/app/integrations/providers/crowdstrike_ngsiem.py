# SPDX-License-Identifier: Apache-2.0

"""CrowdStrike Falcon Next-Gen SIEM (NGSIEM) integration.

The SIEM half of CrowdStrike, kept apart from the Falcon endpoint provider
(``crowdstrike.py``) on purpose: a tenant may run NGSIEM with an API client of
its own, and the catalog lists it with the other SIEMs. It authenticates
exactly like the endpoint provider - a Falcon API client (client ID + secret)
and a cloud region, exchanged for a short-lived bearer token - and reuses that
module's cloud resolution and token exchange.

Tool categories (11 tools):
  - Search: run a CrowdStrike Query Language (CQL / LogScale) query over a
    repository and a time range through the query-job API (start, poll, stop
    on timeout), with bounded, partial-aware results
  - Correlation rules: list, read, and activate / deactivate a rule
  - Detections raised by NGSIEM correlation rules (Alerts API filtered on
    ``product:'ngsiem'``): list and read
  - Saved queries: list, and read one as its LogScale YAML template
  - Lookup files: list, download one as a file of the run, and upload a file
    of the run (e.g. an IOC list exported from OpenCTI)

API contract (checked against CrowdStrike's FalconPy SDK and the LogScale
query-job documentation):
  - ``POST/GET/DELETE /humio/api/v1/repositories/{repository}/queryjobs[/{id}]``
    (``NGSIEM: Read``). A job not polled for 90 seconds deletes itself.
  - ``GET/POST /humio/api/v1/repositories/{repository}/files[/{filename}]``
    (``NGSIEM: Read`` / ``NGSIEM: Write``)
  - ``GET /ngsiem-content/queries/{savedqueries,lookupfiles}/v1`` and
    ``GET /ngsiem-content/entities/savedqueries-template/v1`` (``NGSIEM: Read``)
  - ``GET /correlation-rules/combined/rules/v1``,
    ``GET/PATCH /correlation-rules/entities/rules/v1`` (``Correlation Rules``)
  - ``GET /alerts/queries/alerts/v2`` + ``POST /alerts/entities/alerts/v2``
    (``Alerts: Read``)

Everything a tenant or a log source wrote (events, rule names and queries,
saved-query templates, lookup-file names) is returned inside ``wrap_untrusted``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from app.core.egress import egress_client
from app.integrations.file_delivery import (
    PersistFileFn,
    deliver_file,
    fetch_file,
    read_pipe_file,
)
from app.integrations.http import (
    TRANSIENT_RETRY_STATUSES,
    format_network_error,
    parse_compound_token,
    safe_request,
)
from app.integrations.providers.crowdstrike import (
    request_falcon_token,
    resolve_base_url,
)
from app.services.content_safety import wrap_untrusted
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

__all__ = ["build_tools", "resolve_base_url", "validate_connection"]

_PROVIDER = "CrowdStrike Next-Gen SIEM"
_DEFAULT_BASE_URL = "https://api.crowdstrike.com"
_DEFAULT_TIMEOUT = 30
_TOKEN_SAFETY_WINDOW = 60

_AUTH_ERROR = (
    "Error: Failed to authenticate with CrowdStrike Next-Gen SIEM. Check that "
    "the API client ID and secret are correct and that the integration's cloud "
    "region matches your tenant (US-1 / US-2 / EU-1 / US-GOV-1 / US-GOV-2)."
)

# ---------------------------------------------------------------------------
# Search bounds
# ---------------------------------------------------------------------------

_DEFAULT_REPOSITORY = "search-all"
#: The repositories CrowdStrike documents for NGSIEM searches. Any other plain
#: name (a custom view) is accepted too once it passes ``_REPOSITORY_RE``.
_KNOWN_REPOSITORIES = ("search-all", "investigate_view", "third-party")
#: A repository or view name goes into the URL path: only a plain name, never a
#: path separator, a dot segment or a percent escape.
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
_MAX_QUERY_CHARS = 20_000
_DEFAULT_SEARCH_TIMEOUT = 60
_MIN_SEARCH_TIMEOUT = 10
#: Below the agent loop's 300 s per-tool ceiling, with room for the stop call.
_MAX_SEARCH_TIMEOUT = 240
_MIN_POLL_INTERVAL = 0.5
_MAX_POLL_INTERVAL = 5.0
_DEFAULT_EVENT_LIMIT = 50
_MAX_EVENT_LIMIT = 200
_MAX_EVENT_CHARS = 1_500
_MAX_RESULT_CHARS = 30_000
_STOP_TIMEOUT = 10
#: A poll answers with every event found so far: its body is read in a stream
#: and refused past this size, before it is parsed.
_MAX_POLL_BYTES = 10 * 1024 * 1024
#: A poll that fails with one of these (or on the network) is tried again on
#: the next poll while the search's time lasts.
_POLL_RETRY_STATUSES = frozenset({429, 502, 503, 504})
#: Under this many seconds left, no request is started.
_MIN_REQUEST_BUDGET = 1.0

#: Fields shown first in a rendered event, when present; every other field
#: follows in name order and ``@rawstring`` (often long) comes last.
_PRIORITY_FIELDS = (
    "@timestamp",
    "#event_simpleName",
    "event_simpleName",
    "#repo",
    "#Vendor",
    "ComputerName",
    "UserName",
    "aid",
    "FileName",
    "CommandLine",
    "SHA256HashData",
    "RemoteAddressIP4",
    "LocalAddressIP4",
    "DomainName",
)

#: The unit LogScale is sent for each accepted spelling (its documented forms).
_CANONICAL_UNITS = (
    (("ms", "milli"), "ms"),
    (("s",), "s"),
    (("m",), "m"),
    (("h",), "h"),
    (("d",), "d"),
    (("w",), "w"),
    (("y",), "y"),
)
_UNIT_MILLIS = {"ms": 1, "s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000,
                "w": 7 * 86_400_000, "y": 365 * 86_400_000}

_RELATIVE_TIME_RE = re.compile(
    r"^(\d{1,6})\s*(ms|millis|milliseconds?|s|secs?|seconds?|m|mins?|minutes?|"
    r"h|hrs?|hours?|d|days?|w|weeks?|y|years?)$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Content (saved queries, lookup files, correlation rules, detections)
# ---------------------------------------------------------------------------

_SAVED_QUERY_DOMAINS = ("all", "falcon", "third-party", "dashboards", "query-audit")
_LOOKUP_DOMAINS = (
    "all",
    "falcon",
    "third-party",
    "dashboards",
    "parsers-repository",
    "query-audit",
)
_RULE_SORTS = (
    "created_on",
    "created_on|desc",
    "last_updated_on",
    "last_updated_on|desc",
)
_RULE_STATUSES = ("active", "inactive")
_MAX_LIST_LIMIT = 100
_DEFAULT_LIST_LIMIT = 25
_MAX_TEMPLATE_CHARS = 8_000
_MAX_DETAIL_CHARS = 12_000

#: A lookup file name goes into the URL path and names the stored file.
_LOOKUP_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,199}$")
_LOOKUP_CONTENT_TYPES = {".csv": "text/csv", ".json": "application/json"}
#: CrowdStrike accepts up to 200 MB (CSV) / 100 MB (JSON); the platform holds
#: a file in memory to move it, so it moves at most this much.
_MAX_LOOKUP_BYTES = 50 * 1024 * 1024
_TRANSFER_TIMEOUT = 120


# ---------------------------------------------------------------------------
# Connectivity
# ---------------------------------------------------------------------------


async def validate_connection(
    base_url: str, client_id: str, client_secret: str
) -> tuple[bool, str]:
    """Verify the API client AND its NGSIEM access.

    A token exchange alone proves the client ID and secret, not that the client
    may read NGSIEM: the probe lists one saved query, which needs
    ``NGSIEM: Read`` on a tenant with Next-Gen SIEM enabled.
    """
    if not client_id or not client_secret:
        return False, "Missing API client ID or secret."
    token, _ = await request_falcon_token(base_url, client_id, client_secret)
    if not token:
        return False, (
            "Authentication failed - check the client ID/secret and confirm the "
            "cloud region matches your Falcon tenant."
        )
    try:
        async with egress_client(timeout=20) as client:
            resp = await client.get(
                f"{base_url}/ngsiem-content/queries/savedqueries/v1",
                params={"limit": 1},
                headers=_headers(token),
            )
    except Exception as exc:
        logger.warning("NGSIEM connectivity probe failed: %s", format_network_error(exc, _PROVIDER, "probe"))
        return False, "Authenticated, but the Next-Gen SIEM API could not be reached."
    if resp.is_success:
        return True, "CrowdStrike Next-Gen SIEM is connected and the API client can read it."
    if resp.status_code in (401, 403):
        return False, (
            "Authenticated, but the API client cannot read Next-Gen SIEM: grant it "
            "the 'NGSIEM' Read scope, and check that Next-Gen SIEM is enabled on "
            "this tenant."
        )
    logger.warning("NGSIEM connectivity probe answered HTTP %s", resp.status_code)
    return False, (
        f"Authenticated, but the Next-Gen SIEM API answered HTTP {resp.status_code}."
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _fql_quote(value: str) -> str:
    """Escape a value for an FQL single-quoted literal (``field:'value'``)."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _trunc(text: Any, max_len: int) -> str:
    if text is None:
        return ""
    text = str(text)
    return text if len(text) <= max_len else text[:max_len] + "…"


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def check_repository(value: str) -> tuple[str | None, str | None]:
    """``(repository, error)`` for a repository or view name used in a URL path."""
    name = (value or "").strip() or _DEFAULT_REPOSITORY
    if not _REPOSITORY_RE.fullmatch(name):
        return None, (
            "Error: repository must be a plain repository or view name (letters, "
            "digits, '_' and '-'), such as "
            + ", ".join(f"'{r}'" for r in _KNOWN_REPOSITORIES)
            + "."
        )
    return name, None


def check_lookup_filename(value: str) -> tuple[str | None, str | None]:
    """``(filename, error)`` for a lookup file name used in a URL path."""
    name = (value or "").strip()
    if not name:
        return None, "Error: filename is required (for example 'bad_domains.csv')."
    if ".." in name or not _LOOKUP_FILENAME_RE.fullmatch(name):
        return None, (
            "Error: filename must be a plain file name (letters, digits, spaces, "
            "'.', '_' and '-'), with no path."
        )
    return name, None


def resolve_time(value: Any, field: str) -> tuple[str | int | None, str | None]:
    """Read a search bound: relative (``24h``, ``7d``), ``now``, epoch or ISO 8601.

    Returns ``(value_to_send, error)``: a relative time is sent as written
    (LogScale reads it), an absolute one as epoch milliseconds. ``None`` when
    the bound was not given.
    """
    raw = str(value if value is not None else "").strip()
    if not raw:
        return None, None
    if raw.lower() == "now":
        return "now", None
    if raw.isdigit():
        if len(raw) == 13:
            return int(raw), None
        if len(raw) == 10:
            return int(raw) * 1000, None
        return None, (
            f"Error: {field} '{raw}' is not a Unix timestamp in seconds (10 "
            "digits) or milliseconds (13 digits)."
        )
    match = _RELATIVE_TIME_RE.fullmatch(raw)
    if match:
        return f"{int(match.group(1))}{_canonical_unit(match.group(2))}", None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None, (
            f"Error: {field} '{_trunc(raw, 60)}' is not a time CrowdStrike reads: use "
            "a relative time ('15m', '24h', '7d'), 'now', an ISO 8601 date "
            "('2026-10-01T08:00:00Z') or a Unix timestamp."
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000), None


def _canonical_unit(unit: str) -> str:
    """``mins`` -> ``m``, ``hours`` -> ``h``: the form LogScale documents."""
    unit = unit.lower()
    for prefixes, canonical in _CANONICAL_UNITS:
        if unit.startswith(prefixes):
            return canonical
    return unit


def _relative_ms(value: str | int) -> int | None:
    """Milliseconds back from now of a resolved bound, for ordering checks."""
    if isinstance(value, int):
        return int(time.time() * 1000) - value
    if value == "now":
        return 0
    match = _RELATIVE_TIME_RE.fullmatch(value)
    if not match:
        return None
    return int(match.group(1)) * _UNIT_MILLIS[_canonical_unit(match.group(2))]


def _format_timestamp(value: Any) -> Any:
    """An epoch-millisecond ``@timestamp`` rendered as ISO 8601 (UTC)."""
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return value
    try:
        return datetime.fromtimestamp(millis / 1000, tz=UTC).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return value


def render_event(event: Any) -> str:
    """One event as compact JSON, priority fields first, bounded."""
    if not isinstance(event, dict):
        return _trunc(json.dumps(event, ensure_ascii=False, default=str), _MAX_EVENT_CHARS)
    ordered: dict[str, Any] = {}
    for key in _PRIORITY_FIELDS:
        if key in event:
            ordered[key] = event[key]
    for key in sorted(k for k in event if k not in ordered and k != "@rawstring"):
        ordered[key] = event[key]
    if "@rawstring" in event:
        ordered["@rawstring"] = event["@rawstring"]
    if "@timestamp" in ordered:
        ordered["@timestamp"] = _format_timestamp(ordered["@timestamp"])
    return _trunc(json.dumps(ordered, ensure_ascii=False, default=str), _MAX_EVENT_CHARS)


def _envelope_errors(data: Any) -> str:
    """The messages of a Falcon ``errors`` array, if any."""
    if not isinstance(data, dict):
        return ""
    errors = data.get("errors") or []
    messages = []
    for error in errors if isinstance(errors, list) else [errors]:
        if isinstance(error, dict):
            messages.append(str(error.get("message") or error.get("code") or error))
        elif error:
            messages.append(str(error))
    return _trunc("; ".join(messages), 400)


def _resources(data: Any) -> list:
    if not isinstance(data, dict):
        return []
    resources = data.get("resources")
    if isinstance(resources, list):
        return resources
    if isinstance(resources, dict):
        return [resources]
    return []


def _pagination_hint(data: Any, offset: int, shown: int) -> str:
    """``[More results ...]`` when the Falcon envelope says there are more."""
    meta = data.get("meta") if isinstance(data, dict) else None
    pagination = meta.get("pagination") if isinstance(meta, dict) else None
    if not isinstance(pagination, dict):
        return ""
    try:
        total = int(pagination.get("total"))
    except (TypeError, ValueError):
        return ""
    if offset + shown >= total:
        return ""
    return f"\n[{total} in all - pass offset={offset + shown} for the next page]"


def _job_body(data: Any) -> dict:
    """A LogScale query-job answer, with or without the Falcon envelope."""
    if not isinstance(data, dict):
        return {}
    if "done" in data or "events" in data or "id" in data:
        return data
    resources = data.get("resources")
    if isinstance(resources, dict):
        return resources
    if isinstance(resources, list) and resources and isinstance(resources[0], dict):
        return resources[0]
    return data


def _job_id(data: Any) -> str:
    body = _job_body(data)
    job_id = body.get("id") if isinstance(body, dict) else None
    if not job_id and isinstance(data, dict):
        resources = data.get("resources")
        if isinstance(resources, list) and resources and isinstance(resources[0], str):
            job_id = resources[0]
    return str(job_id or "").strip()


def _content_line(item: Any) -> str:
    """One saved query / lookup file of a content listing."""
    if not isinstance(item, dict):
        return f"  - {_trunc(item, 300)}"
    name = item.get("name") or item.get("filename") or item.get("display_name")
    ident = item.get("id") or item.get("filename")
    parts = [f"  - {_trunc(name or ident or '?', 200)}"]
    if ident and ident != name:
        parts.append(f"(id: {_trunc(ident, 120)})")
    details = []
    for key in ("search_domain", "repository", "description", "labels"):
        value = item.get(key)
        if value:
            details.append(f"{key}: {_trunc(value, 200)}")
    for key in ("last_modified", "last_updated", "updated_at", "modified_at"):
        if item.get(key):
            details.append(f"updated: {_trunc(item[key], 40)}")
            break
    line = " ".join(parts)
    if details:
        line += "\n    " + " | ".join(details)
    return line


def _rule_summary(rule: dict) -> str:
    search = rule.get("search") if isinstance(rule.get("search"), dict) else {}
    operation = rule.get("operation") if isinstance(rule.get("operation"), dict) else {}
    schedule = operation.get("schedule") if isinstance(operation.get("schedule"), dict) else {}
    lines = [
        f"  - {_trunc(rule.get('name') or 'Unnamed rule', 200)} "
        f"[{rule.get('status', '?')}] severity {rule.get('severity', '?')}",
        f"    id: {rule.get('id', '?')} | updated: {_trunc(rule.get('last_updated_on'), 25)}",
    ]
    meta = []
    if search.get("lookback"):
        meta.append(f"lookback: {_trunc(search['lookback'], 40)}")
    if search.get("outcome"):
        meta.append(f"outcome: {_trunc(search['outcome'], 40)}")
    if schedule.get("definition"):
        meta.append(f"schedule: {_trunc(schedule['definition'], 60)}")
    if meta:
        lines.append("    " + " | ".join(meta))
    if search.get("filter"):
        lines.append(f"    query: {_trunc(search['filter'], 300)}")
    return "\n".join(lines)


def _detection_summary(alert: dict) -> str:
    severity = alert.get("severity_name") or alert.get("severity") or "?"
    title = alert.get("display_name") or alert.get("name") or "Unnamed detection"
    line = (
        f"  - [{severity}] {alert.get('status', '?')} - {_trunc(title, 200)}\n"
        f"    id: {alert.get('composite_id') or alert.get('id', '?')}\n"
        f"    Created: {_trunc(alert.get('created_timestamp'), 19)}"
    )
    tactic = alert.get("tactic")
    technique = alert.get("technique")
    if tactic or technique:
        line += f" | Tactic: {tactic or 'N/A'} | Technique: {technique or 'N/A'}"
    if alert.get("assigned_to_name"):
        line += f" | Assigned: {alert['assigned_to_name']}"
    return line


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    persist_file_fn: PersistFileFn | None = None,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build the CrowdStrike Next-Gen SIEM tools.

    ``token`` is the compound ``base_url||client_id||client_secret`` assembled
    by ``integration_tools._get_token``, the same shape as the Falcon endpoint
    provider's. ``persist_file_fn`` stores a downloaded lookup file as a file
    of the run.
    """
    # The secret is the last field and is read whole, even if it holds '||'.
    base_url, client_id, client_secret = parse_compound_token(token, 3, greedy_last=True)
    base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")

    _token_state: dict[str, Any] = {"token": None, "exp": 0.0}

    async def _token() -> str | None:
        now = time.monotonic()
        if _token_state["token"] and _token_state["exp"] > now:
            return _token_state["token"]
        if not (client_id and client_secret):
            return None
        access, ttl = await request_falcon_token(base_url, client_id, client_secret)
        if access:
            _token_state["token"] = access
            _token_state["exp"] = now + max(ttl - _TOKEN_SAFETY_WINDOW, 30)
        return access

    async def _api(
        method: str,
        path: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
        client: Any | None = None,
        expected_statuses: set[int] | None = None,
        max_total_seconds: float | None = None,
    ) -> tuple[Any, str | None]:
        """Authenticated JSON request returning ``(json, error)``.

        ``client`` reuses an open client (the search's); ``max_total_seconds``
        bounds the retries ``safe_request`` may make.
        """
        access_token = await _token()
        if not access_token:
            return None, _AUTH_ERROR
        kwargs: dict[str, Any] = {"headers": _headers(access_token)}
        if params:
            kwargs["params"] = params
        if json_body is not None:
            kwargs["json"] = json_body
        if expected_statuses:
            kwargs["expected_statuses"] = expected_statuses
        if max_total_seconds is not None:
            kwargs["max_total_seconds"] = max_total_seconds
        if method == "get":
            # Reads only: a write must never be sent twice.
            kwargs["retry_statuses"] = TRANSIENT_RETRY_STATUSES
        url = f"{base_url}{path}"
        if client is not None:
            resp, err = await safe_request(client, method, url, _PROVIDER, action, **kwargs)
        else:
            async with egress_client(timeout=timeout) as own_client:
                resp, err = await safe_request(
                    own_client, method, url, _PROVIDER, action, **kwargs
                )
        if err:
            return None, err
        try:
            data = resp.json()
        except ValueError:
            return {}, None
        envelope = _envelope_errors(data)
        if envelope and not _resources(data) and not _job_body(data).get("events"):
            return None, f"Error: {_PROVIDER} refused to {action}: {envelope}"
        return data, None

    async def _stop_search(client: Any, repository: str, job_id: str) -> None:
        """Best effort, bounded as a whole: an unpolled job deletes itself after 90 s."""
        path = f"/humio/api/v1/repositories/{repository}/queryjobs/{quote(job_id, safe='')}"
        try:
            await asyncio.wait_for(
                _api(
                    "delete",
                    path,
                    "stop search",
                    client=client,
                    # A job that already deleted itself is not a failure.
                    expected_statuses={404},
                    max_total_seconds=_STOP_TIMEOUT,
                ),
                _STOP_TIMEOUT,
            )
        except Exception:
            logger.debug("Could not stop NGSIEM query job", exc_info=True)

    async def _poll(client: Any, path: str) -> tuple[dict | None, str | None, bool]:
        """One poll: ``(job, error, transient)``, its body read under ``_MAX_POLL_BYTES``."""
        access_token = await _token()
        if not access_token:
            return None, _AUTH_ERROR, False
        fetched = await fetch_file(
            client,
            "get",
            f"{base_url}{path}",
            provider=_PROVIDER,
            action="poll search",
            max_bytes=_MAX_POLL_BYTES,
            headers=_headers(access_token),
        )
        if isinstance(fetched, str):
            if getattr(fetched, "too_large", False):
                return None, (
                    f"Error: the search's results are over {_MAX_POLL_BYTES // 1024 // 1024} MB. "
                    "Keep fewer fields with `| select([...])`, cap the rows with "
                    "`| head(N)`, aggregate with `| groupBy(...)`, or narrow the time range."
                ), False
            status = getattr(fetched, "status", None)
            return None, str(fetched), status is None or status in _POLL_RETRY_STATUSES
        try:
            data = json.loads(fetched.data)
        except ValueError:
            return None, f"Error: {_PROVIDER} answered the search poll with a body that is not JSON.", False
        envelope = _envelope_errors(data)
        job = _job_body(data)
        if envelope and not job.get("events") and "done" not in job:
            return None, f"Error: {_PROVIDER} refused to poll the search: {envelope}", False
        return job, None, False

    # ── Search ────────────────────────────────────────────────────────────

    async def search_crowdstrike_ngsiem(
        query: str,
        repository: str = _DEFAULT_REPOSITORY,
        start: str = "24h",
        end: str = "",
        limit: int = _DEFAULT_EVENT_LIMIT,
        timeout_seconds: int = _DEFAULT_SEARCH_TIMEOUT,
    ) -> str:
        """Run a CQL query and return the matching events (bounded)."""
        query = str(query or "").strip()
        if not query:
            return "Error: query is required (a CrowdStrike Query Language query)."
        if len(query) > _MAX_QUERY_CHARS:
            return f"Error: query is over {_MAX_QUERY_CHARS} characters."
        repo, err = check_repository(repository)
        if err:
            return err
        start_value, err = resolve_time(start or "24h", "start")
        if err:
            return err
        end_value, err = resolve_time(end, "end")
        if err:
            return err
        start_back = _relative_ms(start_value)
        end_back = _relative_ms(end_value) if end_value is not None else 0
        if start_back is not None and end_back is not None and start_back <= end_back:
            return "Error: start must be earlier than end."
        limit = _clamp_int(limit, _DEFAULT_EVENT_LIMIT, 1, _MAX_EVENT_LIMIT)
        timeout = _clamp_int(
            timeout_seconds, _DEFAULT_SEARCH_TIMEOUT, _MIN_SEARCH_TIMEOUT, _MAX_SEARCH_TIMEOUT
        )

        body: dict[str, Any] = {"queryString": query, "start": start_value, "isLive": False}
        if end_value is not None:
            body["end"] = end_value
        jobs_path = f"/humio/api/v1/repositories/{repo}/queryjobs"
        # timeout_seconds is a hard deadline for the start and every poll,
        # retries included; only the bounded stop call runs past it.
        deadline = time.monotonic() + timeout
        async with egress_client(timeout=_DEFAULT_TIMEOUT) as client:
            try:
                created, err = await asyncio.wait_for(
                    _api(
                        "post",
                        jobs_path,
                        "start search",
                        json_body=body,
                        client=client,
                        max_total_seconds=timeout,
                    ),
                    timeout,
                )
            except TimeoutError:
                return (
                    f"Error: {_PROVIDER} did not accept the search within {timeout} s. "
                    "Retry later or raise timeout_seconds."
                )
            if err:
                return err
            job_id = _job_id(created)
            if not job_id:
                return f"Error: {_PROVIDER} accepted the search but returned no job id."

            job_path = f"{jobs_path}/{quote(job_id, safe='')}"
            result: dict = {}
            done = False
            last_error: str | None = None
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining < _MIN_REQUEST_BUDGET:
                        break
                    try:
                        job, err, transient = await asyncio.wait_for(
                            _poll(client, job_path), remaining
                        )
                    except TimeoutError:
                        break
                    wait = 1.0
                    if err:
                        if not transient:
                            return err
                        last_error = err
                    else:
                        last_error = None
                        result = job or {}
                        if result.get("cancelled"):
                            return (
                                f"Error: {_PROVIDER} cancelled the search before it "
                                "finished (it may have exceeded the tenant's query "
                                "limits). Narrow the time range or the query and retry."
                            )
                        if result.get("done"):
                            done = True
                            break
                        meta = (
                            result.get("metaData")
                            if isinstance(result.get("metaData"), dict)
                            else {}
                        )
                        try:
                            wait = float(meta.get("pollAfter") or 1000) / 1000
                        except (TypeError, ValueError):
                            wait = 1.0
                    wait = max(_MIN_POLL_INTERVAL, min(_MAX_POLL_INTERVAL, wait))
                    if time.monotonic() + wait >= deadline:
                        break
                    await asyncio.sleep(wait)
            finally:
                if not done:
                    await _stop_search(client, repo, job_id)

        if not done and not result and last_error:
            # Every poll failed: say why rather than report an empty search.
            return last_error
        return _format_search(result, done=done, repository=repo, start=start_value,
                              end=end_value, limit=limit, timeout=timeout)

    def _format_search(
        result: dict,
        *,
        done: bool,
        repository: str,
        start: Any,
        end: Any,
        limit: int,
        timeout: int,
    ) -> str:
        events = result.get("events") if isinstance(result.get("events"), list) else []
        meta = result.get("metaData") if isinstance(result.get("metaData"), dict) else {}
        span = f"repository: {repository}, start: {start}, end: {end if end is not None else 'now'}"
        if done:
            header = f"NGSIEM search ({span}) - complete."
        else:
            progress = ""
            try:
                work_done = float(meta.get("workDone"))
                total_work = float(meta.get("totalWork"))
                if total_work > 0:
                    progress = f", about {round(100 * work_done / total_work)}% of the data searched"
            except (TypeError, ValueError):
                pass
            header = (
                f"NGSIEM search ({span}) - PARTIAL: it did not finish within {timeout} s"
                f"{progress}, so it was stopped. The events below are what it had "
                "found so far (newest data is searched first); raise timeout_seconds, "
                "narrow the time range or the query for a complete answer."
            )
        counts = [f"returned: {len(events)}"]
        if meta.get("eventCount") is not None:
            counts.insert(0, f"matched: {meta['eventCount']}")
        if meta.get("isAggregate"):
            counts.append("aggregate query (rows, not raw events)")
        lines = [header, " | ".join(counts)]
        # LogScale reports warnings in metaData (a capped result buffer among
        # them); some answers carry them at the top level too.
        warnings = [
            w
            for source in (result.get("warnings"), meta.get("warnings"))
            if isinstance(source, list)
            for w in source
        ]
        warning_text = ""
        if warnings:
            rendered: list[str] = []
            for warning in warnings:
                if isinstance(warning, dict):
                    warning = warning.get("message") or warning
                line = _trunc(warning, 300)
                if line not in rendered:
                    rendered.append(line)
            rendered = rendered[:5]
            warning_text = "Warnings from the search engine:\n" + "\n".join(
                f"- {w}" for w in rendered
            )
        if not events:
            lines.append("No events matched.")
            if warning_text:
                lines.append(wrap_untrusted(warning_text, f"{_PROVIDER} search warnings"))
            return "\n".join(lines)

        shown: list[str] = []
        budget = _MAX_RESULT_CHARS
        for event in events[:limit]:
            rendered = render_event(event)
            if len(rendered) + 1 > budget:
                break
            shown.append(rendered)
            budget -= len(rendered) + 1
        block = "\n".join(shown)
        if warning_text:
            block = warning_text + "\n\n" + block
        lines.append(f"Showing {len(shown)} of {len(events)} returned events:")
        lines.append(wrap_untrusted(block, f"{_PROVIDER} events ({repository})"))
        hidden = len(events) - len(shown)
        if hidden > 0:
            lines.append(
                f"[{hidden} more returned events not shown - narrow the query, select "
                "fields with `| select([...])`, cap it with `| head(N)`, or aggregate "
                "with `| groupBy(...)`.]"
            )
        return "\n".join(lines)

    # ── Correlation rules ────────────────────────────────────────────────

    async def list_crowdstrike_ngsiem_correlation_rules(
        status: str = "",
        name: str = "",
        filter_query: str = "",
        sort: str = "last_updated_on|desc",
        limit: int = _DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> str:
        """List NGSIEM correlation rules with their query, schedule and status."""
        clauses: list[str] = []
        status = (status or "").strip().lower()
        if status:
            if status not in _RULE_STATUSES:
                return "Error: status must be 'active' or 'inactive'."
            clauses.append(f"status:'{status}'")
        if name:
            clauses.append(f"name:~'{_fql_quote(str(name).strip())}'")
        if filter_query:
            clauses.append(f"({str(filter_query).strip()})")
        sort = (sort or "").strip() or "last_updated_on|desc"
        if sort not in _RULE_SORTS:
            return "Error: sort must be one of " + ", ".join(_RULE_SORTS) + "."
        limit = _clamp_int(limit, _DEFAULT_LIST_LIMIT, 1, _MAX_LIST_LIMIT)
        offset = _clamp_int(offset, 0, 0, 100_000)
        params: dict[str, Any] = {"sort": sort, "limit": limit, "offset": offset}
        if clauses:
            params["filter"] = "+".join(clauses)
        data, err = await _api(
            "get", "/correlation-rules/combined/rules/v1", "list correlation rules", params=params
        )
        if err:
            return err
        rules = [r for r in _resources(data) if isinstance(r, dict)]
        if not rules:
            return "No correlation rules matched."
        body = "\n".join(_rule_summary(r) for r in rules)
        return (
            f"Found {len(rules)} correlation rule{'s' if len(rules) != 1 else ''}:\n"
            + wrap_untrusted(body, f"{_PROVIDER} correlation rules")
            + _pagination_hint(data, offset, len(rules))
        )

    async def get_crowdstrike_ngsiem_correlation_rule(rule_id: str) -> str:
        """Read one correlation rule in full: query, schedule, outcome, ATT&CK."""
        rule_id = str(rule_id or "").strip()
        if not rule_id:
            return "Error: rule_id is required (from list_crowdstrike_ngsiem_correlation_rules)."
        data, err = await _api(
            "get",
            "/correlation-rules/entities/rules/v1",
            f"get correlation rule {_trunc(rule_id, 80)}",
            params={"ids": rule_id},
        )
        if err:
            return err
        rules = [r for r in _resources(data) if isinstance(r, dict)]
        if not rules:
            return f"Error: correlation rule '{_trunc(rule_id, 80)}' was not found."
        detail = _trunc(json.dumps(rules[0], ensure_ascii=False, indent=2, default=str), _MAX_DETAIL_CHARS)
        return "Correlation rule:\n" + wrap_untrusted(detail, f"{_PROVIDER} correlation rule")

    async def set_crowdstrike_ngsiem_correlation_rule_status(
        rule_id: str, status: str, comment: str = ""
    ) -> str:
        """Activate or deactivate a correlation rule."""
        rule_id = str(rule_id or "").strip()
        status = str(status or "").strip().lower()
        if not rule_id:
            return "Error: rule_id is required (from list_crowdstrike_ngsiem_correlation_rules)."
        if status not in _RULE_STATUSES:
            return "Error: status must be 'active' or 'inactive'."
        entry: dict[str, Any] = {"id": rule_id, "status": status}
        comment = str(comment or "").strip()
        if comment:
            entry["comment"] = _trunc(comment, 1000)
        # The PATCH body is a LIST of rule updates (FalconPy issue 1294).
        data, err = await _api(
            "patch",
            "/correlation-rules/entities/rules/v1",
            f"set correlation rule {_trunc(rule_id, 80)} {status}",
            json_body=[entry],
        )
        if err:
            return err
        envelope = _envelope_errors(data)
        if envelope:
            return f"Error: {_PROVIDER} refused to update the rule: {envelope}"
        updated = [r for r in _resources(data) if isinstance(r, dict)]
        if updated and str(updated[0].get("status", status)).lower() != status:
            return (
                f"Error: {_PROVIDER} answered, but the rule's status is "
                f"'{updated[0].get('status')}', not '{status}'."
            )
        return f"Correlation rule {rule_id} is now {status}."

    # ── Detections ────────────────────────────────────────────────────────

    async def list_crowdstrike_ngsiem_detections(
        status: str = "",
        severity: str = "",
        filter_query: str = "",
        limit: int = _DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> str:
        """List detections raised by NGSIEM correlation rules (Alerts API)."""
        clauses = ["product:'ngsiem'"]
        if status:
            clauses.append(f"status:'{_fql_quote(str(status).strip())}'")
        if severity:
            clauses.append(f"severity_name:'{_fql_quote(str(severity).strip())}'")
        if filter_query:
            clauses.append(f"({str(filter_query).strip()})")
        limit = _clamp_int(limit, _DEFAULT_LIST_LIMIT, 1, _MAX_LIST_LIMIT)
        offset = _clamp_int(offset, 0, 0, 100_000)
        params: dict[str, Any] = {
            "filter": "+".join(clauses),
            "limit": limit,
            "sort": "created_timestamp|desc",
        }
        if offset:
            params["offset"] = offset
        data, err = await _api(
            "get", "/alerts/queries/alerts/v2", "list NGSIEM detections", params=params
        )
        if err:
            return err
        ids = [i for i in _resources(data) if isinstance(i, str)]
        if not ids:
            return "No NGSIEM detections matched."
        detail, err = await _api(
            "post",
            "/alerts/entities/alerts/v2",
            "get NGSIEM detection details",
            json_body={"composite_ids": ids[:limit]},
        )
        if err:
            return err
        alerts = [a for a in _resources(detail) if isinstance(a, dict)]
        if not alerts:
            return "No NGSIEM detections matched."
        body = "\n".join(_detection_summary(a) for a in alerts)
        return (
            f"Found {len(alerts)} NGSIEM detection{'s' if len(alerts) != 1 else ''}:\n"
            + wrap_untrusted(body, f"{_PROVIDER} detections")
            + _pagination_hint(data, offset, len(ids))
        )

    async def get_crowdstrike_ngsiem_detection(detection_id: str) -> str:
        """Read one NGSIEM detection in full by its composite id."""
        detection_id = str(detection_id or "").strip()
        if not detection_id:
            return "Error: detection_id is required (from list_crowdstrike_ngsiem_detections)."
        data, err = await _api(
            "post",
            "/alerts/entities/alerts/v2",
            "get NGSIEM detection",
            json_body={"composite_ids": [detection_id]},
        )
        if err:
            return err
        alerts = [a for a in _resources(data) if isinstance(a, dict)]
        if not alerts:
            return f"Error: detection '{_trunc(detection_id, 120)}' was not found."
        detail = _trunc(json.dumps(alerts[0], ensure_ascii=False, indent=2, default=str), _MAX_DETAIL_CHARS)
        return "NGSIEM detection:\n" + wrap_untrusted(detail, f"{_PROVIDER} detection")

    # ── Saved queries ─────────────────────────────────────────────────────

    def _content_params(
        name: str, search_domain: str, domains: tuple[str, ...], limit: int, offset: int
    ) -> tuple[dict[str, Any] | None, str | None]:
        params: dict[str, Any] = {
            "limit": _clamp_int(limit, _DEFAULT_LIST_LIMIT, 1, _MAX_LIST_LIMIT),
            "offset": _clamp_int(offset, 0, 0, 9_999),
        }
        if name:
            params["filter"] = f"name:~'{_fql_quote(str(name).strip())}'"
        domain = (search_domain or "").strip()
        if domain:
            if domain not in domains:
                return None, "Error: search_domain must be one of " + ", ".join(domains) + "."
            params["search_domain"] = domain
        return params, None

    async def list_crowdstrike_ngsiem_saved_queries(
        name: str = "",
        search_domain: str = "",
        limit: int = _DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> str:
        """List NGSIEM saved queries, optionally by name."""
        params, err = _content_params(name, search_domain, _SAVED_QUERY_DOMAINS, limit, offset)
        if err:
            return err
        data, err = await _api(
            "get", "/ngsiem-content/queries/savedqueries/v1", "list saved queries", params=params
        )
        if err:
            return err
        items = _resources(data)
        if not items:
            return "No saved queries matched."
        body = "\n".join(_content_line(i) for i in items)
        return (
            f"Found {len(items)} saved quer{'ies' if len(items) != 1 else 'y'}:\n"
            + wrap_untrusted(body, f"{_PROVIDER} saved queries")
            + _pagination_hint(data, params["offset"], len(items))
            + "\n[get_crowdstrike_ngsiem_saved_query(query_id) returns a query's CQL]"
        )

    async def get_crowdstrike_ngsiem_saved_query(query_id: str, search_domain: str = "") -> str:
        """Read a saved query as its LogScale YAML template (name + CQL)."""
        query_id = str(query_id or "").strip()
        if not query_id:
            return "Error: query_id is required (from list_crowdstrike_ngsiem_saved_queries)."
        params: dict[str, Any] = {"ids": query_id}
        domain = (search_domain or "").strip()
        if domain:
            if domain not in _SAVED_QUERY_DOMAINS:
                return "Error: search_domain must be one of " + ", ".join(_SAVED_QUERY_DOMAINS) + "."
            params["search_domain"] = domain
        data, err = await _api(
            "get",
            "/ngsiem-content/entities/savedqueries-template/v1",
            f"get saved query {_trunc(query_id, 80)}",
            params=params,
        )
        if err:
            return err
        items = _resources(data)
        if not items:
            return f"Error: saved query '{_trunc(query_id, 80)}' was not found."
        parts = []
        for item in items:
            if isinstance(item, dict):
                template = next(
                    (v for k, v in item.items() if "yaml" in k.lower() or k == "template"),
                    None,
                )
                text = template if isinstance(template, str) else json.dumps(
                    item, ensure_ascii=False, indent=2, default=str
                )
            else:
                text = str(item)
            parts.append(_trunc(text, _MAX_TEMPLATE_CHARS))
        return "Saved query:\n" + wrap_untrusted("\n---\n".join(parts), f"{_PROVIDER} saved query")

    # ── Lookup files ──────────────────────────────────────────────────────

    async def list_crowdstrike_ngsiem_lookup_files(
        name: str = "",
        search_domain: str = "",
        limit: int = _DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> str:
        """List NGSIEM lookup files, optionally by name."""
        params, err = _content_params(name, search_domain, _LOOKUP_DOMAINS, limit, offset)
        if err:
            return err
        data, err = await _api(
            "get", "/ngsiem-content/queries/lookupfiles/v1", "list lookup files", params=params
        )
        if err:
            return err
        items = _resources(data)
        if not items:
            return "No lookup files matched."
        body = "\n".join(_content_line(i) for i in items)
        return (
            f"Found {len(items)} lookup file{'s' if len(items) != 1 else ''}:\n"
            + wrap_untrusted(body, f"{_PROVIDER} lookup files")
            + _pagination_hint(data, params["offset"], len(items))
            + "\n[download_crowdstrike_ngsiem_lookup_file(filename) stores one as a file]"
        )

    async def download_crowdstrike_ngsiem_lookup_file(
        filename: str, repository: str = _DEFAULT_REPOSITORY
    ) -> str | ToolResult:
        """Store a lookup file as a file of the run (its text readable inline)."""
        name, err = check_lookup_filename(filename)
        if err:
            return err
        repo, err = check_repository(repository)
        if err:
            return err
        access_token = await _token()
        if not access_token:
            return _AUTH_ERROR
        url = f"{base_url}/humio/api/v1/repositories/{repo}/files/{quote(name, safe='')}"
        async with egress_client(timeout=_TRANSFER_TIMEOUT) as client:
            fetched = await fetch_file(
                client,
                "get",
                url,
                provider=_PROVIDER,
                action=f"download lookup file {name}",
                max_bytes=_MAX_LOOKUP_BYTES,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "*/*"},
            )
        if isinstance(fetched, str):
            return fetched
        suffix = name[name.rfind("."):].lower() if "." in name else ""
        content_type = _LOOKUP_CONTENT_TYPES.get(suffix) or fetched.content_type or "text/csv"
        return await deliver_file(
            data=fetched.data,
            filename=name,
            content_type=content_type,
            source=_PROVIDER,
            persist_file_fn=persist_file_fn,
            links=[("Download URL", url)],
            details=[f"Repository: {repo}"],
        )

    async def upload_crowdstrike_ngsiem_lookup_file(
        file_id: str,
        filename: str = "",
        repository: str = _DEFAULT_REPOSITORY,
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Upload a CSV or JSON file of the run as an NGSIEM lookup file."""
        piped = await read_pipe_file(
            file_context, file_id, max_bytes=_MAX_LOOKUP_BYTES, target=_PROVIDER
        )
        if isinstance(piped, str):
            return ToolResult(tool_call_id="", content=piped, is_error=True)
        data, source_name, _content_type = piped
        name, err = check_lookup_filename(filename or source_name)
        if err:
            return err
        suffix = name[name.rfind("."):].lower() if "." in name else ""
        if suffix not in _LOOKUP_CONTENT_TYPES:
            return "Error: a lookup file must be a .csv or a .json file - pass filename with that extension."
        repo, err = check_repository(repository)
        if err:
            return err
        access_token = await _token()
        if not access_token:
            return _AUTH_ERROR
        async with egress_client(timeout=_TRANSFER_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}/humio/api/v1/repositories/{repo}/files",
                _PROVIDER,
                "upload the lookup file",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                files={"file": (name, data, _LOOKUP_CONTENT_TYPES[suffix])},
            )
        if err:
            return err
        try:
            envelope = _envelope_errors(resp.json())
        except ValueError:
            envelope = ""
        if envelope:
            return f"Error: {_PROVIDER} refused the lookup file: {envelope}"
        return wrap_untrusted(
            f"Uploaded lookup file '{name}' ({len(data)} bytes) to the '{repo}' "
            f"repository. Queries use it with `| match(file=\"{name}\", field=...)`.",
            f"{_PROVIDER} lookup-file upload result",
        )

    executor.register("search_crowdstrike_ngsiem", search_crowdstrike_ngsiem)
    executor.register(
        "list_crowdstrike_ngsiem_correlation_rules", list_crowdstrike_ngsiem_correlation_rules
    )
    executor.register(
        "get_crowdstrike_ngsiem_correlation_rule", get_crowdstrike_ngsiem_correlation_rule
    )
    executor.register(
        "set_crowdstrike_ngsiem_correlation_rule_status",
        set_crowdstrike_ngsiem_correlation_rule_status,
    )
    executor.register("list_crowdstrike_ngsiem_detections", list_crowdstrike_ngsiem_detections)
    executor.register("get_crowdstrike_ngsiem_detection", get_crowdstrike_ngsiem_detection)
    executor.register(
        "list_crowdstrike_ngsiem_saved_queries", list_crowdstrike_ngsiem_saved_queries
    )
    executor.register("get_crowdstrike_ngsiem_saved_query", get_crowdstrike_ngsiem_saved_query)
    executor.register(
        "list_crowdstrike_ngsiem_lookup_files", list_crowdstrike_ngsiem_lookup_files
    )
    executor.register(
        "download_crowdstrike_ngsiem_lookup_file", download_crowdstrike_ngsiem_lookup_file
    )
    executor.register(
        "upload_crowdstrike_ngsiem_lookup_file", upload_crowdstrike_ngsiem_lookup_file
    )

    src = "integration:crowdstrike_ngsiem"
    repository_schema = {
        "type": "string",
        "description": (
            "Repository or view: 'search-all' (default: CrowdStrike and third-party "
            "data), 'investigate_view' (endpoint sensor events, Falcon Insight XDR), "
            "'third-party' (third-party data only), or a custom view name."
        ),
        "default": _DEFAULT_REPOSITORY,
    }
    list_paging = {
        "limit": {"type": "integer", "default": _DEFAULT_LIST_LIMIT, "description": "1-100"},
        "offset": {"type": "integer", "default": 0},
    }
    return [
        ToolDefinition(
            name="search_crowdstrike_ngsiem",
            description=(
                "Search CrowdStrike Falcon Next-Gen SIEM with a CrowdStrike Query "
                "Language (CQL / LogScale) query - threat hunting across endpoint "
                "telemetry and third-party logs. Runs the query as a search job and "
                "waits up to timeout_seconds; a search that does not finish is "
                "stopped and its partial results are returned, marked PARTIAL. Keep "
                "results small: filter first, then `| select([...])`, `| head(N)` "
                "or `| groupBy(...)`. Example: `#event_simpleName=ProcessRollup2 "
                "FileName=/powershell/i | select([@timestamp, ComputerName, "
                "UserName, CommandLine]) | head(50)`."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The CQL query."},
                    "repository": repository_schema,
                    "start": {
                        "type": "string",
                        "description": (
                            "Start of the time range: relative ('15m', '24h', '7d'), "
                            "ISO 8601 or a Unix timestamp. Default '24h'."
                        ),
                        "default": "24h",
                    },
                    "end": {
                        "type": "string",
                        "description": "End of the time range, same formats; default now.",
                    },
                    "limit": {
                        "type": "integer",
                        "default": _DEFAULT_EVENT_LIMIT,
                        "description": f"Events to show, 1-{_MAX_EVENT_LIMIT}.",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "default": _DEFAULT_SEARCH_TIMEOUT,
                        "description": (
                            f"How long to wait for the search, {_MIN_SEARCH_TIMEOUT}-"
                            f"{_MAX_SEARCH_TIMEOUT} seconds."
                        ),
                    },
                },
                "required": ["query"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="list_crowdstrike_ngsiem_correlation_rules",
            description=(
                "List Next-Gen SIEM correlation rules (scheduled CQL detections): "
                "name, status, severity, lookback, schedule and query. Filter by "
                "status (active / inactive), a name substring, or a raw FQL filter "
                "(e.g. `severity:>50`)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": list(_RULE_STATUSES)},
                    "name": {"type": "string"},
                    "filter_query": {"type": "string"},
                    "sort": {
                        "type": "string",
                        "enum": list(_RULE_SORTS),
                        "default": "last_updated_on|desc",
                    },
                    **list_paging,
                },
                "required": [],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_crowdstrike_ngsiem_correlation_rule",
            description=(
                "Read one Next-Gen SIEM correlation rule in full: its CQL query, "
                "lookback, schedule, outcome, severity and MITRE ATT&CK mapping."
            ),
            input_schema={
                "type": "object",
                "properties": {"rule_id": {"type": "string"}},
                "required": ["rule_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="set_crowdstrike_ngsiem_correlation_rule_status",
            description=(
                "Activate or deactivate a Next-Gen SIEM correlation rule (for "
                "example to silence a noisy rule). Changes what the SIEM detects."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "rule_id": {"type": "string"},
                    "status": {"type": "string", "enum": list(_RULE_STATUSES)},
                    "comment": {"type": "string", "description": "Why, kept with the rule."},
                },
                "required": ["rule_id", "status"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="list_crowdstrike_ngsiem_detections",
            description=(
                "List detections raised by Next-Gen SIEM correlation rules, newest "
                "first. Filter by status (new, in_progress, reopened, closed), "
                "severity (Critical, High, Medium, Low, Informational) or a raw FQL "
                "filter."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "severity": {"type": "string"},
                    "filter_query": {"type": "string"},
                    **list_paging,
                },
                "required": [],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_crowdstrike_ngsiem_detection",
            description="Read one Next-Gen SIEM detection in full by its composite id.",
            input_schema={
                "type": "object",
                "properties": {"detection_id": {"type": "string"}},
                "required": ["detection_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="list_crowdstrike_ngsiem_saved_queries",
            description=(
                "List Next-Gen SIEM saved queries (reusable CQL searches), "
                "optionally by name substring and search domain."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "search_domain": {"type": "string", "enum": list(_SAVED_QUERY_DOMAINS)},
                    **list_paging,
                },
                "required": [],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="get_crowdstrike_ngsiem_saved_query",
            description=(
                "Read a Next-Gen SIEM saved query as its LogScale YAML template, "
                "including its CQL - ready to run with search_crowdstrike_ngsiem."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query_id": {"type": "string"},
                    "search_domain": {"type": "string", "enum": list(_SAVED_QUERY_DOMAINS)},
                },
                "required": ["query_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="list_crowdstrike_ngsiem_lookup_files",
            description=(
                "List Next-Gen SIEM lookup files (CSV / JSON tables queries enrich "
                "or match events against, such as IOC lists or asset inventories)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "search_domain": {"type": "string", "enum": list(_LOOKUP_DOMAINS)},
                    **list_paging,
                },
                "required": [],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="download_crowdstrike_ngsiem_lookup_file",
            workspace_ingest=True,
            description=(
                "Download a Next-Gen SIEM lookup file and store it as a file of the "
                f"run (its text readable, up to {_MAX_LOOKUP_BYTES // 1024 // 1024} MB)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "e.g. 'bad_domains.csv'"},
                    "repository": repository_schema,
                },
                "required": ["filename"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="upload_crowdstrike_ngsiem_lookup_file",
            description=(
                "Upload a CSV or JSON file of the run (attached, generated or "
                "downloaded, by file_id) as a Next-Gen SIEM lookup file - for "
                "example an IOC list exported from OpenCTI, then matched in queries "
                "with `| match(file=\"name.csv\", field=...)`. A lookup file with "
                "the same name in that repository is overwritten. Up to "
                f"{_MAX_LOOKUP_BYTES // 1024 // 1024} MB."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string"},
                    "filename": {
                        "type": "string",
                        "description": "Name in NGSIEM, ending in .csv or .json; default the file's own name.",
                    },
                    "repository": repository_schema,
                },
                "required": ["file_id"],
            },
            source=src,
            priority=2,
        ),
    ]
