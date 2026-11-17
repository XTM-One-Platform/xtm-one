"""Shared helpers for the Intel 471 (Titan API) provider package.

Intel 471 exposes the whole Titan API under one fixed SaaS host
(``https://api.intel471.com/v1``), authenticated with **HTTP Basic**: the
account email as the username and a Titan API key as the password. There is no
OAuth and no token refresh; access is bounded by the account's Titan
subscriptions (adversary intelligence, malware intelligence, credentials, ...).

The dispatcher (``integration_tools._get_token``) hands every ``build_tools``
call the compound ``api_username||api_key`` token (mirrors HackerOne): the email
is prepended from ``extra_config.api_username`` and the API key is the stored
secret. ``parse_compound_token`` splits it back into ``(email, api_key)``.

Conventions shared across the domain modules:

* ``BASE_URL`` / ``api_base`` - the single Titan v1 host.
* ``build_basic_headers`` - the ``Authorization: Basic ...`` + JSON Accept header.
* Raw HTTP query params are **camelCase** (``lastUpdatedFrom``, ``iocType``,
  ``watcherGroup`` ...) and the JSON response envelope uses camelCase keys plus
  a ``<noun>TotalCount`` field, so the extractors below read camelCase.
* Search endpoints paginate with ``offset`` + ``count`` (max 100). Titan caps
  the reachable offset window at ~1100 records; ``clamp_offset`` enforces it.
* Stream endpoints (``/indicators/stream``, ``/events/stream``) paginate with a
  ``cursor`` and return ``cursorNext``.
* Time filters (``from`` / ``until`` / ``lastUpdatedFrom`` / ``lastUpdatedUntil``)
  accept a unix-millis integer OR a relative string like ``2days`` / ``1hour`` /
  ``1week`` and are forwarded verbatim.
"""

from __future__ import annotations

import base64
import datetime as _dt
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.http import safe_request

_PROVIDER = "Intel 471"
_SOURCE = "integration:intel471"

# Fixed Titan API host - Intel 471 is SaaS-only, there are no tenant URLs, so
# the base URL is a constant (never surfaced to agent output / system prompts).
BASE_URL = "https://api.intel471.com/v1"

# Titan search endpoints accept ``count`` up to 100 records per page.
MAX_COUNT = 100
DEFAULT_COUNT = 10

# Titan only lets ``offset`` walk the first ~1100 records of a result set; going
# beyond returns an error. Keep callers inside the reachable window and steer
# them toward tighter filters / time ranges instead of deep offset paging.
MAX_OFFSET = 1000

# Valid ``sort`` values across the search endpoints. ``relevance`` is only
# meaningful on the free-text search endpoints (others default to ``latest``),
# but the API tolerates it everywhere, so a single enum keeps the tools simple.
SORT_VALUES: tuple[str, ...] = ("relevance", "latest", "earliest")

_TIMEOUT = 30


def api_base() -> str:
    """Return the Titan API base URL (constant; shared with the connectivity test)."""
    return BASE_URL


def build_basic_headers(api_username: str, api_key: str) -> dict[str, str]:
    """Build the Titan HTTP Basic ``Authorization`` + JSON ``Accept`` headers.

    The email and key are stripped so a value pasted with a trailing newline or
    space (common when copying credentials) can never produce a malformed header
    value (httpx/h11 reject header values containing newlines). Single chokepoint
    shared by the tool builder, ``validate_connection`` and the connectivity test.
    """
    raw = f"{(api_username or '').strip()}:{(api_key or '').strip()}".encode()
    return {
        "Authorization": "Basic " + base64.b64encode(raw).decode("ascii"),
        "Accept": "application/json, text/plain",
    }


def client(timeout: int = _TIMEOUT) -> httpx.AsyncClient:
    """Return a fresh async HTTP client for a single Titan call."""
    return egress_client(timeout=timeout)


