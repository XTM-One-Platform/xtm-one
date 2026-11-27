"""Shared helpers for the Notion integration package.

Holds the constants, logger, HTTP machinery (``NotionContext``), small
extractors, and the ``_parse_json_arg`` coercion helper used by every Notion
domain module. Mirrors the ``jira/_common.py`` split.

Notion API uses either:
  - Internal integration token (api_key): simpler, no OAuth
  - Public OAuth: requires client_id + client_secret + OAuth flow

Both use Bearer token auth with Notion-Version header.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import json
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

from app.core.utils import format_exception_message
from app.integrations.http import (
    AutoRefreshClient,
    format_entity_list,  # noqa: F401 - re-exported for domain modules
    format_http_error,
    format_network_error,
    safe_request,
)
from app.integrations.stale_references import (
    confirm_reference_alive,
    report_stale_reference,
)
from app.services.branding import get_cached_branding
from app.tools.base import ToolResult, is_error_shaped_result

logger = logging.getLogger("app.integrations.providers.notion")

_PROVIDER = "Notion"
_BASE_URL = "https://api.notion.com/v1"
NOTION_API = _BASE_URL
_NOTION_VERSION = "2025-09-03"
NOTION_VERSION = _NOTION_VERSION

# Notion API limit: max children blocks per single request (create page / append).
_NOTION_BLOCK_LIMIT = 100
# ``parent.type`` values of a page that is a database row. Since API
# 2025-09-03 a row created through the data sources API reports
# ``data_source_id``; older rows report ``database_id``. Both carry the
# database properties.
_DATABASE_ROW_PARENT_TYPES = frozenset({"database_id", "data_source_id"})
# Notion answers 409 ``conflict_error`` when two writes on one page race;
# its guidance is to retry. A block PATCH sets the block to a given state,
# so repeating it is safe.
_BLOCK_WRITE_RETRY_STATUSES = frozenset({409})
# Notion API limit: max characters per rich_text[].text.content value.
_NOTION_RICH_TEXT_LIMIT = 2000

# Content preview: max characters of page content included in search results.
# Enough to determine relevance without reading the full page.
_SEARCH_PREVIEW_CHARS = 500
# Default window of get_notion_page (characters per call) - keeps the one-call
# shape of a short page without bloating the context on a long one.
_READ_PAGE_MAX_CHARS = 8000
# Widest window one get_notion_page call may ask for (``max_chars``):
# a deliberate wide read of a long page, still bounded.
_READ_PAGE_WINDOW_MAX_CHARS = 40_000
# Max depth for recursive block fetching (prevents infinite loops). Three
# levels below the page reach a callout's content inside a column
# (column_list -> column -> callout -> heading), the layout the Markdown
# writer builds.
_MAX_BLOCK_DEPTH = 3
# Deepest tree list_notion_block_children(depth=...) descends: a
# column_list -> column -> toggle -> content nesting is three levels down.
_LIST_CHILDREN_MAX_DEPTH = 4
# Most blocks one list_notion_block_children call renders across every level.
_LIST_CHILDREN_MAX_BLOCKS = 400
# Most containers one list_notion_block_children call descends into: each is
# one more paginated fetch, so without a bound a deep listing of a large page
# is hundreds of Notion requests in one tool call against a 3 req/s limit.
# A container past the budget keeps its ``children`` marker and the listing
# says it stopped descending.
_LIST_CHILDREN_MAX_FETCHES = 40
# Max concurrent Notion API requests per tool invocation (Notion rate-limits at 3 req/s).
_NOTION_CONCURRENCY = 3

# ── Cross-run sustained-404 detection ───────────────────────────────────────
# When the same Notion resource (database/page/block) returns 404 across
# multiple separate runs for the same integration, a WARNING is emitted so
# operators can identify permanently-stale references.
#
# _STALE_REF_WARN_THRESHOLD: emit WARNING once this many distinct-run 404s
# accumulate within the sliding TTL window (the counter never resets on an
# intermittent success — only TTL expiry decays it, so this is a
# "sustained failures within the window" signal, not a strictly-consecutive
# one). Keep this low so hourly automations surface stale IDs quickly instead
# of burning multiple failing cycles before suppression kicks in.
# _STALE_REF_REDIS_TTL_SECS: sliding TTL; counter resets if no new 404s arrive
# within this window (14 days — long enough to catch weekly assignments that
# silently degrade across several runs).
_STALE_REF_WARN_THRESHOLD = 2
_STALE_REF_REDIS_KEY_PREFIX = "notion:stale_ref:"
_STALE_REF_REDIS_TTL_SECS = 14 * 24 * 3600
# _STALE_REF_RECHECK_SECS: once the pre-call suppression is active, allow one
# live probe per run again after this much time has elapsed since the LAST
# failing bump, so a resource that was re-shared or restored in Notion can
# recover without waiting for the full counter TTL to expire.  Suppressed
# calls never bump the counter, so the key's remaining TTL tells us exactly
# how long ago the last real 404 happened (age = TTL - remaining).  A probe
# that still 404s re-bumps the counter (sliding TTL) and re-arms suppression
# for another window; a successful probe clears the counter immediately
# (see ``_clear_stale_ref``).  Kept SHORT (1 hour): the in-run
# missing cache already dedupes retries within a run, so the residual cost
# of the recheck is at most one live probe per resource per hour — while a
# long window blackholes a restored resource for days after the operator
# fixes sharing (2026-07-14 incident: a fully accessible database kept
# returning synthetic 404s and the agent relayed "still 404" to the user
# without ever touching the API).
_STALE_REF_RECHECK_SECS = 3600
# _STALE_REF_BUMP_TIMEOUT_SECS: the counter bump is AWAITED by the failing
# call (the run must learn about a sustained reference before the call
# returns), so it is bounded well under the Redis socket timeouts: a
# slow or hung Redis costs the report, never the Notion call.
_STALE_REF_BUMP_TIMEOUT_SECS = 2.0

# Human-readable names for the URL resource types tracked by the in-run
# missing cache and the cross-run stale counter.
_READABLE_RESOURCE_TYPES = {
    "databases": "database",
    "data_sources": "data source",
    "blocks": "block",
    "pages": "page",
}

# Canonical set of Notion block-type discriminator keys. A block update payload
# carries the new content under a key named after the block's type
# (e.g. ``{"paragraph": {...}}``). Notion does NOT allow changing a block's
# type, so when an update payload's type key differs from the existing block's
# type the API rejects it with HTTP 400. We use this set to detect the
# requested type in the payload and pre-flight-check it against the live block.
_NOTION_BLOCK_TYPES = frozenset(
    {
        "paragraph",
        "heading_1",
        "heading_2",
        "heading_3",
        "heading_4",
        "bulleted_list_item",
        "numbered_list_item",
        "to_do",
        "toggle",
        "quote",
        "callout",
        "code",
        "divider",
        "table_of_contents",
        "breadcrumb",
        "equation",
        "embed",
        "bookmark",
        "image",
        "video",
        "audio",
        "file",
        "pdf",
        "link_preview",
        "link_to_page",
        "synced_block",
        "table",
        "table_row",
        "column_list",
        "column",
        "child_page",
        "child_database",
        "template",
    }
)

# The colours a block accepts (``color`` of a text block or a callout): the
# ten names, and each but ``default`` as a ``_background``.
_NOTION_COLOR_NAMES = (
    "default",
    "gray",
    "brown",
    "orange",
    "yellow",
    "green",
    "blue",
    "purple",
    "pink",
    "red",
)
_NOTION_COLORS = frozenset(
    {
        *_NOTION_COLOR_NAMES,
        *(f"{name}_background" for name in _NOTION_COLOR_NAMES if name != "default"),
    }
)

# Notion accepts two levels of nesting in one request - a block, its children
# and theirs - and at most ``_NOTION_BLOCK_LIMIT`` blocks in one ``children``
# array. A deeper or longer payload is cut there and the rest appended under
# the block it belongs to once the request created it.
_REQUEST_NESTING_LEVELS = 2
# The levels of children a block must be created with: a column list holds
# columns holding blocks, a column or a table holds its blocks / rows.
_REQUIRED_CHILD_LEVELS = {"column_list": 2, "column": 1, "table": 1}

# ── Block alignment is UI-only ──────────────────────────────────
# Notion has NO API surface for the alignment (left / center / right) of an
# image or any other media block: the image / video / audio / file / pdf
# block objects accept only their file source and a ``caption``, and the
# request schemas are ``additionalProperties: false``, so an ``alignment``
# key is rejected with a raw HTTP 400 the agent then retries. Verified
# against the API reference for the version this provider pins AND for the
# latest published version (``_NOTION_ALIGNMENT_VERIFIED_VERSIONS``) - the
# 2026-03-11 release adds no alignment / width hint either. Alignment is set
# in the Notion UI (hover the block > alignment icon). Every Notion write
# tool therefore refuses the key up front with the SAME sentence, and
# ``upload_notion_file`` says so on every image block it attaches, so the
# agent tells the user instead of looping on a 400 or - worse - reporting an
# alignment it never applied (the tool executor silently drops kwargs a
# handler does not declare, which is what happened before ``alignment``
# became an explicit, refused argument).
_NOTION_ALIGNMENT_UI_ONLY = (
    "alignment is a UI-only setting in Notion and cannot be set through the API"
)
_NOTION_ALIGNMENT_NOTE = "Note: alignment is UI-only; the block is centered by default."
# API versions whose block schemas were checked for an ``alignment`` field
# (none has one). ``tests/test_notion_image_alignment.py`` pins
# ``_NOTION_VERSION`` to this set: bumping the header means re-reading the
# image block schema - if alignment appears, implement it and drop the guard;
# otherwise add the new version here.
_NOTION_ALIGNMENT_VERIFIED_VERSIONS = frozenset({"2025-09-03", "2026-03-11"})


def _alignment_refusal(what: str, retry: str) -> str:
    """The one refusal every Notion write tool returns for an ``alignment`` request.

    ``what`` names what was attempted (e.g. "image.alignment cannot be set"),
    ``retry`` tells the agent how to resend the call without it. The shared
    sentence and the remedy stay identical across tools so the agent learns
    the limitation once, whichever tool it tried first.
    """
    return (
        f"Error: {what} - {_NOTION_ALIGNMENT_UI_ONLY}. {retry} "
        "Left / center / right are chosen in Notion by hovering the block and "
        "using the alignment icon; a block created through the API is centered "
        "by default. Tell the user the alignment has to be adjusted in Notion."
    )


def _alignment_argument_refusal(
    alignment: Any, what: str, *, nothing_done: str
) -> str | None:
    """Refuse an explicit ``alignment`` tool ARGUMENT, or ``None`` when it is blank.

    Every Notion write tool declares an ``alignment`` parameter ONLY to run
    this check: the tool executor silently drops kwargs a handler does not
    declare, so before the parameter existed ``upload_notion_file(...,
    alignment="left")`` uploaded the file and let the agent report an
    alignment it never applied - and the same silent drop applies to the
    block write tools. The parameter is deliberately absent from
    every input schema (advertising it would invite the model to use it),
    and a blank / ``None`` value (a model echoing the default) is not a
    request. ``what`` names the thing the alignment was meant for ("the
    uploaded file"), ``nothing_done`` states what the refusal left untouched
    ("nothing has been uploaded") so the agent knows the call is safe to
    resend as-is minus the argument.
    """
    value = "" if alignment is None else str(alignment).strip()
    if not value:
        return None
    return _alignment_refusal(
        f"alignment='{value}' cannot be applied to {what}",
        f"Retry the same call without the alignment argument ({nothing_done}).",
    )


# ── Database query auto-expansion thresholds ────────────────────────────
# When query_notion_database returns few results, automatically include
# page content to eliminate follow-up get_notion_page calls.
# ≤ _QUERY_FULL_CONTENT_THRESHOLD → full page content (up to _READ_PAGE_MAX_CHARS each)
# ≤ _QUERY_PREVIEW_THRESHOLD     → content preview (_SEARCH_PREVIEW_CHARS each)
# > _QUERY_PREVIEW_THRESHOLD     → metadata only (title, ID, URL, properties)
_QUERY_FULL_CONTENT_THRESHOLD = 5
_QUERY_PREVIEW_THRESHOLD = 20

# Sentinel returned by ``resolve_data_source_id`` when the given ID belongs to
# a Notion **page**, not a database.  Callers must detect this value and return
# an actionable error to the agent (e.g. "use get_notion_page") instead of
# attempting a database operation with a nonsensical ID.
_PAGE_NOT_DATABASE_SENTINEL = "__notion_page_not_database__"

# Sentinel returned by ``resolve_data_source_id`` when BOTH the databases probe
# and the data_sources probe return 404/object_not_found — the given ID is
# unavailable (deleted, moved, or no longer shared with the integration).
# Callers must detect this value and return an actionable error prompting the
# agent to locate the database by name (``search_notion`` /
# ``list_notion_databases``) rather than propagating a raw 404 or silently
# using the stale ID in subsequent API calls (which would fail anyway).
# Non-404 failures (auth, network, 5xx) deliberately do NOT produce the
# sentinel — see ``_is_not_found_failure``.
_RESOLUTION_FAILED_SENTINEL = "__notion_resolution_failed__"


def _resolution_failed_error(database_id: str) -> str:
    """Actionable error returned by callers that hit ``_RESOLUTION_FAILED_SENTINEL``.

    Single source of truth for the message so every tool (query, templates,
    update, stale-retry) reports the same guidance. The wording must stay
    HONEST about suppression: the 404s that produced the sentinel may have
    come from the in-run missing-resource cache or the cross-run stale
    suppression — fast paths that synthesize 404/object_not_found WITHOUT
    contacting the Notion API. Claiming a live "both probes
    returned 404" here would reintroduce the incident where the agent
    relayed "still 404" to the user while the API was never touched. The
    message therefore names the cached-verdict possibility and the live
    escape hatches (search is never suppressed; the connectivity test clears
    the counters), mirroring ``_make_cross_run_stale_error``.
    """
    recheck_hours = max(1, _STALE_REF_RECHECK_SECS // 3600)
    return (
        f"Error: Notion database {database_id} is unavailable — the "
        "databases and data_sources lookups both reported "
        "404/object_not_found. The database may have been deleted, moved, "
        "or its sharing with this integration revoked. Note: the 404 may "
        "be a cached/suppressed verdict from an earlier failing call or "
        "run, in which case the Notion API was NOT contacted on this "
        "attempt. "
        "What you can do NOW: use search_notion or list_notion_databases "
        "to locate the database by name and obtain a valid ID — search is "
        "never suppressed and returns current data. "
        "If the database has been restored or re-shared, suppression "
        f"clears automatically (a live probe runs within {recheck_hours} "
        "hour(s), and any successful access resets the counter "
        "immediately); an admin can also clear it by running the "
        "integration's connectivity test."
    )


def _is_not_found_failure(resp: Any, err: str | None) -> bool:
    """Return True when a failed request means the resource does not exist.

    Used by :meth:`NotionContext.resolve_data_source_id` to decide whether a
    probe failure is a genuine 404/object_not_found (resource deleted, moved,
    or unshared) as opposed to an auth failure, a transient 5xx, or a network
    error — only the former may produce :data:`_RESOLUTION_FAILED_SENTINEL`,
    otherwise a Notion outage would be misreported to the agent as "the
    database was deleted".

    When the failed ``httpx.Response`` is available (``expected_statuses``
    contract), the status code is authoritative. Without a response (network
    errors, cached-missing fast paths that return ``(None, err)``), fall back
    to the 404-only phrasings: ``format_http_error`` emits ``"resource not
    found"`` exclusively for HTTP 404, and the in-run missing-resource cache /
    cross-run stale suppression embed ``object_not_found`` in their messages.
    """
    if not err:
        return False
    if resp is not None:
        try:
            return resp.status_code == 404
        except Exception:
            return False
    lowered = err.lower()
    return "resource not found" in lowered or "object_not_found" in lowered


def _is_page_not_database_400(resp: Any) -> bool:
    """Return True if *resp* is Notion's 400 "is a page, not a database" error.

    Notion returns this validation error when a **page** ID is passed where a
    database ID is expected (e.g. ``GET /v1/databases/{page_id}``). Shared by
    :meth:`NotionContext.resolve_data_source_id` and the two-step lookup in
    ``get_notion_database`` so the message matching can never drift.
    """
    if resp is None:
        return False
    try:
        if resp.status_code != 400:
            return False
        msg = (resp.json().get("message") or "").lower()
        return "is a page, not a database" in msg
    except Exception:
        return False


def _is_page_id_400(resp: Any) -> bool:
    """Return True if *resp* is a Notion 400 saying the id in its path is a page.

    The broader sibling of :func:`_is_page_not_database_400`: the database
    probe answers "is a page, not a database" and the data-source probe an
    equivalent "is a page" wording when a regular page id reaches them. A
    parent-type probe reads either as a detection miss (the id exists, it is
    a page), never as a failure.
    """
    if resp is None:
        return False
    try:
        if resp.status_code != 400:
            return False
        msg = (resp.json().get("message") or "").lower()
        return "is a page" in msg
    except Exception:
        return False


def _is_database_not_page_400(resp: Any) -> bool:
    """Return True if *resp* is Notion's 400 "is a database, not a page" error.

    The mirror of :func:`_is_page_not_database_400`: Notion
    returns this validation error when a **database** ID is passed where a
    page ID is expected (e.g. ``GET /v1/pages/{database_id}``). Used by the
    page read tools so the agent gets an actionable "use get_notion_database
    / query_notion_database" redirect instead of the raw 400 (which triggered
    a cascade of wasted follow-up calls in production on 2026-07-18).
    """
    if resp is None:
        return False
    try:
        if resp.status_code != 400:
            return False
        msg = (resp.json().get("message") or "").lower()
        return "is a database, not a page" in msg
    except Exception:
        return False


def _names_existing_resource(resp: Any, err: str | None) -> bool:
    """True when a failed call proves the id in its path EXISTS - as another type.

    Notion's 400 "is a page, not a database" (and its mirror) can only be said
    about a resource it found, so the stale-reference detector reads it as the
    resource answering. Without the failed response (no
    ``expected_statuses``), the message is still in the formatted error.
    """
    if _is_page_not_database_400(resp) or _is_database_not_page_400(resp):
        return True
    lowered = (err or "").lower()
    return "is a page, not a database" in lowered or "is a database, not a page" in lowered


_ExpectedJsonShape = Literal["object", "array", "any"]


def _parse_json_arg(
    value: Any,
    *,
    what: str,
    expected: _ExpectedJsonShape = "any",
) -> tuple[Any, str | None]:
    """Coerce a JSON-typed tool argument into a Python object.

    Although the tool schema declares these arguments as ``string``, LLMs
    (notably Anthropic Claude) routinely emit a parsed ``dict``/``list``
    directly when the parameter name ends in ``_json``. ``json.loads``
    then raises ``TypeError: the JSON object must be str, bytes or
    bytearray, not dict``, the call counts as a hard error, and the agent
    loop retries — sometimes 6 calls in parallel per iteration. In
    production this triggered DB pool exhaustion and OOM crashes (multiple
    long-running assignments hammering Notion in tight retry loops).

    This helper accepts both shapes safely:
      * ``None`` / ``""`` → ``(None, None)`` so callers can keep their
        existing ``if value:`` guards.
      * ``dict`` / ``list`` → returned as-is.
      * ``str`` → parsed via :func:`json.loads`.
      * Any other type → structured error string the LLM can act on.

    ``expected`` tailors the unsupported-type error so the retry guidance
    actually matches the tool's real schema (an array-shaped tool like
    ``children_json`` / ``sorts_json`` should not be told to provide a
    JSON object).  Use ``"object"`` for dict-shaped args, ``"array"``
    for list-shaped args, or ``"any"`` (default) when either is valid.
    The helper does not enforce the ``dict`` vs ``list`` split itself —
    callers keep their per-tool shape checks downstream so the error
    messages can stay nuanced (e.g. "must be a JSON object describing
    a single filter condition or a compound ``and``/``or`` group").

    Returns ``(parsed_value, error_message)``. On success ``error_message``
    is ``None``; on failure ``parsed_value`` is ``None`` and the caller
    should return the error string back to the agent.
    """
    if value is None or value == "":
        return None, None
    if isinstance(value, bool):
        # ``bool`` is a subclass of ``int``, so any future change that
        # broadens the accepted-types tuple to include ``int`` would
        # silently start accepting ``True`` / ``False`` here.  Reject
        # explicitly so the refactor is forced to think about it.
        return None, _unsupported_json_arg_error(what, value, expected)
    if isinstance(value, (dict, list)):
        return value, None
    if isinstance(value, str):
        try:
            return json.loads(value), None
        except json.JSONDecodeError as exc:
            # Wording carries both ``Invalid {what}``
            # AND ``must be valid JSON`` so the same
            # error helper satisfies both regression suites and so the
            # LLM sees consistent phrasing across all Notion tools. The
            # offset and the excerpt locate the broken character in a
            # payload of thousands.
            return None, (
                f"Error: Invalid {what} — must be valid JSON ({exc.msg} at "
                f"character {exc.pos}, line {exc.lineno} column {exc.colno}; "
                f"the parser stopped at >>> in: {_json_error_excerpt(value, exc.pos)})."
            )
    return None, _unsupported_json_arg_error(what, value, expected)


# Characters of the payload a JSON parse error quotes around the position.
_JSON_ERROR_EXCERPT_CHARS = 40


def _json_error_excerpt(text: str, pos: int) -> str:
    """The ``_JSON_ERROR_EXCERPT_CHARS`` characters of *text* around *pos*, ``>>>`` at *pos*.

    Control characters are written as escapes so the excerpt stays on one
    line, and ``...`` marks a side the excerpt cut.
    """
    pos = max(0, min(pos, len(text)))
    start = max(0, pos - _JSON_ERROR_EXCERPT_CHARS // 2)
    end = min(len(text), start + _JSON_ERROR_EXCERPT_CHARS)
    start = max(0, end - _JSON_ERROR_EXCERPT_CHARS)

    def visible(part: str) -> str:
        return "".join(
            ch if ch.isprintable() else ch.encode("unicode_escape").decode("ascii")
            for ch in part
        )

    return (
        f"{'...' if start else ''}{visible(text[start:pos])}>>>"
        f"{visible(text[pos:end])}{'...' if end < len(text) else ''}"
    )


def _unsupported_json_arg_error(
    what: str, value: Any, expected: _ExpectedJsonShape
) -> str:
    """Render the unsupported-type error string for ``_parse_json_arg``.

    The ``expected`` shape gates which JSON containers are mentioned so
    the LLM's retry payload actually matches the tool's real schema.
    The ``"must be a JSON object or JSON string"`` substring is
    deliberately preserved for ``"object"`` so the existing
    regression tests still match.
    """
    if expected == "object":
        shape = "JSON object or JSON string"
    elif expected == "array":
        shape = "JSON array or JSON string"
    else:
        shape = "JSON object, JSON array, or JSON string"
    return f"Error: {what} must be a {shape} (got {type(value).__name__})."


# What Notion shows for a person it will not name to this connection: without
# the integration's "Read user information" capability a user mention reads
# ``@Anonymous`` and every user object is cut to its id.
NOTION_UNNAMED_PERSON = "@Anonymous"

# The ids of the people a tool call met whose names Notion withheld; ``None``
# outside a call wrapped by ``with_unnamed_people_note`` (a KB sync renders the
# same text and keeps Notion's wording as is).
_unnamed_people: ContextVar[set[str] | None] = ContextVar(
    "notion_unnamed_people", default=None
)


def _is_unnamed_user(user: object) -> bool:
    """True for a user object Notion cut to its id (no name, no type)."""
    return (
        isinstance(user, dict)
        and bool(user.get("id"))
        and not user.get("name")
        and not user.get("type")
    )


def _record_unnamed_person(user: dict) -> None:
    seen = _unnamed_people.get()
    if seen is not None:
        seen.add(str(user["id"]))


def _record_unnamed_mention(item: object) -> None:
    if not isinstance(item, dict) or item.get("type") != "mention":
        return
    mention = item.get("mention") or {}
    user = mention.get("user")
    if mention.get("type") == "user" and _is_unnamed_user(user):
        _record_unnamed_person(user)


def _person_label(user: object, fallback: str = "?") -> str:
    """The name a people value shows, ``@Anonymous`` when Notion withheld it."""
    if not isinstance(user, dict):
        return fallback
    name = user.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    if _is_unnamed_user(user):
        _record_unnamed_person(user)
        return NOTION_UNNAMED_PERSON
    return fallback


def unnamed_people_note(count: int) -> str:
    """The line a tool result ends with when Notion withheld people's names."""
    who = "1 person" if count == 1 else f"{count} people"
    platform = get_cached_branding().name
    return (
        f"[Notion withheld the name of {who} in this result (shown as "
        f'"{NOTION_UNNAMED_PERSON}"): they are members of the workspace, but '
        "this Notion connection is not allowed to read user information, so "
        "do not report them as anonymous. To show their names, enable "
        "'Read user information without email addresses' in the Notion "
        "integration's Capabilities tab (notion.so/profile/integrations), "
        f"then reconnect this Notion integration in {platform} if it was "
        "authorized through OAuth.]"
    )


def user_capability_error(err: str, *, action: str) -> str:
    """A 403 from the ``/v1/users`` endpoints, worded as the capability it lacks.

    ``/users/me`` answers without that capability, so a green connection test
    says nothing about it.
    """
    platform = get_cached_branding().name
    return (
        f"Error: Notion refused to {action}: this Notion connection is not "
        "allowed to read user information. Enable 'Read user information "
        "without email addresses' (or 'including email addresses') in the "
        "Notion integration's Capabilities tab (notion.so/profile/integrations), "
        f"then reconnect this Notion integration in {platform} if it was "
        f"authorized through OAuth. Notion said: {err}"
    )


@contextmanager
def collect_unnamed_people() -> Iterator[set[str]]:
    """Collect the people Notion would not name during the block it wraps."""
    seen: set[str] = set()
    token = _unnamed_people.set(seen)
    try:
        yield seen
    finally:
        _unnamed_people.reset(token)


@contextmanager
def unnamed_people_not_collected() -> Iterator[None]:
    """Read values without noting withheld names, for an answer that is data.

    The note ``with_unnamed_people_note`` appends is prose for a reader; after
    a JSON answer it would make the JSON unreadable. A row read as data says a
    name was withheld itself (``{"id": ..., "name": null}``).
    """
    token = _unnamed_people.set(None)
    try:
        yield
    finally:
        _unnamed_people.reset(token)


def with_unnamed_people_note(
    handler: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    """*handler*, whose result ends with ``unnamed_people_note`` when it met any.

    An error result is returned as is: the people it may have named are not
    what it reports.
    """

    @functools.wraps(handler)
    async def noting(*args: Any, **kwargs: Any) -> Any:
        with collect_unnamed_people() as seen:
            result = await handler(*args, **kwargs)
        if not seen:
            return result
        note = unnamed_people_note(len(seen))
        if isinstance(result, ToolResult):
            if result.is_error:
                return result
            return dataclasses.replace(result, content=f"{result.content}\n\n{note}")
        if isinstance(result, str) and not is_error_shaped_result(result):
            return f"{result}\n\n{note}"
        return result

    return noting


def _rich_text_to_plain(rich_text: list) -> str:
    """Convert Notion rich_text array to plain string."""
    if _unnamed_people.get() is not None:
        for item in rich_text:
            _record_unnamed_mention(item)
    return "".join(t.get("plain_text", "") for t in rich_text)


# A link made in the Notion editor to one of the workspace's own pages is
# stored as a path (``/0b1c...``); the Markdown link grammar needs a URL.
_NOTION_APP_ORIGIN = "https://www.notion.so"
_MARKDOWN_FLAGS = ("bold", "italic", "strikethrough", "code")


def _rich_text_link(item: dict) -> str | None:
    """The URL a rich_text run links to, absolute, or ``None``.

    A run with no ``type`` is a text run, as the Notion API reads one in a
    request body.
    """
    url = item.get("href")
    if not url and (item.get("type") or "text") == "text":
        link = (item.get("text") or {}).get("link")
        url = link.get("url") if isinstance(link, dict) else None
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    if url.startswith("/"):
        url = _NOTION_APP_ORIGIN + url
    return url


# How a user mention reads: ``@[Name](user:<id>)``, so a writer checking its
# page tells a real mention from the literal text "@Name". The comment writer
# parses the same form back into a mention.
USER_MENTION_SCHEME = "user:"


def _user_mention_text(item: dict[str, Any]) -> str | None:
    """A user mention as ``@[Name](user:<id>)``; ``None`` for any other run."""
    if item.get("type") != "mention":
        return None
    mention = item.get("mention") or {}
    user = mention.get("user")
    if mention.get("type") != "user" or not isinstance(user, dict) or not user.get("id"):
        return None
    name = str(item.get("plain_text") or "").strip().removeprefix("@").strip()
    if not name:
        name = _person_label(user).removeprefix("@")
    name = name.replace("[", "(").replace("]", ")")
    return f"@[{name}]({USER_MENTION_SCHEME}{user['id']})"


def _rich_text_run(item: dict) -> tuple[str, tuple[bool, ...], str | None]:
    """A run's text, its Markdown-expressible flags and its link."""
    user_mention = _user_mention_text(item)
    if user_mention is not None:
        _record_unnamed_mention(item)
        annotations = item.get("annotations") or {}
        flags = tuple(bool(annotations.get(flag)) for flag in _MARKDOWN_FLAGS)
        return user_mention, flags, None
    if item.get("type") == "equation":
        expression = (item.get("equation") or {}).get("expression")
        text = f"${expression or item.get('plain_text', '')}$"
    elif item.get("type") == "text":
        text = (item.get("text") or {}).get("content") or item.get("plain_text", "")
    else:
        _record_unnamed_mention(item)
        text = item.get("plain_text", "")
    annotations = item.get("annotations") or {}
    flags = tuple(bool(annotations.get(flag)) for flag in _MARKDOWN_FLAGS)
    return text, flags, _rich_text_link(item)


def _markdown_link_target(url: str) -> str:
    """*url* in a form the ``[label](url)`` grammar reads to its end."""
    return url.replace("(", "%28").replace(")", "%29").replace(" ", "%20")


def _render_markdown_run(text: str, flags: tuple[bool, ...], link: str | None) -> str:
    core = text.strip()
    if not core:
        return text
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()) :]
    bold, italic, strikethrough, code = flags
    if code:
        core = f"`{core}`"
    if bold and italic:
        core = f"***{core}***"
    elif bold:
        core = f"**{core}**"
    elif italic:
        core = f"*{core}*"
    if strikethrough:
        core = f"~~{core}~~"
    if link:
        core = f"[{core}]({_markdown_link_target(link)})"
    return f"{lead}{core}{trail}"