async def titan_get(
    headers: dict[str, str],
    path: str,
    action: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: int = _TIMEOUT,
    expected_statuses: set[int] | None = None,
) -> tuple[Any, str | None]:
    """GET ``{BASE_URL}{path}`` and return ``(json, error)``.

    Wraps the shared ``safe_request`` (standard error strings + 429 retry) and
    parses the JSON body. Every read tool routes through here so error handling,
    the 412 "invalid filter combination" precondition failure, and the empty-body
    case are handled uniformly. Returns ``(None, error_string)`` on failure and
    ``(payload, None)`` on success (``payload`` is ``{}`` for an empty body).
    """
    async with client(timeout) as c:
        resp, err = await safe_request(
            c,
            "get",
            f"{BASE_URL}{path}",
            _PROVIDER,
            action,
            headers=headers,
            params=params,
            expected_statuses=expected_statuses,
        )
        if err:
            return (resp, err) if expected_statuses is not None else (None, err)
        try:
            return resp.json(), None
        except Exception:
            return {}, None


async def titan_request(
    headers: dict[str, str],
    method: str,
    path: str,
    action: str,
    *,
    params: dict[str, Any] | None = None,
    json: Any | None = None,
    timeout: int = _TIMEOUT,
    expected_statuses: set[int] | None = None,
) -> tuple[Any, str | None]:
    """Verb-agnostic Titan request returning ``(json, error)``.

    Used by the write surface (watcher create/update/delete). Only attaches a
    JSON body when one is supplied. A 204 (empty body) success returns ``({}, None)``.
    """
    request_kwargs: dict[str, Any] = {"headers": headers}
    if params is not None:
        request_kwargs["params"] = params
    if json is not None:
        request_kwargs["json"] = json
    async with client(timeout) as c:
        resp, err = await safe_request(
            c,
            method,
            f"{BASE_URL}{path}",
            _PROVIDER,
            action,
            expected_statuses=expected_statuses,
            **request_kwargs,
        )
        if err:
            return (resp, err) if expected_statuses is not None else (None, err)
        try:
            return resp.json(), None
        except Exception:
            return {}, None


def norm_sort(sort: Any, default: str = "latest") -> str:
    """Normalise a caller-supplied ``sort`` to a valid Titan value.

    Anything outside :data:`SORT_VALUES` (None, typos, hallucinated values)
    falls back to ``default`` so a bad sort never breaks a search. Single
    source of truth for the validation the search tools all need.
    """
    return sort if sort in SORT_VALUES else default


def clamp_count(count: Any, default: int = DEFAULT_COUNT) -> int:
    """Clamp a caller-supplied ``count`` into ``[1, MAX_COUNT]``.

    Garbage (None / non-numeric) falls back to ``default`` rather than erroring,
    so a hallucinated value never breaks a search.
    """
    try:
        return max(1, min(int(count), MAX_COUNT))
    except (TypeError, ValueError):
        return default


def clamp_offset(offset: Any) -> int:
    """Clamp a caller-supplied ``offset`` into ``[0, MAX_OFFSET]``.

    Titan errors past ~1100 records; clamping keeps deep-paging attempts inside
    the reachable window instead of surfacing an opaque API error.
    """
    try:
        return max(0, min(int(offset), MAX_OFFSET))
    except (TypeError, ValueError):
        return 0


def trunc(text: Any, max_len: int = 300) -> str:
    """Collapse whitespace and truncate ``text`` to ``max_len`` chars."""
    if text is None:
        return ""
    text = str(text).replace("\r", " ").replace("\n", " ").strip()
    return text[:max_len] + ("\u2026" if len(text) > max_len else "")


def ms_to_dt(value: Any) -> str:
    """Render a Titan timestamp (unix millis or ISO string) to minute precision.

    Titan activity/creation times are usually unix-millis integers; some nested
    fields are ISO strings. Handle both and degrade to an empty string on junk.
    """
    if value in (None, ""):
        return ""
    # Numeric unix-millis (int or numeric string).
    try:
        millis = int(value)
    except (TypeError, ValueError):
        # Already an ISO-ish string - trim to minute precision.
        return str(value)[:16]
    if millis <= 0:
        return ""
    try:
        return (
            _dt.datetime.fromtimestamp(millis / 1000, tz=_dt.UTC)
            .strftime("%Y-%m-%d %H:%M")
        )
    except (ValueError, OverflowError, OSError):
        return ""


def extract_items(payload: Any, *keys: str) -> list[dict[str, Any]]:
    """Pull the result list out of a Titan search response.

    Titan responses wrap the records under a camelCase noun key (``actors``,
    ``reports``, ``cveReports`` / ``cve_reports``, ``indicators`` ...). Try each
    supplied key in order, then fall back to the first list-valued field so a
    minor key-name drift never yields "no results" on a real hit.
    """
    if not isinstance(payload, dict):
        return []
    for key in keys:
        val = payload.get(key)
        if isinstance(val, list):
            return [item for item in val if isinstance(item, dict)]
    for val in payload.values():
        if isinstance(val, list):
            return [item for item in val if isinstance(item, dict)]
    return []