def _rich_text_to_markdown(rich_text: list) -> str:
    """Render a rich_text array in the inline Markdown the Notion writer reads.

    Links come back as ``[label](url)`` and bold / italic / strikethrough /
    code as their markers - the grammar ``formatting._parse_inline_markdown``
    parses - so text an agent reads here and writes back keeps every link and
    format it did not touch. A user mention reads ``@[Name](user:<id>)``;
    another mention keeps its text, linked to what it points at when it has
    an address; underline and colour have no Markdown form and are not shown.
    """
    runs: list[list] = []
    for item in rich_text or []:
        if not isinstance(item, dict):
            continue
        text, flags, link = _rich_text_run(item)
        if runs and runs[-1][1] == flags and runs[-1][2] == link:
            runs[-1][0] += text
        else:
            runs.append([text, flags, link])
    return "".join(_render_markdown_run(*run) for run in runs)


def _extract_title(obj: dict) -> str:
    """Extract title from a Notion page or database object."""
    # Database title
    title_list = obj.get("title", [])
    if isinstance(title_list, list) and title_list:
        return _rich_text_to_plain(title_list)

    # Page title (in properties)
    props = obj.get("properties", {})
    for prop in props.values():
        if prop.get("type") == "title":
            return _rich_text_to_plain(prop.get("title", []))

    return "Untitled"