def extract_total(payload: Any, fallback: int) -> int:
    """Read the ``<noun>TotalCount`` total out of a Titan search response.

    Titan reports the full match count in a ``...TotalCount`` field (e.g.
    ``actorTotalCount``, ``reportTotalCount``). Return the first such field, or
    ``fallback`` (usually the page length) when none is present.
    """
    if isinstance(payload, dict):
        for key, val in payload.items():
            if key.endswith("TotalCount") and isinstance(val, int):
                return val
    return fallback


def safe_seg(value: Any) -> bool:
    """True when ``value`` is a safe single URL path segment (no traversal).

    Rejects empty values, ``.`` / ``..``, forward and back slashes, and NUL so
    a caller-supplied uid can never escape its intended path position. Single
    source of truth for every tool that interpolates a uid into a Titan path.
    """
    s = str(value or "").strip()
    return bool(s) and s not in (".", "..") and not any(c in s for c in "/\\\x00")


def more_hint(total: int, shown: int, offset: int) -> str:
    """Return a pagination hint when more records are reachable, else ``""``."""
    seen = offset + shown
    # ``<=`` because clamp_offset allows offset == MAX_OFFSET, so the page at
    # exactly MAX_OFFSET is still reachable and worth suggesting.
    if total > seen and seen <= MAX_OFFSET:
        return (
            f"\n[Showing {shown} of {total}; pass offset={seen} for the next page "
            f"(Titan allows paging up to offset {MAX_OFFSET} - narrow the time "
            "range or filters for deeper results).]"
        )
    if total > seen:
        return (
            f"\n[Showing records up to offset {MAX_OFFSET} of {total}; narrow the "
            "time range or filters to reach the rest.]"
        )
    return ""


def add_time_filters(
    params: dict[str, Any],
    *,
    from_: str = "",
    until: str = "",
    last_updated_from: str = "",
    last_updated_until: str = "",
) -> None:
    """Attach Titan camelCase time-window filters to ``params`` when provided.

    Each value is forwarded verbatim (unix millis or a relative string like
    ``2days`` / ``1hour`` / ``1week``); empty values are skipped.
    """
    if from_ and str(from_).strip():
        params["from"] = str(from_).strip()
    if until and str(until).strip():
        params["until"] = str(until).strip()
    if last_updated_from and str(last_updated_from).strip():
        params["lastUpdatedFrom"] = str(last_updated_from).strip()
    if last_updated_until and str(last_updated_until).strip():
        params["lastUpdatedUntil"] = str(last_updated_until).strip()


# Reusable JSON-schema fragments so every search tool exposes a consistent set
# of pagination + time-window params without repeating the boilerplate.
TIME_FILTER_PROPS: dict[str, Any] = {
    "from_time": {
        "type": "string",
        "description": (
            "Creation-time lower bound (inclusive). Unix millis or a relative "
            "string like '2days', '1hour', '1week'."
        ),
    },
    "until_time": {
        "type": "string",
        "description": (
            "Creation-time upper bound (exclusive). Unix millis or a relative "
            "string like '1day'."
        ),
    },
    "last_updated_from": {
        "type": "string",
        "description": "Last-updated lower bound (inclusive). Unix millis or relative string.",
    },
    "last_updated_until": {
        "type": "string",
        "description": "Last-updated upper bound (exclusive). Unix millis or relative string.",
    },
}

PAGE_PROPS: dict[str, Any] = {
    "offset": {
        "type": "integer",
        "default": 0,
        "description": f"Records to skip for paging (0-{MAX_OFFSET}).",
    },
    "count": {
        "type": "integer",
        "default": DEFAULT_COUNT,
        "description": f"Records to return (1-{MAX_COUNT}).",
    },
}

SORT_PROP: dict[str, Any] = {
    "sort": {
        "type": "string",
        "enum": list(SORT_VALUES),
        "description": (
            "Sort order: 'relevance' (free-text search), 'latest' (newest "
            "first), or 'earliest' (oldest first)."
        ),
    },
}