def _extract_notion_user_name(user: dict[str, Any] | None) -> str:
    """Extract the best available display name from a Notion user object."""
    if not isinstance(user, dict):
        return "Unknown author"
    name = user.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    person = user.get("person", {})
    if isinstance(person, dict):
        email = person.get("email")
        if isinstance(email, str):
            email = email.strip()
            if email:
                return email
    user_id = user.get("id")
    if isinstance(user_id, str):
        user_id = user_id.strip()
        if user_id:
            return user_id
    return "Unknown author"


def _extract_notion_error_code(resp: httpx.Response) -> str | None:
    """Extract Notion's structured error code when present."""
    try:
        body = resp.json()
    except (json.JSONDecodeError, ValueError):
        return None
    code = body.get("code")
    return code if isinstance(code, str) and code else None


def _format_comment_scope_error(
    err: str,
    *,
    action: str,
    required_scope: str,
    status_code: int | None = None,
    error_code: str | None = None,
) -> str:
    """Rewrite Notion's generic capability error into actionable comment guidance.

    This function is only called from the comment tools (``list_notion_comments``
    and ``add_notion_comment``), both of which target the ``/v1/comments``
    endpoint.  On that endpoint a 403 response — regardless of the specific
    Notion error code — is treated as a *heuristic* signal that the
    integration is missing the ``comments:read`` / ``comments:write``
    capability: Notion has been observed returning ``"unauthorized"``,
    ``"restricted_resource"``, ``"validation_error"``, and code-less 403
    bodies (``"Insufficient permissions for this endpoint."``) for that same
    root cause, so the check cannot rely on the error code.  A 403 can *also*
    mean the target page or block is simply not shared with the integration
    (independent of capabilities), which is why the rewritten message
    presents both remediations instead of asserting a single cause.

    ``error_code`` is currently unused (the code set proved unreliable — see
    above) but stays in the signature so the call sites keep forwarding the
    metadata that ``request_meta`` extracts, ready for code-specific handling
    if Notion's error codes become dependable.
    """
    lower = err.lower()
    missing_capability_markers = (
        "required capabilities",
        "comments capability",
        "capability enabled",
        "insufficient permissions for this endpoint",
    )
    # Heuristic: any 403 from the /v1/comments endpoint most commonly means
    # missing comment capabilities, but it can also mean the page/block is
    # not shared with the integration — the message below covers both fixes.
    # This helper is only called by the comment tools, so no other endpoint's
    # 403 can reach it.
    generic_403_signal = status_code == 403
    if any(marker in lower for marker in missing_capability_markers) or generic_403_signal:
        platform = get_cached_branding().name
        # Least-privilege guidance: name only the capability the failing
        # operation actually needs, and mention the sibling capability as
        # optional for users who plan to use both comment tools.
        if required_scope == "comments:read":
            needed = "'Read comments' (comments:read)"
            optional = (
                "'Insert comments' (comments:write) if the agent should "
                "also add comments"
            )
        else:
            needed = "'Insert comments' (comments:write)"
            optional = (
                "'Read comments' (comments:read) if the agent should "
                "also read comments"
            )
        return (
            f"Error: The connected Notion integration was not allowed to "
            f"{action}. It may be missing the '{required_scope}' capability, "
            "or the target page may not be shared with the integration. "
            "To fix: (1) open the Notion integration's settings, go to "
            f"the Capabilities tab, and enable {needed} — optionally also "
            f"enable {optional}; "
            "(2) make sure the target page is shared with the integration "
            "in Notion (page menu > Connections); (3) reconnect or "
            f"re-authorize this Notion integration in {platform} so the "
            "updated capabilities take effect."
        )
    return err


# What ``_block_to_text`` writes before a text block's content - the
# ``update_notion_block(content=...)`` path strips it from text an agent
# copied back from a read. A callout is written with its ``> [!...]`` marker
# instead (``_callout_marker``).
_TEXT_BLOCK_PREFIXES: dict[str, str] = {
    "paragraph": "",
    "heading_1": "# ",
    "heading_2": "## ",
    "heading_3": "### ",
    "heading_4": "#### ",
    "bulleted_list_item": "• ",
    "numbered_list_item": "1. ",
    "quote": "> ",
    "callout": "",
    "toggle": "",
}


def _block_color_suffix(data: dict) -> str:
    """A block's non-default colour as the writer's trailing ``{color=...}``, else ``""``."""
    color = data.get("color")
    if isinstance(color, str) and color and color != "default":
        return f" {{color={color}}}"
    return ""


def _icon_value(icon: object) -> str | None:
    """An emoji or external icon as ``icon=`` takes it; ``None`` for any other icon.

    An uploaded icon's URL is signed and expires, so it is not something to
    write back.
    """
    if not isinstance(icon, dict):
        return None
    if icon.get("type") == "emoji":
        return icon.get("emoji") or None
    if icon.get("type") == "external":
        return (icon.get("external") or {}).get("url") or None
    return None


def _callout_marker(data: dict) -> str:
    """A callout's ``> [!icon=... color=...]`` marker, the one the writer parses."""
    parts = []
    icon = _icon_value(data.get("icon"))
    if icon:
        parts.append(f"icon={icon}")
    color = data.get("color")
    if isinstance(color, str) and color and color != "default":
        parts.append(f"color={color}")
    return f"> [!{' '.join(parts) or 'callout'}]"


def _block_to_text(block: dict) -> str:
    """Convert a Notion block to the text an agent reads.

    Text is rendered as inline Markdown (``_rich_text_to_markdown``), so its
    links and formatting are visible; a code block keeps its raw text. A
    block's colour and a callout's icon are written the way the Markdown
    writer reads them (``{color=...}``, ``> [!icon=... color=...]``), so a
    block read and written back keeps them.
    """
    block_type = block.get("type", "")
    data = block.get(block_type, {})

    if block_type == "callout":
        text = _rich_text_to_markdown(data.get("rich_text", []))
        marker = _callout_marker(data)
        return f"{marker} {text}" if text else marker

    if block_type in _TEXT_BLOCK_PREFIXES:
        text = _rich_text_to_markdown(data.get("rich_text", []))
        return f"{_TEXT_BLOCK_PREFIXES[block_type]}{text}{_block_color_suffix(data)}"

    if block_type == "code":
        text = _rich_text_to_plain(data.get("rich_text", []))
        lang = data.get("language", "")
        return f"```{lang}\n{text}\n```"

    if block_type == "to_do":
        text = _rich_text_to_markdown(data.get("rich_text", []))
        checked = "☑" if data.get("checked") else "☐"
        return f"{checked} {text}{_block_color_suffix(data)}"

    if block_type == "divider":
        return "---"

    if block_type == "table_of_contents":
        return "[Table of Contents]"

    if block_type == "breadcrumb":
        return "[Breadcrumb]"

    if block_type == "child_page":
        return f"[Child page: {data.get('title', 'Untitled')}]"

    if block_type == "child_database":
        return f"[Child database: {data.get('title', 'Untitled')}]"

    if block_type == "image":
        img = data.get("file", data.get("external", {}))
        url = img.get("url", "")
        caption = _rich_text_to_markdown(data.get("caption", []))
        return f"[Image: {' - '.join(p for p in (caption, url) if p)}]"

    if block_type == "bookmark":
        url = data.get("url", "")
        caption = _rich_text_to_markdown(data.get("caption", []))
        return f"[Bookmark: {' - '.join(p for p in (caption, url) if p)}]"

    if block_type == "link_to_page":
        page_id = data.get("page_id", data.get("database_id", ""))
        return f"[Link to page: {page_id}]"

    if block_type == "embed":
        return f"[Embed: {data.get('url', '')}]"

    if block_type in ("video", "audio", "file", "pdf"):
        file_data = data.get("file", data.get("external", {}))
        url = file_data.get("url", "")
        return f"[{block_type.title()}: {url}]"

    if block_type == "equation":
        return f"$${data.get('expression', '')}$$"

    if block_type == "table_row":
        cells = data.get("cells", [])
        cell_texts = [_rich_text_to_markdown(cell) for cell in cells]
        return " | ".join(cell_texts)

    if block_type == "table":
        # Table blocks are containers; their content comes from child table_row blocks.
        width = data.get("table_width", "?")
        return f"[Table: {width} columns]"

    if block_type == "synced_block":
        # Synced blocks mirror content from another block.
        # Content is in child blocks (handled by recursive fetch).
        return ""

    if block_type in ("column_list", "column"):
        # Column containers — content is in child blocks (handled by recursive fetch).
        return ""

    return ""


def _block_body_key(block: dict) -> str | None:
    """The key a block's body lives under: its ``type``, else the block type key it carries."""
    kind = block.get("type")
    if isinstance(kind, str) and isinstance(block.get(kind), dict):
        return kind
    for key, value in block.items():
        if key in _NOTION_BLOCK_TYPES and isinstance(value, dict):
            return key
    return None


def _required_child_levels(block: Any) -> int:
    if not isinstance(block, dict):
        return 0
    return _REQUIRED_CHILD_LEVELS.get(_block_body_key(block) or "", 0)


@dataclasses.dataclass(frozen=True)
class DeferredChildren:
    """Children one request could not carry, appended to the block at ``path`` once it exists.

    ``path`` is the index of the top-level block, then the index of each child
    down to that block. ``drop_placeholder``: the block was sent with an empty
    paragraph because it must have a child, removed once these are appended.
    """

    path: tuple[int, ...]
    children: list
    drop_placeholder: bool = False


def _split_request_nesting(blocks: list) -> tuple[list, list[DeferredChildren]]:
    """*blocks* as one request may carry them, and the children cut off.

    Notion refuses a block nested deeper than ``_REQUEST_NESTING_LEVELS``
    below the request's top level, and more than ``_NOTION_BLOCK_LIMIT``
    blocks in one ``children`` array. Children deeper than that are cut at
    the block that holds them; a block that must be created with its
    children (a table, a column list) and would not fit is cut with every
    sibling after it, so the order of a column's content survives. The input
    is never mutated: a block that loses children is a copy.
    """
    deferred: list[DeferredChildren] = []

    def trim(block: Any, path: tuple[int, ...], level: int) -> Any:
        if not isinstance(block, dict):
            return block
        key = _block_body_key(block)
        body = block.get(key) if key else None
        children = body.get("children") if isinstance(body, dict) else None
        if not isinstance(children, list) or not children:
            return block
        kept: list = []
        rest: list = []
        if level >= _REQUEST_NESTING_LEVELS:
            rest = children
        else:
            for idx, child in enumerate(children):
                if (
                    idx >= _NOTION_BLOCK_LIMIT
                    or level + 1 + _required_child_levels(child) > _REQUEST_NESTING_LEVELS
                ):
                    rest = children[idx:]
                    break
                kept.append(trim(child, (*path, idx), level + 1))
        placeholder = not kept and bool(_REQUIRED_CHILD_LEVELS.get(key or ""))
        if placeholder:
            kept = [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}}]
        if rest:
            deferred.append(DeferredChildren(path, rest, drop_placeholder=placeholder))
        new_body = {k: v for k, v in body.items() if k != "children"}
        if kept:
            new_body["children"] = kept
        return {**block, key: new_body}

    return [trim(block, (idx,), 0) for idx, block in enumerate(blocks)], deferred


async def clear_stale_ref_counters(integration_id: str) -> int:
    """Delete ALL cross-run stale-reference counters for an integration.

    Operator escape hatch: called when the integration's
    connectivity test succeeds, so an admin who just repaired sharing can
    reset every quarantined Notion ID in one click instead of waiting for
    the recheck window or deleting Redis keys by hand.

    Returns the number of deleted keys.  Best-effort: any Redis error is
    logged at debug level and swallowed (returns 0).
    """
    if not integration_id:
        return 0
    pattern = f"{_STALE_REF_REDIS_KEY_PREFIX}{integration_id}:*"
    try:
        from app.core.redis import redis_client  # lazy import — keeps the test surface small

        r = await redis_client()
        deleted = 0
        batch: list = []
        async for redis_key in r.scan_iter(match=pattern, count=500):
            batch.append(redis_key)
            if len(batch) >= 500:
                deleted += await r.delete(*batch)
                batch = []
        if batch:
            deleted += await r.delete(*batch)
        if deleted:
            logger.info(
                "Notion: cleared %d stale-reference counter(s) for "
                "integration %s after a successful connectivity test",
                deleted,
                integration_id,
            )
        return int(deleted)
    except Exception as exc:
        logger.debug(
            "Notion stale-ref counter purge failed for integration %s: %s",
            integration_id,
            format_exception_message(exc),
        )
        return 0


class NotionContext:
    """Shared HTTP machinery for a single ``build_tools`` invocation.

    Holds the auth headers, the per-invocation concurrency semaphore, and the
    throttled request wrappers / page-traversal helpers that every Notion
    domain module shares. Lifted verbatim from the closures that previously
    lived inside ``build_tools``.

    ``integration_id`` is optional but enables cross-run stale-reference
    detection: when the same Notion resource (database, page, block) returns
    404 across *separate* runs for the same integration, a WARNING is emitted
    after ``_STALE_REF_WARN_THRESHOLD`` occurrences so operators can identify
    permanently-deleted or un-shared Notion IDs that silently degrade
    scheduled assignments, and the reference is reported to the run through
    ``app.integrations.stale_references`` (an assignment run fails on it;
    a chat turn listens to nothing).
    """

    def __init__(
        self,
        token: str,
        refresh_fn: Any = None,
        *,
        integration_id: str | None = None,
    ) -> None:
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": _NOTION_VERSION,
            "Content-Type": "application/json",
        }
        self.refresh_fn = refresh_fn
        self._integration_id: str | None = integration_id
        self._sem = asyncio.Semaphore(_NOTION_CONCURRENCY)
        self._missing_resources: dict[tuple[str, str], str] = {}
        # Normalised resource IDs whose cross-run stale counter was already
        # bumped during THIS run. The in-run missing cache is keyed per
        # (type, id), but agents routinely probe the same dead ID as a
        # database, then a page, then a block within one run — without this
        # set those probes would bump the shared Redis counter several times
        # in a single run and reach the warn threshold immediately, defeating
        # the cross-run semantics.
        self._stale_ref_bumped: set[str] = set()
        # Per-run cache of the cross-run stale pre-check, keyed by normalised
        # resource ID.  The value is the (possibly still-running) asyncio Task
        # performing the Redis round-trip, so SEQUENTIAL probes reuse the
        # completed result and CONCURRENT probes (e.g. parallel page-title
        # resolution via asyncio.gather) share a single in-flight Redis GET
        # instead of each issuing their own.
        self._stale_check_tasks: dict[str, asyncio.Task[bool]] = {}
        # Resource IDs whose stale counter had a non-zero value when the
        # pre-check read it this run, or that this run's own bump created.
        # When a subsequent call for such an ID SUCCEEDS, the counter is
        # deleted (fire-and-forget) so a recovered resource self-heals
        # immediately instead of carrying a ratcheted count where a single
        # future transient 404 would instantly re-arm a full suppression
        # window. The bump half matters for an id that 404s as one
        # type and answers as another in the same run (a data source id first
        # probed as a database): it is alive, and its counter must not grow.
        self._stale_counter_present: set[str] = set()
        # Counter value the pre-check read per resource ID this run: how many
        # separate runs a suppressed resource already failed in.
        self._stale_counts: dict[str, int] = {}
        # Resource IDs known SUSTAINED missing this run (a bump reached the
        # threshold, or the pre-check suppressed the call), whoever found it,
        # with their failing-run count. An incidental read that found one
        # reports nothing, but it leaves the id in the in-run missing cache and
        # spends the once-per-run bump - so a later call that ADDRESSES the
        # id reports it from here, whatever the read cached before.
        self._sustained_counts: dict[str, int] = {}

    @staticmethod
    def _resource_key_from_url(url: str) -> tuple[str, str] | None:
        """Extract a cache key ``(resource_type, resource_id)`` from a Notion API URL.

        The ID is normalised (dashes stripped, lowercased) because the same
        Notion UUID legitimately appears both dashed and dashless across tool
        calls — the cache must treat them as one resource.
        """
        parts = [p for p in urlparse(url).path.split("/") if p]
        if len(parts) < 3 or parts[0] != "v1":
            return None
        resource_type = parts[1]
        if resource_type not in {"databases", "data_sources", "blocks", "pages"}:
            return None
        resource_id = parts[2].strip().replace("-", "").lower()
        if not resource_id:
            return None
        return resource_type, resource_id

    @staticmethod
    def _is_unambiguous_resource_path(url: str) -> bool:
        """True when a GET 404 on this path can only mean the URL-path resource is gone.

        Bare ``/v1/{type}/{id}`` and ``/v1/blocks/{id}/children`` qualify —
        the ``children`` sub-path is accepted for **blocks only** (the sole
        children endpoint the Notion API exposes), so the predicate can never
        widen to unexpected ``/v1/{type}/{id}/children`` shapes.
        Deeper sub-resource paths do NOT: Notion returns the very same
        404 / ``object_not_found`` for ``/v1/pages/{id}/properties/{prop}``
        when only the *property* is unknown on a perfectly healthy page —
        caching that would wrongly blacklist the page for the rest of the run.
        """
        parts = [p for p in urlparse(url).path.split("/") if p]
        return len(parts) == 3 or (
            len(parts) == 4 and parts[1] == "blocks" and parts[3] == "children"
        )

    def _cached_missing_resource_error(self, url: str) -> str | None:
        key = self._resource_key_from_url(url)
        if not key:
            return None
        return self._missing_resources.get(key)

    def _cache_missing_resource_error(
        self,
        method: str,
        url: str,
        err: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
    ) -> str:
        """Cache a missing-resource failure and rewrite it with recovery guidance.

        Only GET failures on unambiguous resource paths are cached: on a GET
        of ``/v1/{type}/{id}`` (or ``.../children``) the 404 unambiguously
        refers to the resource in the URL path, whereas a POST/PATCH 404 can
        refer to a resource named in the request *body* (e.g.
        ``move_notion_page`` with a missing new parent, or ``append``
        targeting a missing ``after`` sibling), and a GET on a deeper
        sub-resource path (``/pages/{id}/properties/{prop}``) can 404 for the
        *property* on a healthy page — caching those would wrongly blacklist
        a path resource that still exists. Detection deliberately does NOT
        match the bare ``"could not find"`` substring: Notion 400 validation
        errors reuse that phrasing for *properties* ("Could not find property
        with name or id") on perfectly healthy databases, and caching those
        would block the resource for the rest of the run even after the
        caller fixes its payload. ``"resource not found"`` is safe —
        ``format_http_error`` only emits it for HTTP 404.

        The *cached* message is deliberately action-agnostic (it names only
        the resource, not the failing operation): the original ``err`` embeds
        the first call's ``action`` text ("while trying to list block
        children"), which would misreport what a *later* call through a
        different tool was attempting.
        """
        if method.lower() != "get":
            return err
        if not self._is_unambiguous_resource_path(url):
            return err
        key = self._resource_key_from_url(url)
        if not key:
            return err
        not_found = (
            status_code == 404
            or error_code == "object_not_found"
            or "resource not found" in err.lower()
        )
        if not not_found:
            return err
        resource_type, _ = key
        # Display the ID exactly as it appeared in the URL (the cache key is
        # normalised, which would mangle dashed UUIDs in the message).
        raw_id = [p for p in urlparse(url).path.split("/") if p][2]
        readable_type = _READABLE_RESOURCE_TYPES.get(resource_type, "resource")
        guidance = (
            f"Action required: this Notion {readable_type} ({raw_id}) "
            "is unavailable (deleted, moved, or not shared with the current "
            "integration). Re-share it in Notion or update the configured ID. "
            "Further calls to this missing resource are skipped for this run."
        )
        self._missing_resources[key] = (
            f"Error: skipped call to Notion {readable_type} {raw_id} — an "
            "earlier call in this run already found it missing "
            f"(404 / object_not_found).\n{guidance}"
        )
        return f"{err}\n{guidance}"

    async def _bump_stale_ref(
        self, key: tuple[str, str], url: str, *, report: bool = True
    ) -> None:
        """Increment the cross-run 404-failure counter for a Notion resource.

        Uses a Redis counter keyed by integration ID and normalised resource
        ID (type-agnostic: probing the same dead ID as a database, page, and
        block is ONE stale reference, and ``self._stale_ref_bumped`` caps the
        bump at once per run per ID). Once the counter reaches
        ``_STALE_REF_WARN_THRESHOLD`` a WARNING is logged on every further
        failing run so operators can identify Notion IDs that are permanently
        gone or no longer shared with the integration — and so a log-based
        alert stays active while the reference stays broken. The counter has
        a sliding TTL (``_STALE_REF_REDIS_TTL_SECS``); it is not reset by an
        intermittent success, only by TTL expiry, so the signal means
        "N failing runs within the window", not strictly consecutive runs.
        From the same threshold the reference is also reported to the run
        (``report_stale_reference``): a first miss may be the agent's
        own typo, a sustained one is the run's configuration. *report* is
        False for an id the run only READ off another resource (a relation
        target, a child block): it is counted, never held against the run.

        Awaited by the callers, not fire-and-forget: the run must learn about
        the reference before the call returns - before a probe of the same id
        as another type can prove it alive, and before the run ends. Bounded
        by ``_STALE_REF_BUMP_TIMEOUT_SECS`` for that reason.

        Best-effort: a Redis error or timeout is logged at debug level and
        swallowed so an unavailable Redis instance never disrupts normal tool
        execution.
        """
        if not self._integration_id:
            return
        resource_type, resource_id = key
        if resource_id in self._stale_ref_bumped:
            return
        self._stale_ref_bumped.add(resource_id)
        redis_key = (
            f"{_STALE_REF_REDIS_KEY_PREFIX}{self._integration_id}:{resource_id}"
        )
        try:
            count = await asyncio.wait_for(
                self._incr_stale_counter(redis_key, resource_id),
                timeout=_STALE_REF_BUMP_TIMEOUT_SECS,
            )
        except Exception as exc:
            # Best-effort: a Redis failure must never surface to the caller —
            # the stale counter is observability, not functionality.
            logger.debug(
                "Notion stale-ref counter bump failed for %s: %s",
                redis_key,
                format_exception_message(exc),
            )
            return
        if count >= _STALE_REF_WARN_THRESHOLD:
            self._sustained_counts[resource_id] = count
            readable_type = _READABLE_RESOURCE_TYPES.get(resource_type, "resource")
            logger.warning(
                "Notion sustained stale reference: Notion %s %s has returned "
                "404/object_not_found %d time(s) across separate runs for "
                "integration %s. The resource is likely permanently deleted "
                "or no longer shared with this integration. Update the agent "
                "prompt or assignment to remove or replace this Notion ID.",
                readable_type,
                resource_id,
                count,
                self._integration_id,
            )
            if report:
                self._report_sustained_stale_ref(key, url, count)

    async def _incr_stale_counter(self, redis_key: str, resource_id: str) -> int:
        """INCR the counter and re-apply its sliding TTL; return the new count."""
        from app.core.redis import redis_client  # lazy import — keeps the test surface small

        r = await redis_client()
        count = int(await r.incr(redis_key))
        self._stale_counter_present.add(resource_id)
        # Re-apply a sliding TTL on every bump so the window resets if the
        # resource starts working again (counter decays to zero after the
        # TTL with no new failures).
        await r.expire(redis_key, _STALE_REF_REDIS_TTL_SECS)
        return count

    async def _is_cross_run_stale(self, key: tuple[str, str]) -> bool:
        """Best-effort Redis pre-check: True when this resource's stale counter >= threshold.

        Called before making a GET on an unambiguous resource path to decide
        whether to short-circuit the HTTP call entirely.  When the counter has
        already reached ``_STALE_REF_WARN_THRESHOLD`` in previous runs, the
        resource is treated as stale (sustained 404s) and the API call is
        suppressed.

        The per-ID result is memoised in ``_stale_check_tasks`` as an asyncio
        Task: sequential probes reuse the completed result and CONCURRENT
        probes to the same resource ID (e.g. parallel title resolution via
        ``asyncio.gather``) await the same in-flight Task — so the pre-check
        costs at most one Redis round-trip per resource ID per run.

        Returns ``False`` on any error (Redis unavailable, no integration_id)
        so a Redis outage can never become a hard blocker on a live resource
        (fail-open semantics).
        """
        if not self._integration_id:
            return False
        _, resource_id = key
        task = self._stale_check_tasks.get(resource_id)
        if task is None:
            task = asyncio.ensure_future(self._check_stale_counter(resource_id))
            self._stale_check_tasks[resource_id] = task
        return await task

    async def _check_stale_counter(self, resource_id: str) -> bool:
        """One Redis round-trip deciding whether *resource_id* is cross-run stale.

        Recovery probe: suppressed calls never bump the counter, so the key's
        remaining TTL encodes the time since the last real 404
        (``age = _STALE_REF_REDIS_TTL_SECS - remaining``).  Once that age
        reaches ``_STALE_REF_RECHECK_SECS`` the pre-check stands down and lets
        the call through so a re-shared/restored resource can recover without
        waiting the full counter TTL — a still-dead resource just 404s again,
        re-bumps the counter (sliding TTL), and re-arms suppression for
        another window.  Never raises (fail-open on any error).
        """
        redis_key = (
            f"{_STALE_REF_REDIS_KEY_PREFIX}{self._integration_id}:{resource_id}"
        )
        try:
            from app.core.redis import redis_client  # lazy import — keeps the test surface small

            r = await redis_client()
            # Pipeline GET + TTL so the pre-check is genuinely ONE Redis
            # round-trip (transaction=False: plain pipelining, no MULTI/EXEC
            # needed for two independent reads).
            pipe = r.pipeline(transaction=False)
            pipe.get(redis_key)
            pipe.ttl(redis_key)
            raw, remaining = await pipe.execute()
            if raw is not None and int(raw) > 0:
                # Remember that a counter exists for this ID so a later
                # SUCCESSFUL call this run can delete it (self-heal).
                self._stale_counter_present.add(resource_id)
                self._stale_counts[resource_id] = int(raw)
            if raw is not None and int(raw) >= _STALE_REF_WARN_THRESHOLD:
                # Fail open when the TTL is unreadable (-1 no expiry, -2 gone,
                # or a client quirk): a live probe is always the safe default.
                if not isinstance(remaining, int) or remaining <= 0:
                    return False
                # Suppress only while the last failing bump is younger than
                # the recheck window; once the window is reached, let this
                # run probe the API to detect recovery instead of
                # suppressing forever.
                return _STALE_REF_REDIS_TTL_SECS - remaining < _STALE_REF_RECHECK_SECS
        except Exception as exc:
            # Best-effort: a Redis failure must never surface to the caller —
            # the stale pre-check is observability/optimisation, not
            # correctness.  Let the call proceed normally so the resource can
            # surface a fresh 404 (and bump the counter as usual) instead of
            # silently disappearing.
            logger.debug(
                "Notion stale-ref pre-check failed for %s: %s",
                redis_key,
                format_exception_message(exc),
            )
        return False

    async def _clear_stale_ref(self, resource_id: str) -> None:
        """Delete the cross-run stale counter for a recovered resource.

        Fired (fire-and-forget) when a call for a resource whose counter was
        seen non-zero this run SUCCEEDS: the resource is demonstrably alive,
        so the accumulated failure count must not linger.  Without this, the
        counter only decays via the 14-day sliding TTL and a single future
        transient 404 would increment it straight past the threshold,
        re-arming a full suppression window against a healthy resource.
        Best-effort: a Redis error is logged at debug level and
        swallowed.
        """
        redis_key = (
            f"{_STALE_REF_REDIS_KEY_PREFIX}{self._integration_id}:{resource_id}"
        )
        try:
            from app.core.redis import redis_client  # lazy import — keeps the test surface small

            r = await redis_client()
            await r.delete(redis_key)
            logger.info(
                "Notion: cleared stale-reference counter for recovered "
                "resource %s (integration %s)",
                resource_id,
                self._integration_id,
            )
        except Exception as exc:
            logger.debug(
                "Notion stale-ref counter clear failed for %s: %s",
                redis_key,
                format_exception_message(exc),
            )

    def _self_heal_stale_ref_on_success(self, method: str, url: str) -> None:
        """Self-heal after an unambiguous-path GET proved its resource exists.

        Called from the success paths of :meth:`request` / :meth:`request_meta`,
        and from their failure paths when the failure itself names an existing
        resource (a 400 "is a page, not a database", see
        :func:`_names_existing_resource`). See :meth:`note_resource_alive`.
        """
        if not self._integration_id or method.lower() != "get":
            return
        if not self._is_unambiguous_resource_path(url):
            return
        key = self._resource_key_from_url(url)
        if key:
            self.note_resource_alive(key[1])

    def note_resource_alive(self, resource_id: str) -> None:
        """*resource_id* demonstrably exists: it answered, or a write naming it succeeded.

        A stale reference reported for it earlier in the run is withdrawn -
        a page parent the create-page type probes missed is alive
        once the page is created under it. The cross-run counter is deleted
        (fire-and-forget) only when one exists for it this run
        (``_stale_counter_present``), so the common case (healthy resource,
        no counter) costs zero extra Redis operations.
        """
        resource_id = str(resource_id or "").strip().replace("-", "").lower()
        if not self._integration_id or not resource_id:
            return
        confirm_reference_alive(
            provider="notion", integration_id=self._integration_id, key=resource_id
        )
        if resource_id not in self._stale_counter_present:
            return
        self._stale_counter_present.discard(resource_id)
        asyncio.ensure_future(self._clear_stale_ref(resource_id))

    async def _suppress_if_cross_run_stale(
        self, method: str, url: str, *, report: bool = True
    ) -> str | None:
        """Pre-call cross-run stale guard shared by ``request``/``request_meta``.

        For GET requests on unambiguous resource paths, checks the Redis
        counter before touching the API.  When the resource has already
        exceeded the stale threshold across previous runs (and is not yet due
        for a recovery probe), returns the synthetic stale error — after
        pre-populating the in-run missing cache so further calls within this
        run also skip without another Redis round-trip — logs a WARNING and,
        unless *report* is False (an id only read off another resource),
        reports the sustained reference to the run: this run used it again.
        Returns ``None`` when the call should proceed normally.
        """
        if method.lower() != "get" or not self._is_unambiguous_resource_path(url):
            return None
        key = self._resource_key_from_url(url)
        if not key or not await self._is_cross_run_stale(key):
            return None
        stale_err = self._make_cross_run_stale_error(url, key)
        self._missing_resources[key] = stale_err
        logger.warning(
            "Notion: suppressing call to cross-run-stale resource "
            "(%s %s) — stale counter has reached threshold; "
            "API call skipped",
            method.upper(),
            urlparse(url).path,
        )
        self._sustained_counts[key[1]] = self._stale_counts.get(
            key[1], _STALE_REF_WARN_THRESHOLD
        )
        if report:
            self._report_sustained_stale_ref(key, url, self._sustained_counts[key[1]])
        return stale_err

    def _report_if_known_sustained(self, method: str, url: str) -> None:
        """Report a resource this run already knows is sustained missing.

        Called when a call that ADDRESSES the resource ends on a miss the
        detector did not report itself - the in-run missing cache answered, or
        the once-per-run bump was already spent - typically because an
        incidental read (a relation title, a preview) found it first.
        """
        if method.lower() != "get":
            return
        key = self._resource_key_from_url(url)
        if key is None or key[1] not in self._sustained_counts:
            return
        self._report_sustained_stale_ref(key, url, self._sustained_counts[key[1]])

    def _report_sustained_stale_ref(
        self, key: tuple[str, str], url: str, failing_runs: int
    ) -> None:
        """Report a sustained stale reference to the run, if one is listening."""
        if not self._integration_id:
            return
        resource_type, resource_id = key
        report_stale_reference(
            provider="notion",
            provider_label=_PROVIDER,
            integration_id=self._integration_id,
            resource_type=_READABLE_RESOURCE_TYPES.get(resource_type, "resource"),
            resource_id=[p for p in urlparse(url).path.split("/") if p][2],
            failing_runs=failing_runs,
            key=resource_id,
        )

    def _make_cross_run_stale_error(self, url: str, key: tuple[str, str]) -> str:
        """Build the synthetic error string for a cross-run-stale resource.

        The message must be HONEST about what happened: the Notion API was
        NOT called on this attempt — this is a cached verdict from earlier
        failing runs.  The 2026-07-14 incident showed the previous
        wording ("has likely been deleted") led the agent to report "still
        404" to the user while never probing, derailing the debugging
        session.  The message also offers a live fallback (search) since
        the search endpoint is never suppressed.
        """
        resource_type, _ = key
        raw_id = [p for p in urlparse(url).path.split("/") if p][2]
        readable_type = _READABLE_RESOURCE_TYPES.get(resource_type, "resource")
        recheck_hours = max(1, _STALE_REF_RECHECK_SECS // 3600)
        return (
            f"Error: this call was suppressed WITHOUT contacting the Notion "
            f"API — a cached verdict, not a live check. Notion {readable_type} "
            f"{raw_id} is considered stale because it returned "
            f"404/object_not_found across {_STALE_REF_WARN_THRESHOLD}+ "
            "separate runs (deleted, moved, or not shared with this "
            "integration at the time). "
            "What you can do NOW: use search_notion or "
            "list_notion_databases to locate the resource by name — search "
            "is never suppressed and returns current data. "
            "If the resource has been restored or re-shared, suppression "
            f"clears automatically (a live probe runs within {recheck_hours} "
            "hour(s), and any successful access resets the counter "
            "immediately); an admin can also clear it by running the "
            "integration's connectivity test."
        )

    def client(self) -> AutoRefreshClient:
        return AutoRefreshClient(self.headers, self.refresh_fn)

    async def request(
        self,
        client: AutoRefreshClient,
        method: str,
        url: str,
        provider: str,
        action: str,
        *,
        report_stale: bool = True,
        count_stale: bool = True,
        **kwargs: Any,
    ) -> tuple[Any, str | None]:
        """Rate-limited wrapper around safe_request (max _NOTION_CONCURRENCY in-flight).

        *report_stale* is False for an id the run did not address but READ
        off another resource - a relation target, a child block, a search hit
        - whose miss is data, not the run's configuration: it still feeds the
        cross-run counter, it is never reported to the run.

        *count_stale* is False for a probe whose 404 is routine - the
        ``/databases/{id}`` probe of an id that is a data_source_id: the miss
        is cached for the run but never feeds the cross-run counter, which
        would otherwise grow for a healthy resource on every run. The next
        probe of the same id counts it when the id is gone.
        """
        cached_err = self._cached_missing_resource_error(url)
        if cached_err:
            logger.info(
                "Notion: skipping call to known-missing resource (%s %s)",
                method.upper(),
                urlparse(url).path,
            )
            if report_stale:
                self._report_if_known_sustained(method, url)
            if kwargs.get("expected_statuses") is not None:
                # Callers that opt into ``safe_request``'s
                # ``expected_statuses`` contract branch on
                # ``resp.status_code`` — synthesize the 404 the skipped
                # call would have produced so the cached fast path honours
                # the same contract (mirrors ``request_meta`` returning
                # ``404 / object_not_found`` metadata on its cached path).
                synthetic = httpx.Response(
                    404,
                    json={
                        "object": "error",
                        "status": 404,
                        "code": "object_not_found",
                        "message": cached_err,
                    },
                )
                return synthetic, cached_err
            return None, cached_err
        # Cross-run stale short-circuit: suppress the call before touching the
        # API when the resource has already exceeded the stale threshold
        # across previous runs (see _suppress_if_cross_run_stale).
        stale_err = await self._suppress_if_cross_run_stale(
            method, url, report=report_stale
        )
        if stale_err:
            if kwargs.get("expected_statuses") is not None:
                synthetic = httpx.Response(
                    404,
                    json={
                        "object": "error",
                        "status": 404,
                        "code": "object_not_found",
                        "message": stale_err,
                    },
                )
                return synthetic, stale_err
            return None, stale_err
        async with self._sem:
            resp, err = await safe_request(client, method, url, provider, action, **kwargs)
        # The slot is released before any Redis round-trip below: a slow
        # counter write must not queue the next Notion call behind it.
        if err:
            # ``resp`` is passed through untouched so the opt-in
            # ``expected_statuses`` contract of ``safe_request`` (caller
            # branches on ``resp.status_code``) keeps working.
            status_code = resp.status_code if resp is not None else None
            key = self._resource_key_from_url(url)
            was_cached_before = key is not None and key in self._missing_resources
            result_err = self._cache_missing_resource_error(
                method, url, err, status_code=status_code
            )
            # Bump the cross-run stale counter when this resource was JUST
            # added to the in-run missing cache (once per run per resource
            # — subsequent calls hit the cached fast-path above).
            if key is not None and not was_cached_before and key in self._missing_resources:
                if count_stale:
                    await self._bump_stale_ref(key, url, report=report_stale)
                    if report_stale:
                        # The bump is once per run per id: an incidental read
                        # may have spent it on this id already.
                        self._report_if_known_sustained(method, url)
            elif _names_existing_resource(resp, err):
                self._self_heal_stale_ref_on_success(method, url)
            return resp, result_err
        # Success: self-heal the cross-run stale counter for a resource
        # that previously accumulated 404s.
        self._self_heal_stale_ref_on_success(method, url)
        return resp, None

    async def request_meta(
        self,
        client: AutoRefreshClient,
        method: str,
        url: str,
        provider: str,
        action: str,
        *,
        report_stale: bool = True,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None, int | None, str | None]:
        """Rate-limited request helper that preserves HTTP metadata on failures.

        Returns ``(response, None, None, None)`` on a successful 2xx
        response, or ``(None, error_string, status_code, notion_error_code)``
        when the request fails — ``status_code`` and ``notion_error_code``
        are populated for HTTP errors only (both ``None`` for network /
        transport errors) so callers can detect Notion's specific
        ``restricted_resource`` / capability-missing responses and rewrite
        them into actionable tool errors. *report_stale* as in
        :meth:`request`.

        ``log_path`` matches ``safe_request``'s convention
        (``urlparse(url).path`` — query string and fragment already
        stripped) so the WARNING log format stays consistent with every
        other integration request, instead of special-casing one
        endpoint.
        """
        cached_err = self._cached_missing_resource_error(url)
        if cached_err:
            logger.info(
                "Notion: skipping call to known-missing resource (%s %s)",
                method.upper(),
                urlparse(url).path,
            )
            if report_stale:
                self._report_if_known_sustained(method, url)
            return None, cached_err, 404, "object_not_found"
        # Cross-run stale short-circuit (same guard as request()).
        stale_err = await self._suppress_if_cross_run_stale(
            method, url, report=report_stale
        )
        if stale_err:
            return None, stale_err, 404, "object_not_found"
        log_path = urlparse(url).path
        async with self._sem:
            try:
                resp: httpx.Response = await getattr(client, method)(url, **kwargs)
            except httpx.HTTPError as exc:
                err = format_network_error(exc, provider, action)
                logger.warning(
                    "%s integration network error (%s %s): %s",
                    provider,
                    method.upper(),
                    log_path,
                    err,
                )
                return None, err, None, None
        # The slot is released before any Redis round-trip below (see request()).
        if resp.is_success:
            # Success: self-heal the cross-run stale counter for a
            # resource that previously accumulated 404s.
            self._self_heal_stale_ref_on_success(method, url)
            return resp, None, None, None
        err = format_http_error(resp, provider, action)
        logger.warning(
            "%s integration HTTP error (%s %s): %s",
            provider,
            method.upper(),
            log_path,
            err,
        )
        status_code = resp.status_code
        error_code = _extract_notion_error_code(resp)
        key = self._resource_key_from_url(url)
        was_cached_before = key is not None and key in self._missing_resources
        result_err = self._cache_missing_resource_error(
            method,
            url,
            err,
            status_code=status_code,
            error_code=error_code,
        )
        if key is not None and not was_cached_before and key in self._missing_resources:
            await self._bump_stale_ref(key, url, report=report_stale)
            if report_stale:
                self._report_if_known_sustained(method, url)
        elif _names_existing_resource(resp, err):
            self._self_heal_stale_ref_on_success(method, url)
        return None, result_err, status_code, error_code

    async def fetch_page_preview(
        self,
        client: AutoRefreshClient,
        page_id: str,
        max_chars: int = _SEARCH_PREVIEW_CHARS,
    ) -> str:
        """Fetch the first few blocks of a page and return a text preview."""
        resp, err = await self.request(
            client,
            "get",
            f"{_BASE_URL}/blocks/{page_id}/children",
            _PROVIDER,
            "fetch page preview",
            report_stale=False,
            params={"page_size": "3"},
        )
        if err:
            return ""
        blocks = resp.json().get("results", [])
        parts = []
        for block in blocks:
            text = _block_to_text(block)
            if text:
                parts.append(text)
        preview = "\n".join(parts)
        if len(preview) > max_chars:
            preview = preview[:max_chars] + "…"
        return preview

    async def fetch_all_blocks(
        self,
        client: AutoRefreshClient,
        block_id: str,
        *,
        depth: int = 0,
    ) -> list[dict]:
        """Recursively fetch all blocks under a parent, up to _MAX_BLOCK_DEPTH.

        Children at each depth level are fetched concurrently (capped by
        _NOTION_CONCURRENCY semaphore to avoid Notion rate limits). Every id
        here is read off a page the caller already fetched, so a miss is never
        reported as the run's stale reference.
        """
        ordered: list[dict] = []
        params: dict[str, str] = {"page_size": "100"}
        while True:
            resp, err = await self.request(
                client,
                "get",
                f"{_BASE_URL}/blocks/{block_id}/children",
                _PROVIDER,
                "fetch blocks",
                report_stale=False,
                params=params,
            )
            if err:
                break
            data = resp.json()
            results = data.get("results", [])

            # Identify blocks that need children fetched
            expandable = [
                b for b in results if b.get("has_children") and depth < _MAX_BLOCK_DEPTH
            ]

            # Fetch all children in parallel instead of one-by-one
            children_map: dict[str, list[dict]] = {}
            if expandable:
                child_lists = await asyncio.gather(
                    *[
                        self.fetch_all_blocks(client, b["id"], depth=depth + 1)
                        for b in expandable
                    ]
                )
                children_map = dict(
                    zip(
                        [b["id"] for b in expandable],
                        child_lists,
                        strict=True,
                    )
                )

            # Build ordered list: each block followed by its children
            for block in results:
                ordered.append(block)
                if block["id"] in children_map:
                    for child in children_map[block["id"]]:
                        child.setdefault("_depth", depth + 1)
                        ordered.append(child)

            if not data.get("has_more") or not data.get("next_cursor"):
                break
            params["start_cursor"] = data["next_cursor"]
        return ordered

    async def append_block_children(
        self,
        client: AutoRefreshClient,
        parent_id: str,
        children: list,
        *,
        after_block_id: str = "",
        action: str = "append blocks",
    ) -> tuple[int, str | None]:
        """Append *children* under *parent_id*, whatever their count and nesting.

        Returns ``(appended_top_level_blocks, error)``. Batches of
        ``_NOTION_BLOCK_LIMIT``; with *after_block_id* the first batch goes
        right after that block and each next one after the previous batch's
        last block, so order holds at the requested position (Notion
        otherwise appends every batch at the end). Children nested deeper
        than one request carries are appended under the blocks just created
        (``_split_request_nesting``), so one Markdown call builds a column
        layout of callouts.
        """
        appended = 0
        current_after = after_block_id
        for start in range(0, len(children), _NOTION_BLOCK_LIMIT):
            sendable, deferred = _split_request_nesting(children[start : start + _NOTION_BLOCK_LIMIT])
            body: dict[str, Any] = {"children": sendable}
            if current_after:
                body["after"] = current_after
            resp, err = await self.request(
                client, "patch", f"{_BASE_URL}/blocks/{parent_id}/children", _PROVIDER, action, json=body
            )
            if err:
                return appended, err
            appended += len(sendable)
            try:
                created = [r.get("id") for r in (resp.json().get("results") or []) if isinstance(r, dict)]
            except (ValueError, AttributeError):
                created = []
            if after_block_id and created and created[-1]:
                current_after = created[-1]
            if deferred:
                nested_err = await self.append_deferred_children(
                    client, deferred, created_ids=created, action=action
                )
                if nested_err:
                    return appended, nested_err
        return appended, None

    async def append_deferred_children(
        self,
        client: AutoRefreshClient,
        deferred: list[DeferredChildren],
        *,
        created_ids: list | None = None,
        parent_id: str = "",
        action: str = "append blocks",
    ) -> str | None:
        """Append what ``_split_request_nesting`` cut off, under the blocks a request created.

        *created_ids* are the ids of the request's top-level blocks, in order;
        without them they are read from *parent_id*'s children (a page just
        created, whose children are exactly those blocks). Returns an error
        naming what could not be added, else ``None``.
        """
        listed: dict[str, list[str]] = {}

        async def child_ids(block_id: str) -> tuple[list[str], str | None]:
            if block_id in listed:
                return listed[block_id], None
            ids: list[str] = []
            params: dict[str, str] = {"page_size": "100"}
            while True:
                resp, err = await self.request(
                    client,
                    "get",
                    f"{_BASE_URL}/blocks/{block_id}/children",
                    _PROVIDER,
                    "read back the blocks just added",
                    report_stale=False,
                    params=params,
                )
                if err:
                    return ids, err
                try:
                    data = resp.json()
                except ValueError:
                    return ids, f"Error: Notion returned an unreadable listing of block {block_id}."
                ids.extend(r.get("id", "") for r in data.get("results", []) if isinstance(r, dict))
                if not data.get("has_more") or not data.get("next_cursor"):
                    break
                params["start_cursor"] = data["next_cursor"]
            listed[block_id] = ids
            return ids, None

        tops = list(created_ids or [])
        if not tops and parent_id:
            tops, err = await child_ids(parent_id)
            if err:
                return err
        for entry in deferred:
            if entry.path[0] >= len(tops) or not tops[entry.path[0]]:
                return (
                    "Error: the nested blocks could not be added: Notion did not "
                    "return the id of the block they belong to."
                )
            target = tops[entry.path[0]]
            for index in entry.path[1:]:
                ids, err = await child_ids(target)
                if err:
                    return err
                if index >= len(ids) or not ids[index]:
                    return (
                        f"Error: the nested blocks could not be added: block {target} "
                        f"has no child at position {index + 1}."
                    )
                target = ids[index]
            _, err = await self.append_block_children(client, target, entry.children, action=action)
            if err:
                return err
            if entry.drop_placeholder:
                listed.pop(target, None)
                ids, err = await child_ids(target)
                if err:
                    return err
                if ids and ids[0]:
                    _, err = await self.request(
                        client, "delete", f"{_BASE_URL}/blocks/{ids[0]}", _PROVIDER, "remove a placeholder block"
                    )
                    if err:
                        return err
        return None

    async def resolve_relation_titles(
        self,
        client: AutoRefreshClient,
        props: dict,
        *,
        limit: int = 20,
    ) -> dict[str, str]:
        """Resolve relation property page IDs to human-readable titles.

        Fetches page metadata in parallel (up to *limit* pages) and returns
        a mapping ``{page_id: title}``. A related page in a database the
        integration cannot see 404s on every run: that is data, never
        reported as the run's stale reference.
        """
        page_ids: list[str] = []
        seen: set[str] = set()
        for prop in props.values():
            if prop.get("type") == "relation":
                for rel in prop.get("relation", []):
                    pid = rel.get("id", "")
                    if pid and pid not in seen:
                        seen.add(pid)
                        page_ids.append(pid)
        if not page_ids:
            return {}

        # Cap to avoid excessive API calls
        page_ids = page_ids[:limit]

        async def _get_title(pid: str) -> tuple[str, str]:
            resp, err = await self.request(
                client,
                "get",
                f"{_BASE_URL}/pages/{pid}",
                _PROVIDER,
                "resolve page title",
                report_stale=False,
            )
            if err:
                return (pid, pid[:8])
            return (pid, _extract_title(resp.json()))

        results = await asyncio.gather(*[_get_title(pid) for pid in page_ids])
        return dict(results)

    async def resolve_data_source_id(
        self,
        client: AutoRefreshClient,
        database_id: str,
    ) -> str:
        """Resolve a database_id to its primary data_source_id (API 2025-09-03).

        The 2025-09-03 API restructured databases: GET /v1/databases/{id} now
        returns a list of ``data_sources`` instead of the schema.  Most
        operations (query, retrieve schema, update properties) now require
        a ``data_source_id``.

        Resolution strategy (max 2 HTTP calls):
        1. ``GET /v1/databases/{id}`` — extract ``data_sources[0].id``
        2. If that fails, ``GET /v1/data_sources/{id}`` — the input may
           already be a data_source_id (the two ID types are NOT
           interchangeable per the Notion API docs).

        Returns ``_PAGE_NOT_DATABASE_SENTINEL`` when Notion confirms the ID
        belongs to a **page**, not a database (HTTP 400 "is a page, not a
        database").  Callers must detect this sentinel and return an
        actionable error such as "use get_notion_page" instead of
        attempting further database operations.

        Returns ``_RESOLUTION_FAILED_SENTINEL`` when BOTH probes fail with
        404/object_not_found — the ID is unavailable (deleted, moved, or no
        longer shared with the integration).  Callers must detect this
        sentinel and return an actionable error directing the agent to
        ``search_notion`` / ``list_notion_databases``.  Non-404 failures
        (auth, network, 5xx) fall back to returning the original ID so the
        caller's actual API call surfaces the real error.
        """
        # ``expected_statuses`` keeps two routine misses of this probe out of
        # the warning stream: the 400 of a page id (below) and the 404 of an
        # id that is already a data_source_id, which step 2 resolves. A 404 on
        # both probes still warns below. ``count_stale=False`` keeps that 404
        # out of the cross-run stale counter too: step 2 counts the id once
        # when it is gone.
        resp, err = await self.request(
            client,
            "get",
            f"{_BASE_URL}/databases/{database_id}",
            _PROVIDER,
            "resolve data source",
            expected_statuses={400, 404},
            count_stale=False,
        )
        if not err:
            data = resp.json()
            data_sources = data.get("data_sources", [])
            if data_sources:
                ds_id = data_sources[0]["id"]
                ds_name = data_sources[0].get("name", "")
                logger.info(
                    "Notion: resolved database %s → data_source %s (%s)",
                    database_id,
                    ds_id,
                    ds_name,
                )
                return ds_id
        elif _is_page_not_database_400(resp):
            # Notion returns 400 "is a page, not a database" when the caller
            # accidentally passes a page ID where a database ID is expected.
            # Short-circuit immediately — step 2 would just 404 — and return
            # a sentinel so the calling tool can emit an actionable error.
            logger.warning(
                "Notion: %s is a page, not a database — "
                "database operations are not available for this ID; "
                "use get_notion_page to read its content",
                database_id,
            )
            return _PAGE_NOT_DATABASE_SENTINEL
        elif resp is not None and resp.status_code == 400:
            # ``expected_statuses`` suppresses safe_request's own WARNING for
            # every 400 from this probe (the benign daily page-not-database
            # case must stay out of the warning stream). Any OTHER 400 is
            # unexpected here — surface it explicitly so it stays diagnosable
            # before falling through to the data_source probe.
            logger.warning(
                "Notion: unexpected 400 while resolving data source for %s "
                "(not the page-not-database case): %s",
                database_id,
                err,
            )

        # Whether the databases probe failed with a genuine not-found (as
        # opposed to succeeding with an empty ``data_sources`` list, or
        # failing with an auth / network / 5xx error).
        db_probe_not_found = _is_not_found_failure(resp, err)

        # ``expected_statuses=set()`` opts into receiving the failed response
        # (so the 404 check below can read ``status_code``) while keeping
        # every non-2xx in the warning log.
        ds_resp, ds_err = await self.request(
            client,
            "get",
            f"{_BASE_URL}/data_sources/{database_id}",
            _PROVIDER,
            "check data source",
            expected_statuses=set(),
        )
        if not ds_err:
            logger.info(
                "Notion: %s is already a valid data_source_id",
                database_id,
            )
            return database_id

        if db_probe_not_found and _is_not_found_failure(ds_resp, ds_err):
            logger.warning(
                "Notion: could not resolve data_source_id for %s — "
                "database lookup and data_source lookup both returned "
                "404/object_not_found. The resource is likely deleted, "
                "moved, or not shared with this integration. Returning "
                "_RESOLUTION_FAILED_SENTINEL so callers can surface an "
                "actionable error instead of passing the stale ID to "
                "further API calls.",
                database_id,
            )
            return _RESOLUTION_FAILED_SENTINEL

        # At least one probe failed for a reason OTHER than not-found (auth,
        # network, 5xx, or a database with no data_sources list). Do NOT
        # claim the resource is gone — fall back to the original ID so the
        # caller's actual API call surfaces the real error.
        logger.warning(
            "Notion: could not resolve data_source_id for %s — "
            "lookup failed but not (only) with 404 (db probe: %s; "
            "data_source probe: %s). Falling back to the original ID so "
            "the real error surfaces on the actual call.",
            database_id,
            err or "ok-but-no-data_sources",
            ds_err,
        )
        return database_id

    async def normalize_relation_properties(
        self,
        client: AutoRefreshClient,
        props: dict,
    ) -> str | None:
        """Rewrite relation property *schemas* to use ``data_source_id``.

        The 2025-09-03 API requires relation columns to reference the
        target database's **data source** (``relation.data_source_id``),
        but LLMs routinely emit the legacy shape
        ``{"relation": {"database_id": "..."}}`` — Notion then rejects the
        whole request with ``relation.data_source_id should be defined,
        instead was 'undefined'``. Resolve the target database's primary
        data source (reusing :meth:`resolve_data_source_id`) and rewrite
        the property in place. Properties that already carry a
        ``data_source_id`` are left untouched. Resolution is memoised
        per call, so several relation columns pointing at the same
        target database cost a single lookup.

        Only schema-shaped relations (``relation`` is a dict) are handled;
        page property *values* use ``{"relation": [{"id": ...}]}`` (a
        list) and are deliberately ignored.

        Returns an actionable error string when a relation property names
        no target at all (neither ``database_id`` nor ``data_source_id``)
        — Notion would reject it with the same opaque validation error,
        so fail fast with retry guidance instead. Also returns an
        actionable error when the target ``database_id`` is actually a
        Notion **page** (:meth:`resolve_data_source_id` returned
        :data:`_PAGE_NOT_DATABASE_SENTINEL`) — the sentinel must never be
        serialized into a request payload. Returns ``None`` when all
        relation properties are valid (possibly after rewriting).
        """
        resolved_cache: dict[str, str] = {}
        for name, prop in props.items():
            if not isinstance(prop, dict):
                continue
            relation = prop.get("relation")
            if not isinstance(relation, dict) or relation.get("data_source_id"):
                continue
            raw_db_id = relation.get("database_id")
            db_id = raw_db_id.strip() if isinstance(raw_db_id, str) else ""
            if not db_id:
                return (
                    f"Error: relation property '{name}' must reference a "
                    "target database — include "
                    '{"relation": {"database_id": "DATABASE_ID"}} '
                    '(or a "data_source_id" directly). Use '
                    "list_notion_databases or search_notion to find the ID."
                )
            # Memoise per call: several relation columns commonly point at
            # the same target database — resolve each distinct ID once.
            # ``db_id`` is stripped once above and used consistently for
            # both the cache key and the resolution call, so whitespace
            # in the incoming JSON can't split the cache or pollute the
            # resolved value. Normalise the key (dashless, lowercase)
            # since the same Notion UUID legitimately appears dashed and
            # dashless.
            cache_key = db_id.replace("-", "").lower()
            ds_id = resolved_cache.get(cache_key)
            if ds_id is None:
                ds_id = await self.resolve_data_source_id(client, db_id)
                resolved_cache[cache_key] = ds_id
            if ds_id == _PAGE_NOT_DATABASE_SENTINEL:
                # The relation target is a page, not a database — never let
                # the sentinel be serialized into the request payload (Notion
                # would reject it with an opaque invalid-UUID 400).
                return (
                    f"Error: relation property '{name}' targets '{db_id}', "
                    "which is a Notion page, not a database. Relations must "
                    "point at a database — use list_notion_databases or "
                    "search_notion to find the correct database ID."
                )
            if ds_id == _RESOLUTION_FAILED_SENTINEL:
                # The relation target ID is unavailable (deleted, moved, or not
                # shared with the integration) — never serialize the sentinel
                # into the request payload. The 404s behind the sentinel may be
                # cached/suppressed verdicts (see ``_resolution_failed_error``),
                # so the wording must not claim a live check.
                return (
                    f"Error: relation property '{name}' targets '{db_id}', "
                    "which is unavailable (deleted, moved, or not shared with "
                    "this integration — possibly a cached verdict from an "
                    "earlier failing call or run). Use list_notion_databases "
                    "or search_notion to find the correct database ID — "
                    "search is never suppressed and returns current data."
                )
            relation.pop("database_id", None)
            relation["data_source_id"] = ds_id
            logger.info(
                "Notion: rewrote relation property %r to data_source_id %s "
                "(from database_id %s)",
                name,
                ds_id,
                db_id,
            )
        return None
