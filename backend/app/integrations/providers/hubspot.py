# SPDX-License-Identifier: Apache-2.0

"""HubSpot integration — CRM, Contacts, Companies, Deals, Tickets, Campaigns, Files,
and the activity layer: engagements, record timelines, forms, sequences, teams and
batch reads (``hubspot_activity``).

API docs: https://developers.hubspot.com/docs/reference/api
Auth: Bearer token (Private App access token)
Base URL: https://api.hubapi.com
"""

import contextlib
import functools
import json as _json
import logging
import re
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.file_delivery import (
    deliver_file,
    fetch_file,
    file_content_type,
    format_kb,
    format_mb,
    link_lines,
    read_pipe_file,
    uploaded_file_answer,
)
from app.integrations.http import (
    TRANSIENT_RETRY_STATUSES,
    app_token_cache_key,
    format_entity_list,
    format_pagination_hint,
    get_shared_rate_limiter,
    required_scopes,
    safe_request,
)
from app.integrations.providers.hubspot_activity import (
    TOOL_SCOPES,
    HubSpotActivity,
    activity_definitions,
    scopes_sentence,
    search_shown_note,
    search_total,
)
from app.integrations.providers.hubspot_line_items import HubSpotLineItems, line_item_definitions
from app.integrations.providers.hubspot_notes import HubSpotNotes, note_definitions
from app.integrations.providers.hubspot_records import (
    ASSOCIATION_CATEGORIES,
    ENGAGEMENT_SPECS,
    association_label,
    association_type_text,
    epoch_ms,
    format_engagement,
    normalize_engagement,
    parse_hubspot_time,
    record_properties,
    split_names,
)
from app.integrations.providers.hubspot_records import (
    engagement_type as as_engagement_type,
)
from app.integrations.providers.hubspot_scope import api_object_type, apply_object_type_scope
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

_BASE_URL = "https://api.hubapi.com"

# ── Files (Files API v3) and record attachments ─────────────────────────────
#: Largest file downloaded or uploaded.
_MAX_FILE_BYTES = 100 * 1024 * 1024
#: Where an uploaded file lands unless the agent names a folder.
_DEFAULT_UPLOAD_FOLDER = "/agent-uploads"
_FILE_ACCESS = ("PRIVATE", "PUBLIC_NOT_INDEXABLE", "PUBLIC_INDEXABLE")
#: The engagements whose ``hs_attachment_ids`` are a record's files, and the
#: property that names each one.
_ENGAGEMENT_TITLE_PROPERTIES = {
    "notes": "hs_note_body",
    "emails": "hs_email_subject",
    "calls": "hs_call_title",
    "meetings": "hs_meeting_title",
    "tasks": "hs_task_subject",
}
_ENGAGEMENT_TYPES = tuple(_ENGAGEMENT_TITLE_PROPERTIES)
_MAX_ENGAGEMENTS_PER_TYPE = 200
#: Attachments of one listing described with their metadata (one call each).
_MAX_ATTACHMENTS_DESCRIBED = 25
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _file_name(meta: dict) -> str:
    """A file's name with its extension (the Files API keeps them apart)."""
    name = str(meta.get("name") or f"hubspot-file-{meta.get('id', '')}").strip()
    extension = str(meta.get("extension") or "").strip().lstrip(".")
    if extension and not name.lower().endswith(f".{extension.lower()}"):
        name = f"{name}.{extension}"
    return name


def _format_file(meta: dict) -> str:
    parts = [f"{_file_name(meta)} (file id: {meta.get('id', '?')})"]
    if isinstance(meta.get("size"), int):
        parts.append(format_kb(meta["size"]))
    for key in ("type", "access"):
        if meta.get(key):
            parts.append(str(meta[key]))
    line = " | ".join(parts)
    url = meta.get("url") or meta.get("defaultHostingUrl")
    if url:
        line += f" | URL: {url}"
    return line


def _attachment_ids(engagement: dict) -> list[str]:
    """The file ids of an engagement's ``hs_attachment_ids`` (``;``-separated)."""
    raw = str((engagement.get("properties") or {}).get("hs_attachment_ids") or "")
    return [part.strip() for part in raw.split(";") if part.strip()]


def _engagement_label(engagement_type: str, engagement: dict) -> str:
    props = engagement.get("properties") or {}
    title = _HTML_TAG_RE.sub(" ", str(props.get(_ENGAGEMENT_TITLE_PROPERTIES[engagement_type]) or ""))
    title = " ".join(title.split())
    label = f"{engagement_type[:-1]} {engagement.get('id', '?')}"
    if title:
        label += f" '{title[:80]}{'...' if len(title) > 80 else ''}'"
    if props.get("hs_timestamp"):
        label += f" ({props['hs_timestamp']})"
    return label

# Default properties to request for each CRM object type
_CONTACT_PROPS = (
    "firstname,lastname,email,phone,company,jobtitle,lifecyclestage,hs_lead_status,hubspot_owner_id,country,"
    "notes_last_contacted,notes_last_updated,lastmodifieddate,createdate"
)
#: What a single contact read adds: ownership, location, conversions and sales activity.
_CONTACT_DETAIL_PROPS = _CONTACT_PROPS + (
    ",hubspot_owner_assigneddate,state,city,num_contacted_notes,first_conversion_event_name,first_conversion_date,"
    "recent_conversion_event_name,recent_conversion_date,num_conversion_events,hs_analytics_source,"
    "hs_sequences_is_enrolled,hs_latest_sequence_enrolled_date,hs_sales_email_last_opened,"
    "hs_sales_email_last_replied,engagements_last_meeting_booked,num_associated_deals"
)
_COMPANY_PROPS = (
    "name,domain,industry,phone,city,state,country,numberofemployees,annualrevenue,lifecyclestage,"
    "hubspot_owner_id,notes_last_contacted,createdate"
)
_DEAL_PROPS = "dealname,amount,dealstage,pipeline,closedate,hs_lastmodifieddate,createdate,hubspot_owner_id"
# hs_object_id is always returned by HubSpot regardless of requested properties
_DEAL_DEFAULT_KEYS = set(_DEAL_PROPS.split(",")) | {"hs_object_id"}
_TICKET_PROPS = (
    "subject,content,hs_pipeline,hs_pipeline_stage,hs_ticket_priority,hubspot_owner_id,createdate,hs_lastmodifieddate"
)
# hs_object_id is always returned by HubSpot regardless of requested properties
_TICKET_DEFAULT_KEYS = set(_TICKET_PROPS.split(",")) | {"hs_object_id"}
#: The properties of each record type shown with their labels (stage, status, owner...).
_LABELLED = {
    "contacts": ("lifecyclestage", "hs_lead_status", "hubspot_owner_id"),
    "companies": ("lifecyclestage", "industry", "hubspot_owner_id"),
    "deals": ("dealstage", "pipeline", "hubspot_owner_id"),
    "tickets": ("hs_pipeline", "hs_pipeline_stage", "hs_ticket_priority", "hubspot_owner_id"),
}
logger = logging.getLogger(__name__)

#: The arguments naming an association type (create_hubspot_association, delete_hubspot_association).
_ASSOCIATION_TYPE_ARGUMENTS: dict[str, Any] = {
    "label": {
        "type": "string",
        "description": "Association label name, as get_hubspot_associations shows it (e.g. 'Primary')",
        "default": "",
    },
    "association_category": {
        "type": "string",
        "description": "With association_type_id: HUBSPOT_DEFINED (default), USER_DEFINED or INTEGRATOR_DEFINED",
        "default": "",
    },
    "association_type_id": {"type": "string", "description": "Association type ID, instead of label", "default": ""},
}

#: The HubSpot tool being run, so a 403 can name the tool that needs the scope.
_CURRENT_TOOL: ContextVar[str] = ContextVar("hubspot_current_tool", default="")


def _named(tool: str, handler: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(handler)
    async def run(*args: Any, **kwargs: Any) -> Any:
        token = _CURRENT_TOOL.set(tool)
        try:
            return await handler(*args, **kwargs)
        finally:
            _CURRENT_TOOL.reset(token)

    return run


def _search_result(text: str, records: list[dict[str, Any]]) -> ToolResult:
    """A search answer: *text* for the model, the records listed as its structured value.

    An assignment's gate check (``non_empty`` / ``empty`` / a path condition)
    tests the structured value, never the text: "Found 0 tickets" is a
    non-empty string, and a text-only answer left the check unevaluated, so the
    run went ahead on an empty pipeline. Nothing found is ``[]``.
    """
    structured = [
        {"id": r.get("id"), "properties": dict(r.get("properties") or {})} for r in records if isinstance(r, dict)
    ]
    return ToolResult(tool_call_id="", content=text, structured=structured)


def _is_read(method: str, url: str) -> bool:
    """A request that reads (safe to retry on a transient gateway error)."""
    return method.lower() == "get" or url.endswith(("/search", "/batch/read"))


def _scope_error(resp: httpx.Response, action: str, err: str) -> str:
    """A 403 that names the missing scope(s) and the tool that needs them."""
    tool = _CURRENT_TOOL.get()
    scopes = required_scopes(resp)
    if scopes:
        return (
            f"Error: HubSpot access denied while trying to {action} - the private app token is missing the "
            f"scope(s) {', '.join(scopes)}, which {tool or 'this tool'} needs. A HubSpot Super Admin adds them "
            "under Settings > Integrations > Private Apps > (this app) > Scopes; retrying before that cannot succeed."
        )
    declared = TOOL_SCOPES.get(tool)
    if declared:
        return f"{err} ({tool} uses the private app scopes {', '.join(dict.fromkeys(declared))}.)"
    return err

# ── Rate limiting (see build_tools) ─────────────────────────────────────────
# HubSpot's CRM Search API is capped at 5 requests/second per ACCOUNT — shared
# across every user and integration in the portal — which is far stricter than
# the general burst limit (100-200 requests / 10 s) and the source of the 429
# When an agent fires several parallel deal/contact/etc.
# search tool calls they all breach the cap in the same instant and each
# retries. We pace ALL HubSpot requests for an account through one shared token
# bucket kept a safe step under the search cap so the burst is smoothed instead
# of fired simultaneously. With a token bucket the worst-case requests in any
# 1 s window is ``capacity + rate``, so capacity is kept at 1 (no meaningful
# instantaneous burst) and the sustained rate at 4/s (20 % headroom under 5/s).
# Pacing non-search calls too keeps the limiter simple and also respects the
# looser burst limit; the cost is negligible for agentic workloads (sequential
# calls are network-latency-bound and rarely reach the cap on their own).
_HUBSPOT_MAX_RPS = 4.0      # sustained requests/second (under the 5/s search cap)
_HUBSPOT_BURST = 1.0        # token bucket capacity (max instantaneous burst)
_HUBSPOT_CONCURRENCY = 4    # max in-flight requests per account


class HubSpotIntegration(BaseIntegration):
    provider_name = "hubspot"
    display_name = "HubSpot"
    icon = "Users"
    description = (
        "Connect HubSpot for CRM — contacts, companies, deals, and marketing campaigns."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        async with egress_client(timeout=30) as client:
            resp = await client.get(
                f"{_BASE_URL}/crm/v3/objects/contacts",
                headers={"Authorization": f"Bearer {token}"},
                params={"limit": "1"},
            )
            return resp.status_code == 200

    async def get_user_info(self, token: str) -> dict:
        async with egress_client(timeout=30) as client:
            resp = await client.get(
                f"{_BASE_URL}/account-info/v3/details",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "portal_id": data.get("portalId"),
                    "account_type": data.get("accountType"),
                    "time_zone": data.get("timeZone"),
                }
            # Fallback: just confirm we can access the API
            return {"status": "connected"}


# HubSpot CRM search filter operator → value-shape contract.
# https://developers.hubspot.com/docs/reference/api/crm/search
_HUBSPOT_ARRAY_OPERATORS = {"IN", "NOT_IN"}  # take ``values`` (array of strings)
_HUBSPOT_NO_VALUE_OPERATORS = {"HAS_PROPERTY", "NOT_HAS_PROPERTY"}  # take no value
# Negated scalar operators: a multi-element value means "none of these", so it
# upgrades to NOT_IN — using IN would flip the filter's polarity from "exclude"
# to "include" (e.g. NEQ ["a","b"] means "neither a nor b", i.e. NOT_IN).
_HUBSPOT_NEGATED_SCALAR_OPERATORS = {"NEQ", "NOT_CONTAINS_TOKEN"}
# Range operators: on a date or datetime property HubSpot reads epoch
# milliseconds; a custom date property refuses an ISO date with a bare 400.
_HUBSPOT_RANGE_OPERATORS = {"GT", "GTE", "LT", "LTE", "BETWEEN"}
_ISO_DATE_VALUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$")


def _range_bound(value: str) -> str:
    """An ISO date or time bounding a range, as the epoch milliseconds HubSpot reads.

    ``2026-11-18`` is midnight UTC (HubSpot stores a date property there),
    a time with no offset is UTC. Any other value is kept as written.
    """
    text = value.strip()
    if not _ISO_DATE_VALUE_RE.match(text):
        return value
    moment = parse_hubspot_time(text.replace(" ", "T", 1))
    return str(epoch_ms(moment)) if moment is not None else value


def _coerce_scalar_str(value: Any) -> str | None:
    """Coerce a tool-supplied value into a HubSpot scalar string slot.

    HubSpot's string slots (``value``, ``highValue``, ``query``, ``after``,
    sort fields) reject JSON arrays/objects — passing one is the root cause
    of the ``Cannot deserialize ... String from Array value`` 400.
    The LLM also frequently wraps a single value in a one-element array;
    unwrap it. Numbers/bools are stringified (HubSpot property values are
    strings). Returns ``None`` when there is no usable scalar (multi-element
    array, dict, or empty) so the caller can decide what to do.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return _coerce_scalar_str(value[0]) if len(value) == 1 else None
    return None


def _coerce_json_arg(value: Any) -> Any:
    """Accept either a JSON string or an already-parsed object.

    Tool args are declared as JSON strings, but some runtimes hand the
    already-parsed list/dict straight through. ``json.loads`` on a parsed
    object raises ``TypeError`` (not a ``ValueError``), so be explicit.
    Returns the parsed object, or ``None`` for empty/invalid input.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return _json.loads(value)
        except ValueError:
            return None
    return None


def _normalize_filter(flt: Any) -> dict | None:
    """Normalize one HubSpot search filter to its operator's value contract.

    Repairs the common tool/LLM mistakes that make HubSpot reject the
    payload with HTTP 400:

      * scalar operator (EQ/NEQ/GT/...) given an array ``value`` — unwrap a
        single-element array to a scalar; **upgrade a multi-element array to
        ``IN`` with ``values``** (the only schema-valid reading of "match
        any of these", and lossless unlike taking just the first element);
      * ``IN``/``NOT_IN`` given the list under ``value`` instead of
        ``values`` — move it across and drop ``value``;
      * ``HAS_PROPERTY``/``NOT_HAS_PROPERTY`` carrying a stray value — drop it;
      * an ISO date or time bounding a range (``GT``/``GTE``/``LT``/``LTE``/
        ``BETWEEN``) — send its epoch milliseconds, which date and datetime
        properties both read;
      * non-string scalars — stringify.

    Returns ``None`` for a filter that can't be made schema-valid so the
    caller drops it instead of forwarding a payload HubSpot still 400s on:
    a missing ``propertyName``; ``IN``/``NOT_IN`` whose ``values`` normalize
    to empty; ``BETWEEN`` missing either endpoint; or a scalar operator whose
    ``value`` can't be coerced. Dropping degrades to a broader (successful)
    search rather than a hard error.
    """
    if not isinstance(flt, dict):
        return None
    prop = flt.get("propertyName")
    if not isinstance(prop, str) or not prop.strip():
        return None
    raw_op = flt.get("operator")
    operator = raw_op.upper() if isinstance(raw_op, str) and raw_op.strip() else "EQ"

    out: dict[str, Any] = {"propertyName": prop, "operator": operator}

    if operator in _HUBSPOT_NO_VALUE_OPERATORS:
        return out

    if operator in _HUBSPOT_ARRAY_OPERATORS:
        raw = flt.get("values", flt.get("value"))
        items = raw if isinstance(raw, list) else ([raw] if raw is not None else [])
        values = [s for s in (_coerce_scalar_str(v) for v in items) if s is not None]
        if not values:
            # IN/NOT_IN require at least one value; an empty list still 400s.
            return None
        out["values"] = values
        return out

    if operator == "BETWEEN":
        low = _coerce_scalar_str(flt.get("value"))
        high = _coerce_scalar_str(flt.get("highValue"))
        if low is None or high is None:
            # BETWEEN needs both endpoints; a half-open range still 400s.
            return None
        out["value"] = _range_bound(low)
        out["highValue"] = _range_bound(high)
        return out

    # Scalar operators (EQ, NEQ, GT, GTE, LT, LTE, CONTAINS_TOKEN, ...).
    raw = flt.get("value")
    if isinstance(raw, list) and len(raw) > 1:
        # A scalar operator with multiple values is invalid for HubSpot; the
        # schema-valid reading is "match any of these" → IN, or "none of
        # these" → NOT_IN for negated operators (e.g. NEQ). Picking NOT_IN for
        # the negated case preserves the filter's polarity instead of flipping
        # "not equal" into "equal any of".
        values = [s for s in (_coerce_scalar_str(v) for v in raw) if s is not None]
        if not values:
            return None
        out["operator"] = (
            "NOT_IN" if operator in _HUBSPOT_NEGATED_SCALAR_OPERATORS else "IN"
        )
        out["values"] = values
        return out
    scalar = _coerce_scalar_str(raw)
    if scalar is None:
        # A scalar operator requires a value; drop it when none can be coerced.
        return None
    out["value"] = _range_bound(scalar) if operator in _HUBSPOT_RANGE_OPERATORS else scalar
    return out


def _normalize_filter_groups(parsed: Any) -> list[dict]:
    """Build a normalized HubSpot ``filterGroups`` list from parsed filters.

    Accepts the three shapes the tools document — a ``filterGroups`` list, a
    single filterGroup object, or a flat list of filters — and normalizes
    every leaf filter via :func:`_normalize_filter`. Empty groups are dropped.
    """

    def _norm_group(group: dict) -> dict | None:
        raw = group.get("filters")
        if not isinstance(raw, list):
            return None
        filters = [f for f in (_normalize_filter(x) for x in raw) if f is not None]
        return {"filters": filters} if filters else None

    groups: list[dict] = []
    if (
        isinstance(parsed, list)
        and parsed
        and all(isinstance(g, dict) and "filters" in g for g in parsed)
    ):
        # Already fully-qualified filterGroups list.
        for g in parsed:
            ng = _norm_group(g)
            if ng:
                groups.append(ng)
    elif isinstance(parsed, dict) and "filters" in parsed:
        # Single filterGroup object.
        ng = _norm_group(parsed)
        if ng:
            groups.append(ng)
    elif isinstance(parsed, list):
        # Flat list of filter objects — wrap in one filterGroup.
        filters = [f for f in (_normalize_filter(x) for x in parsed) if f is not None]
        if filters:
            groups.append({"filters": filters})
    return groups


def _build_search_body(
    *,
    properties: list[str],
    query: str = "",
    filters_json: str = "",
    sorts_json: str = "",
    sort_by: str = "",
    sort_direction: str = "DESCENDING",
    count: int = 10,
    after: str = "",
) -> dict:
    """Build a HubSpot CRM search request body.

    Supports free-text ``query``, structured ``filterGroups`` (via
    *filters_json*), sorting (via *sorts_json* or *sort_by*/*sort_direction*),
    and pagination — matching the full
    capabilities of ``POST /crm/v3/objects/{type}/search``.

    *filters_json* format (JSON string)::

        [{"propertyName": "createdate", "operator": "GTE", "value": "2025-01-01"}]

    Supported operators: EQ, NEQ, GT, GTE, LT, LTE, BETWEEN, IN,
    NOT_IN, HAS_PROPERTY, NOT_HAS_PROPERTY, CONTAINS_TOKEN,
    NOT_CONTAINS_TOKEN.
    """
    try:
        limit = max(1, min(int(count), 200))
    except (TypeError, ValueError):
        limit = 10
    body: dict[str, Any] = {
        "properties": properties,
        "limit": limit,
    }
    query_str = _coerce_scalar_str(query)
    if query_str:
        body["query"] = query_str
    if filters_json:
        # Normalize every filter to the type contract of its operator so a
        # tool/LLM-supplied array in a string slot (the HTTP 400) is
        # repaired instead of being forwarded to HubSpot verbatim. Accepts
        # either a JSON string or an already-parsed object.
        parsed_filters = _coerce_json_arg(filters_json)
        if parsed_filters is not None:
            filter_groups = _normalize_filter_groups(parsed_filters)
            if filter_groups:
                body["filterGroups"] = filter_groups
    if sorts_json:
        try:
            sorts = _json.loads(sorts_json)
            if isinstance(sorts, list):
                valid_sorts: list[dict[str, str]] = []
                for item in sorts:
                    if not isinstance(item, dict):
                        logger.warning("hubspot sorts_json: skipping non-dict sort item: %r", item)
                        continue
                    property_name = item.get("propertyName")
                    direction = item.get("direction")
                    normalized_direction = (
                        direction.upper() if isinstance(direction, str) else ""
                    )
                    has_valid_property = (
                        isinstance(property_name, str) and bool(property_name.strip())
                    )
                    has_valid_direction = normalized_direction in (
                        "ASCENDING",
                        "DESCENDING",
                    )
                    if not (has_valid_property and has_valid_direction):
                        logger.warning(
                            "hubspot sorts_json: skipping invalid sort item: %r", item
                        )
                        continue
                    valid_sorts.append(
                        {
                            "propertyName": property_name,
                            "direction": normalized_direction,
                        }
                    )
                if valid_sorts:
                    body["sorts"] = valid_sorts
        except ValueError:
            pass  # Ignore invalid JSON — fall back to sort_by/sort_direction
    if "sorts" not in body:
        sort_by_str = _coerce_scalar_str(sort_by)
        if sort_by_str:
            direction_str = _coerce_scalar_str(sort_direction) or "DESCENDING"
            body["sorts"] = [
                {"propertyName": sort_by_str, "direction": direction_str.upper()}
            ]
    after_str = _coerce_scalar_str(after)
    if after_str:
        body["after"] = after_str
    return body


def _redact_search_body(body: dict) -> dict:
    """Return a copy of a CRM search body that is safe to log.

    The structural fields needed to root-cause a 400 (property names,
    operators, sorts, limit, pagination) are kept; the free-text ``query``
    and filter ``value``/``values``/``highValue`` fields are replaced with
    ``"<redacted>"`` because they can carry customer data / PII.
    """
    redacted = dict(body)
    if "query" in redacted:
        redacted["query"] = "<redacted>"
    groups = redacted.get("filterGroups")
    if isinstance(groups, list):
        redacted["filterGroups"] = [
            {
                "filters": [
                    {
                        k: "<redacted>" if k in ("value", "values", "highValue") else v
                        for k, v in f.items()
                    }
                    for f in g.get("filters", [])
                    if isinstance(f, dict)
                ]
            }
            for g in groups
            if isinstance(g, dict)
        ]
    return redacted


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    # A multipart upload sets its own Content-Type (with the boundary).
    upload_headers = {"Authorization": f"Bearer {token}"}
    persist_file_fn = _kwargs.get("persist_file_fn")
    # The HubSpot object-type Scope(s) of the run is a hard boundary.
    object_type_scope = _kwargs.get("object_type_scope")

    # ── Rate limiting ─────────────────────────────────────────────────────
    # Funnel every HubSpot request for this account through one process-wide
    # shared token bucket (keyed by a secret-free hash of the token) so a burst
    # of parallel tool calls — across conversations, agents and assignments —
    # is paced below HubSpot's 5 req/s per-account search cap instead of firing
    # simultaneously and triggering a 429 retry storm. See the module
    # constants above for the rate/burst/concurrency rationale.
    _limiter = get_shared_rate_limiter(
        app_token_cache_key("hubspot", token),
        rate=_HUBSPOT_MAX_RPS,
        capacity=_HUBSPOT_BURST,
        max_concurrency=_HUBSPOT_CONCURRENCY,
    )

    async def _throttled(
        client: httpx.AsyncClient,
        method: str,
        url: str,
        provider: str,
        action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        """Rate-limited wrapper around :func:`safe_request`.

        Acquires a slot from the shared per-account limiter (pacing the request
        rate and capping concurrency) before delegating to ``safe_request``,
        which keeps its own 429 decorrelated-jitter retry. Holding the slot for
        the whole call also applies natural back-pressure while a 429 is being
        retried, so retries don't pile on top of new requests. A read is also
        retried on a transient gateway error, and a 403 names the scope(s) the
        private app misses and the tool that needs them. The failed response is
        handed back only to a caller that passes ``expected_statuses``.
        """
        branches = "expected_statuses" in kwargs
        kwargs.setdefault("expected_statuses", set())
        if _is_read(method, url):
            kwargs.setdefault("retry_statuses", TRANSIENT_RETRY_STATUSES)
        async with _limiter.slot():
            resp, err = await safe_request(client, method, url, provider, action, **kwargs)
        if err and resp is not None and resp.status_code == 403:
            err = _scope_error(resp, action, err)
        if err and not branches:
            return None, err
        return resp, err

    def _register(name: str, handler: Callable[..., Awaitable[Any]]) -> None:
        executor.register(name, _named(name, handler))

    activity = HubSpotActivity(
        _throttled,
        headers=headers,
        search_body=_build_search_body,
        default_properties={
            "contacts": _CONTACT_PROPS,
            "companies": _COMPANY_PROPS,
            "deals": _DEAL_PROPS,
            "tickets": _TICKET_PROPS,
        },
        object_type_scope=object_type_scope,
    )

    async def _labelled(
        client: httpx.AsyncClient, object_type: str, records: list[dict], extra: list[str], raw_values: bool
    ) -> None:
        if not raw_values:
            await activity.prepare_labels(
                client, object_type, [record_properties(r) for r in records], [*_LABELLED.get(object_type, ()), *extra]
            )

    def _shown(object_type: str, props: dict, name: str, raw_values: bool) -> str:
        return activity.shown(object_type, name, props.get(name, ""), raw_values=raw_values)

    async def _associations_text(client: httpx.AsyncClient, record: dict) -> str:
        """A record read's associations: engagements read and shown, records by id."""
        lines: list[str] = []
        for obj_type, data in (record.get("associations") or {}).items():
            ids = [str(r.get("id", "")) for r in (data or {}).get("results", []) if r.get("id")]
            if not ids:
                continue
            type_ = as_engagement_type(obj_type)
            if type_:
                lines.append(f"  {obj_type} ({len(ids)}):")
                lines.extend(await activity.describe_engagements(client, type_, ids))
            else:
                lines.append(f"  {obj_type}: {', '.join(ids)}")
        return "\n--- Associations ---\n" + "\n".join(lines) if lines else ""

    def _owner_text(props: dict, raw_values: bool) -> str:
        owner_id = props.get("hubspot_owner_id", "")
        if not owner_id:
            return "none"
        if raw_values:
            return owner_id
        text = activity.owner_lines(owner_id)
        assigned = props.get("hubspot_owner_assigneddate")
        return f"{text} | assigned {assigned}" if assigned else text

    # ── Contacts ─────────────────────────────────────────────────────────

    async def get_hubspot_contact(
        contact_id: str, associations: str = "", properties: str = "", raw_values: bool = False
    ) -> str:
        """Get detailed information about a specific HubSpot contact, optionally including associations."""
        extra = split_names(properties)
        names = list(dict.fromkeys([*_CONTACT_DETAIL_PROPS.split(","), *extra]))
        params: dict[str, str] = {"properties": ",".join(names)}
        if associations:
            params["associations"] = associations
        async with egress_client(timeout=30) as client:
            resp, err = await _throttled(
                client,
                "get",
                f"{_BASE_URL}/crm/v3/objects/contacts/{contact_id}",
                "HubSpot",
                "get contact",
                headers=headers,
                params=params,
            )
            if err:
                return err
            c = resp.json()
            p = record_properties(c)
            await _labelled(client, "contacts", [c], extra, raw_values)
            assoc_text = await _associations_text(client, c)

        def show(name: str) -> str:
            return _shown("contacts", p, name, raw_values) or "N/A"

        lines = [
            f"Contact ID: {c['id']}",
            f"Name: {p.get('firstname', '')} {p.get('lastname', '')}",
            f"Email: {show('email')}",
            f"Phone: {show('phone')}",
            f"Company: {show('company')}",
            f"Job Title: {show('jobtitle')}",
            f"Lifecycle Stage: {show('lifecyclestage')}",
            f"Lead Status: {show('hs_lead_status')}",
            f"Owner: {_owner_text(p, raw_values)}",
            f"Location: {', '.join(p[k] for k in ('city', 'state', 'country') if p.get(k)) or 'N/A'}",
            f"Created: {show('createdate')}",
            f"Last Modified: {show('lastmodifieddate')}",
        ]
        for name, label in (
            ("first_conversion_event_name", "First conversion"),
            ("recent_conversion_event_name", "Recent conversion"),
        ):
            if p.get(name):
                date_name = name.replace("_event_name", "_date")
                lines.append(f"{label}: {p[name]} at {p.get(date_name, 'unknown time')}")
        for name, label in (
            ("num_conversion_events", "Form conversions"),
            ("hs_analytics_source", "Original source"),
            ("notes_last_contacted", "Last contacted"),
            ("notes_last_updated", "Last activity"),
            ("num_contacted_notes", "Sales activities"),
            ("engagements_last_meeting_booked", "Last meeting booked"),
            ("hs_sales_email_last_opened", "Last sales email opened"),
            ("hs_sales_email_last_replied", "Last sales email reply"),
            ("hs_sequences_is_enrolled", "Enrolled in a sequence"),
            ("hs_latest_sequence_enrolled_date", "Latest sequence enrollment"),
            ("num_associated_deals", "Deals"),
        ):
            if p.get(name):
                lines.append(f"{label}: {p[name]}")
        lines.extend(f"{name}: {show(name)}" for name in extra if p.get(name) and name not in _CONTACT_DETAIL_PROPS.split(","))
        note = "" if raw_values else activity.labels_note("contacts")
        return "\n".join(lines) + assoc_text + (f"\n{note}" if note else "")

    def _contact_line(c: dict, extra: list[str], raw_values: bool) -> str:
        p = record_properties(c)
        name = f"{p.get('firstname', '')} {p.get('lastname', '')}".strip() or "Unnamed"
        parts = [name, f"Email: {p.get('email', 'N/A')}", f"Company: {p.get('company', 'N/A')}"]
        for prop, label in (
            ("jobtitle", "Title"),
            ("lifecyclestage", "Lifecycle"),
            ("hs_lead_status", "Lead status"),
            ("country", "Country"),
        ):
            if p.get(prop):
                parts.append(f"{label}: {_shown('contacts', p, prop, raw_values)}")
        owner_id = p.get("hubspot_owner_id")
        parts.append(f"Owner: {_shown('contacts', p, 'hubspot_owner_id', raw_values) if owner_id else 'none'}")
        parts.append(f"Created: {(p.get('createdate') or 'N/A')[:10]}")
        for prop, label in (("notes_last_contacted", "Last contacted"), ("notes_last_updated", "Last activity")):
            if p.get(prop):
                parts.append(f"{label}: {p[prop][:10]}")
        parts.extend(f"{n}: {_shown('contacts', p, n, raw_values)}" for n in extra if p.get(n))
        parts.append(f"ID: {c['id']}")
        return " | ".join(parts)

    async def search_hubspot_contacts(
        query: str = "",
        filters_json: str = "",
        sort_by: str = "",
        sort_direction: str = "DESCENDING",
        count: int = 10,
        after: str = "",
        properties: str = "",
        raw_values: bool = False,
    ) -> str | ToolResult:
        """Search and filter contacts with optional sorting and structured filters."""
        extra = [n for n in split_names(properties) if n not in _CONTACT_PROPS.split(",")]
        body = _build_search_body(
            properties=list(dict.fromkeys([*_CONTACT_PROPS.split(","), *extra])),
            query=query,
            filters_json=filters_json,
            sort_by=sort_by,
            sort_direction=sort_direction,
            count=count,
            after=after,
        )
        async with egress_client(timeout=60) as client:
            results, total, paging, err = await activity.search_pages(
                client, "contacts", body, count, action="search contacts"
            )
            if err and not results:
                return err
            await _labelled(client, "contacts", results, extra, raw_values)
        text = format_entity_list(
            results, lambda c: _contact_line(c, extra, raw_values), noun="contact", total=search_total(results, total)
        )
        text += search_shown_note(results, total, paging) + format_pagination_hint(paging)
        if err:
            text += f"\nStopped early: {err.splitlines()[0]}"
        note = "" if raw_values else activity.labels_note("contacts")
        return _search_result(text + (f"\n{note}" if note and results else ""), results)

    async def create_hubspot_contact(
        email: str,
        firstname: str = "",
        lastname: str = "",
        phone: str = "",
        company: str = "",
        jobtitle: str = "",
        lifecyclestage: str = "",
    ) -> str:
        """Create a new contact in HubSpot."""
        props: dict[str, str] = {"email": email}
        if firstname:
            props["firstname"] = firstname
        if lastname:
            props["lastname"] = lastname
        if phone:
            props["phone"] = phone
        if company:
            props["company"] = company
        if jobtitle:
            props["jobtitle"] = jobtitle
        if lifecyclestage:
            props["lifecyclestage"] = lifecyclestage
        async with egress_client(timeout=30) as client:
            resp, err = await _throttled(
                client,
                "post",
                f"{_BASE_URL}/crm/v3/objects/contacts",
                "HubSpot",
                "create contact",
                headers=headers,
                json={"properties": props},
            )
            if err:
                return err
            c = resp.json()
            return f"Contact created successfully. ID: {c['id']}"

    async def update_hubspot_contact(contact_id: str, properties_json: str) -> str:
        """Update a contact's properties. Pass properties as a JSON object, e.g. {\"phone\": \"+1234567890\", \"jobtitle\": \"CTO\"}."""
        try:
            props = _json.loads(properties_json)
        except ValueError:
            return "Error: Invalid JSON for properties."
        async with egress_client(timeout=30) as client:
            _, err = await _throttled(
                client,
                "patch",
                f"{_BASE_URL}/crm/v3/objects/contacts/{contact_id}",
                "HubSpot",
                "update contact",
                headers=headers,
                json={"properties": props},
            )
            if err:
                return err
            return f"Contact {contact_id} updated successfully."

    # ── Companies ────────────────────────────────────────────────────────

    async def get_hubspot_company(
        company_id: str, associations: str = "", properties: str = "", raw_values: bool = False
    ) -> str:
        """Get detailed information about a specific HubSpot company, optionally including associations."""
        extra = split_names(properties)
        params: dict[str, str] = {"properties": ",".join(dict.fromkeys([*_COMPANY_PROPS.split(","), *extra]))}
        if associations:
            params["associations"] = associations
        async with egress_client(timeout=30) as client:
            resp, err = await _throttled(
                client,
                "get",
                f"{_BASE_URL}/crm/v3/objects/companies/{company_id}",
                "HubSpot",
                "get company",
                headers=headers,
                params=params,
            )
            if err:
                return err
            c = resp.json()
            p = record_properties(c)
            await _labelled(client, "companies", [c], extra, raw_values)
            assoc_text = await _associations_text(client, c)

        def show(name: str) -> str:
            return _shown("companies", p, name, raw_values) or "N/A"

        lines = [
            f"Company ID: {c['id']}",
            f"Name: {show('name')}",
            f"Domain: {show('domain')}",
            f"Industry: {show('industry')}",
            f"Phone: {show('phone')}",
            f"City: {show('city')}",
            f"State: {show('state')}",
            f"Country: {show('country')}",
            f"Employees: {show('numberofemployees')}",
            f"Annual Revenue: {show('annualrevenue')}",
            f"Lifecycle Stage: {show('lifecyclestage')}",
            f"Owner: {_owner_text(p, raw_values)}",
            f"Created: {show('createdate')}",
        ]
        if p.get("notes_last_contacted"):
            lines.append(f"Last contacted: {p['notes_last_contacted']}")
        lines.extend(f"{name}: {show(name)}" for name in extra if p.get(name) and name not in _COMPANY_PROPS.split(","))
        note = "" if raw_values else activity.labels_note("companies")
        return "\n".join(lines) + assoc_text + (f"\n{note}" if note else "")

    async def search_hubspot_companies(
        query: str = "",
        filters_json: str = "",
        sort_by: str = "",
        sort_direction: str = "DESCENDING",
        count: int = 10,
        after: str = "",
        properties: str = "",
        raw_values: bool = False,
    ) -> str | ToolResult:
        """Search and filter companies with optional sorting and structured filters."""
        extra = [n for n in split_names(properties) if n not in _COMPANY_PROPS.split(",")]
        body = _build_search_body(
            properties=list(dict.fromkeys([*_COMPANY_PROPS.split(","), *extra])),
            query=query,
            filters_json=filters_json,
            sort_by=sort_by,
            sort_direction=sort_direction,
            count=count,
            after=after,
        )
        async with egress_client(timeout=60) as client:
            results, total, paging, err = await activity.search_pages(
                client, "companies", body, count, action="search companies"
            )
            if err and not results:
                return err
            await _labelled(client, "companies", results, extra, raw_values)

        def _fmt(c: dict) -> str:
            p = record_properties(c)
            parts = [
                p.get("name", "Unnamed"),
                f"Domain: {p.get('domain', 'N/A')}",
                f"Industry: {_shown('companies', p, 'industry', raw_values) or 'N/A'}",
            ]
            for prop, label in (("country", "Country"), ("lifecyclestage", "Lifecycle")):
                if p.get(prop):
                    parts.append(f"{label}: {_shown('companies', p, prop, raw_values)}")
            if p.get("hubspot_owner_id"):
                parts.append(f"Owner: {_shown('companies', p, 'hubspot_owner_id', raw_values)}")
            parts.append(f"Created: {(p.get('createdate') or 'N/A')[:10]}")
            parts.extend(f"{n}: {_shown('companies', p, n, raw_values)}" for n in extra if p.get(n))
            parts.append(f"ID: {c['id']}")
            return " | ".join(parts)

        text = (
            format_entity_list(results, _fmt, noun="company", total=search_total(results, total))
            + search_shown_note(results, total, paging)
            + format_pagination_hint(paging)
        )
        if err:
            text += f"\nStopped early: {err.splitlines()[0]}"
        return _search_result(text, results)

    async def create_hubspot_company(
        name: str,
        domain: str = "",
        industry: str = "",
        phone: str = "",
        city: str = "",
        state: str = "",
        country: str = "",
    ) -> str:
        """Create a new company in HubSpot."""
        props: dict[str, str] = {"name": name}
        if domain:
            props["domain"] = domain
        if industry:
            props["industry"] = industry
        if phone:
            props["phone"] = phone
        if city:
            props["city"] = city
        if state:
            props["state"] = state
        if country:
            props["country"] = country
        async with egress_client(timeout=30) as client:
            resp, err = await _throttled(
                client,
                "post",
                f"{_BASE_URL}/crm/v3/objects/companies",
                "HubSpot",
                "create company",
                headers=headers,
                json={"properties": props},
            )
            if err:
                return err
            c = resp.json()
            return f"Company created successfully. ID: {c['id']}"

    async def update_hubspot_company(company_id: str, properties_json: str) -> str:
        """Update a company's properties. Pass properties as a JSON object, e.g. {\"industry\": \"Technology\", \"phone\": \"+1234567890\"}."""
        try:
            props = _json.loads(properties_json)
        except ValueError:
            return "Error: Invalid JSON for properties."
        async with egress_client(timeout=30) as client:
            _, err = await _throttled(
                client,
                "patch",
                f"{_BASE_URL}/crm/v3/objects/companies/{company_id}",
                "HubSpot",
                "update company",
                headers=headers,
                json={"properties": props},
            )
            if err:
                return err
            return f"Company {company_id} updated successfully."

    # ── Deals ────────────────────────────────────────────────────────────

    async def list_hubspot_deals(count: int = 20, after: str = "") -> str:
        """List deals from HubSpot CRM."""
        params: dict[str, str] = {
            "limit": str(min(count, 100)),
            "properties": _DEAL_PROPS,
        }
        if after:
            params["after"] = after
        async with egress_client(timeout=30) as client:
            resp, err = await _throttled(
                client,
                "get",
                f"{_BASE_URL}/crm/v3/objects/deals",
                "HubSpot",
                "list deals",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()
            results = data.get("results", [])
            await _labelled(client, "deals", results, [], False)

            def _fmt(d: dict) -> str:
                p = d.get("properties", {})
                amount = p.get("amount", "N/A")
                if amount and amount != "N/A":
                    with contextlib.suppress(ValueError, TypeError):
                        amount = f"${float(amount):,.0f}"
                return (
                    f"{p.get('dealname', 'Unnamed')} | Amount: {amount} "
                    f"| Stage: {_shown('deals', p, 'dealstage', False) or 'N/A'} "
                    f"| Pipeline: {_shown('deals', p, 'pipeline', False) or 'N/A'} "
                    f"| Close: {(p.get('closedate') or 'N/A')[:10]} | ID: {d['id']}"
                )

            text = format_entity_list(results, _fmt, noun="deal")
            paging = data.get("paging", {}).get("next", {}).get("after", "")
            return text + format_pagination_hint(paging)

    async def get_hubspot_deal(
        deal_id: str, associations: str = "", properties: str = "", raw_values: bool = False
    ) -> str:
        """Get detailed information about a specific HubSpot deal, optionally including associations."""
        requested_props = _DEAL_PROPS
        if properties.strip():
            extra_props = [prop.strip() for prop in properties.split(",") if prop.strip()]
            if extra_props:
                all_props = set(_DEAL_PROPS.split(",")) | set(extra_props)
                requested_props = ",".join(all_props)
        params: dict[str, str] = {"properties": requested_props}
        if associations:
            params["associations"] = associations
        async with egress_client(timeout=30) as client:
            resp, err = await _throttled(
                client,
                "get",
                f"{_BASE_URL}/crm/v3/objects/deals/{deal_id}",
                "HubSpot",
                "get deal",
                headers=headers,
                params=params,
            )
            if err:
                return err
            d = resp.json()
            p = d.get("properties", {})
            extra = [k for k in p if k not in _DEAL_DEFAULT_KEYS]
            await _labelled(client, "deals", [d], extra, raw_values)
            assoc_text = await _associations_text(client, d)
        amount = p.get("amount", "N/A")
        if amount and amount != "N/A":
            with contextlib.suppress(ValueError, TypeError):
                amount = f"${float(amount):,.0f}"
        text = (
            f"Deal ID: {d['id']}\n"
            f"Name: {p.get('dealname', 'N/A')}\n"
            f"Amount: {amount}\n"
            f"Stage: {_shown('deals', p, 'dealstage', raw_values) or 'N/A'}\n"
            f"Pipeline: {_shown('deals', p, 'pipeline', raw_values) or 'N/A'}\n"
            f"Close Date: {(p.get('closedate') or 'N/A')[:10]}\n"
            f"Owner: {_owner_text(p, raw_values)}\n"
            f"Created: {p.get('createdate', 'N/A')}\n"
            f"Last Modified: {p.get('hs_lastmodifieddate', 'N/A')}"
        )
        # Include any extra properties beyond the default set
        for key, val in p.items():
            if key not in _DEAL_DEFAULT_KEYS and val is not None:
                text += f"\n{key}: {_shown('deals', p, key, raw_values) if val != '' else val}"
        return text + assoc_text

    async def search_hubspot_deals(
        query: str = "",
        filters_json: str = "",
        sorts_json: str = "",
        sort_by: str = "",
        sort_direction: str = "DESCENDING",
        count: int = 10,
        after: str = "",
        properties: str = "",
        raw_values: bool = False,
    ) -> str | ToolResult:
        """Search and filter deals with optional sorting and structured filters."""
        default_props = _DEAL_PROPS.split(",")
        extra = split_names(properties)
        props = list(set(default_props) | set(extra)) if extra else default_props
        body = _build_search_body(
            properties=props,
            query=query,
            filters_json=filters_json,
            sorts_json=sorts_json,
            sort_by=sort_by,
            sort_direction=sort_direction,
            count=count,
            after=after,
        )
        async with egress_client(timeout=60) as client:
            results, total, paging, err = await activity.search_pages(
                client, "deals", body, count, action="search deals"
            )
            if err and not results:
                return err
            await _labelled(client, "deals", results, [n for n in extra if n not in _DEAL_DEFAULT_KEYS], raw_values)

        def _fmt(d: dict) -> str:
            p = d.get("properties", {})
            amount = p.get("amount", "N/A")
            if amount and amount != "N/A":
                with contextlib.suppress(ValueError, TypeError):
                    amount = f"${float(amount):,.0f}"
            line = (
                f"{p.get('dealname', 'Unnamed')} | Amount: {amount} "
                f"| Stage: {_shown('deals', p, 'dealstage', raw_values) or 'N/A'} "
                f"| Pipeline: {_shown('deals', p, 'pipeline', raw_values) or 'N/A'} "
                f"| Close: {(p.get('closedate') or 'N/A')[:10]} "
                f"| Created: {(p.get('createdate') or 'N/A')[:10]} | ID: {d['id']}"
            )
            if p.get("hubspot_owner_id"):
                line += f" | Owner: {_shown('deals', p, 'hubspot_owner_id', raw_values)}"
            # Append any extra properties beyond the default set
            extras = {
                k: v
                for k, v in p.items()
                if k not in _DEAL_DEFAULT_KEYS and v is not None and v != ""
            }
            if extras:
                line += " | " + " | ".join(f"{k}: {_shown('deals', p, k, raw_values)}" for k in extras)
            return line

        text = (
            format_entity_list(results, _fmt, noun="deal", total=search_total(results, total))
            + search_shown_note(results, total, paging)
            + format_pagination_hint(paging)
        )
        if err:
            text += f"\nStopped early: {err.splitlines()[0]}"
        return _search_result(text, results)

    async def create_hubspot_deal(
        dealname: str,
        amount: str = "",
        dealstage: str = "",
        pipeline: str = "",
        closedate: str = "",
    ) -> str:
        """Create a new deal in HubSpot."""
        props: dict[str, str] = {"dealname": dealname}
        if amount:
            props["amount"] = amount
        if dealstage:
            props["dealstage"] = dealstage
        if pipeline:
            props["pipeline"] = pipeline
        if closedate:
            props["closedate"] = closedate
        async with egress_client(timeout=30) as client:
            resp, err = await _throttled(
                client,
                "post",
                f"{_BASE_URL}/crm/v3/objects/deals",
                "HubSpot",
                "create deal",
                headers=headers,
                json={"properties": props},
            )
            if err:
                return err
            d = resp.json()
            return f"Deal created successfully. ID: {d['id']}"

    async def update_hubspot_deal(deal_id: str, properties_json: str) -> str:
        """Update a deal's properties. Pass properties as a JSON object, e.g. {\"amount\": \"50000\", \"dealstage\": \"closedwon\"}."""
        try:
            props = _json.loads(properties_json)
        except ValueError:
            return "Error: Invalid JSON for properties."
        async with egress_client(timeout=30) as client:
            _, err = await _throttled(
                client,
                "patch",
                f"{_BASE_URL}/crm/v3/objects/deals/{deal_id}",
                "HubSpot",
                "update deal",
                headers=headers,
                json={"properties": props},
            )
            if err:
                return err
            return f"Deal {deal_id} updated successfully."

    # ── Marketing Campaigns ──────────────────────────────────────────────

    async def list_hubspot_campaigns(count: int = 20, after: str = "") -> str:
        """List marketing email campaigns from HubSpot."""
        params: dict[str, str] = {"limit": str(min(count, 100))}
        if after:
            params["after"] = after
        async with egress_client(timeout=30) as client:
            resp, err = await _throttled(
                client,
                "get",
                f"{_BASE_URL}/marketing/v3/emails",
                "HubSpot",
                "list campaigns",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()
            results = data.get("results", [])

            def _fmt(e: dict) -> str:
                return (
                    f"{e.get('name', 'Unnamed')} | Subject: {e.get('subject', 'N/A')} "
                    f"| Type: {e.get('type', 'N/A')} | Status: {e.get('state', 'N/A')} "
                    f"| ID: {e.get('id', '')}"
                )

            text = format_entity_list(
                results,
                _fmt,
                noun="campaign",
                empty_msg="No marketing campaigns found.",
            )
            paging = data.get("paging", {}).get("next", {}).get("after", "")
            return text + format_pagination_hint(paging)

    async def get_hubspot_campaign(campaign_id: str) -> str:
        """Get detailed information about a specific marketing email campaign."""
        async with egress_client(timeout=30) as client:
            resp, err = await _throttled(
                client,
                "get",
                f"{_BASE_URL}/marketing/v3/emails/{campaign_id}",
                "HubSpot",
                "get campaign",
                headers=headers,
            )
            if err:
                return err
            e = resp.json()
            stats = e.get("statistics", {})
            stats_str = ""
            if stats:
                stats_str = (
                    f"\n--- Statistics ---\n"
                    f"Sent: {stats.get('counters', {}).get('sent', 'N/A')}\n"
                    f"Opens: {stats.get('counters', {}).get('open', 'N/A')}\n"
                    f"Clicks: {stats.get('counters', {}).get('click', 'N/A')}\n"
                    f"Bounces: {stats.get('counters', {}).get('bounce', 'N/A')}\n"
                    f"Unsubscribes: {stats.get('counters', {}).get('unsubscribed', 'N/A')}\n"
                    f"Open Rate: {stats.get('ratios', {}).get('openratio', 'N/A')}\n"
                    f"Click Rate: {stats.get('ratios', {}).get('clickratio', 'N/A')}"
                )
            return (
                f"Campaign ID: {e.get('id', '')}\n"
                f"Name: {e.get('name', 'N/A')}\n"
                f"Subject: {e.get('subject', 'N/A')}\n"
                f"Type: {e.get('type', 'N/A')}\n"
                f"Status: {e.get('state', 'N/A')}\n"
                f"From: {e.get('from', 'N/A')}\n"
                f"Reply-To: {e.get('replyTo', 'N/A')}\n"
                f"Created: {e.get('createdAt', 'N/A')}\n"
                f"Updated: {e.get('updatedAt', 'N/A')}"
                f"{stats_str}"
            )

    # ── Pipelines (for deal stage reference) ─────────────────────────────

    async def list_hubspot_pipelines(object_type: str = "deals") -> str:
        """List pipelines and their stages for deals or tickets."""
        async with egress_client(timeout=30) as client:
            resp, err = await _throttled(
                client,
                "get",
                f"{_BASE_URL}/crm/v3/pipelines/{object_type}",
                "HubSpot",
                "list pipelines",
                headers=headers,
            )
            if err:
                return err
            results = resp.json().get("results", [])
            if not results:
                return f"No pipelines found for {object_type}."
            lines = []
            for pipeline in results:
                lines.append(
                    f"Pipeline: {pipeline.get('label', 'Unnamed')} (ID: {pipeline.get('id', '')})"
                )
                for stage in pipeline.get("stages", []):
                    lines.append(
                        f"  └─ {stage.get('label', '')} (ID: {stage.get('id', '')})"
                    )
            return "\n".join(lines)

    # ── Owners ───────────────────────────────────────────────────────────

    async def list_hubspot_owners(count: int = 50) -> str:
        """List HubSpot owners (users who can be assigned to contacts, companies, or deals)."""
        async with egress_client(timeout=30) as client:
            resp, err = await _throttled(
                client,
                "get",
                f"{_BASE_URL}/crm/v3/owners",
                "HubSpot",
                "list owners",
                headers=headers,
                params={"limit": str(min(count, 100))},
            )
            if err:
                return err
            results = resp.json().get("results", [])

            def _fmt(o: dict) -> str:
                return (
                    f"{o.get('firstName', '')} {o.get('lastName', '')} | Email: {o.get('email', 'N/A')} "
                    f"| ID: {o.get('id', '')}"
                )

            return format_entity_list(results, _fmt, noun="owner")

    async def search_hubspot_owners(query: str = "", count: int = 50) -> str:
        """Search HubSpot owners (team members) by name or email.

        HubSpot's owners API has **no** ``/search`` endpoint (unlike
        contacts/companies/deals), so this pages the owners list and filters
        client-side with a case-insensitive substring match on first/last name
        and email. A full email is additionally pushed to the owners API's
        exact ``email`` filter as a fast path. An empty ``query`` lists owners
        (bounded by ``count``).

        The page-size argument is named ``count`` (not ``limit``) to match every
        other HubSpot tool (``search_hubspot_contacts``, ``list_hubspot_owners``,
        …); the executor drops kwargs the handler doesn't declare, so a model
        that learned ``count`` from the sibling tools would otherwise have it
        silently ignored.
        """
        q = (query or "").strip()
        want = max(1, min(count, 100))
        ql = q.lower()

        def _fmt(o: dict) -> str:
            name = (
                f"{o.get('firstName', '')} {o.get('lastName', '')}".strip()
                or "Unnamed"
            )
            return (
                f"{name} | Email: {o.get('email', 'N/A')} | ID: {o.get('id', '')}"
            )

        def _matches(o: dict) -> bool:
            if not q:
                return True
            haystack = " ".join(
                str(o.get(k, "") or "") for k in ("firstName", "lastName", "email")
            ).lower()
            return ql in haystack

        empty_msg = (
            f"No HubSpot owners found matching '{q}'." if q else "No owners found."
        )

        async with egress_client(timeout=30) as client:
            # Fast path: a full email maps to the owners API's exact ``email``
            # filter, sidestepping the client-side scan and its page cap.
            if "@" in q:
                resp, err = await _throttled(
                    client,
                    "get",
                    f"{_BASE_URL}/crm/v3/owners",
                    "HubSpot",
                    f"search owners by email {q}",
                    headers=headers,
                    params={"email": q, "limit": "100"},
                )
                if err:
                    return err
                exact = resp.json().get("results", [])
                if exact:
                    return format_entity_list(exact[:want], _fmt, noun="owner")
                # No exact-email hit → fall through to a substring scan so a
                # partial email (e.g. "@acme.com") still matches.

            # General path: page the owners list (bounded) and filter
            # client-side. Owner lists are small (one per HubSpot seat), so a
            # modest page cap keeps cost bounded without missing realistic data.
            collected: list[dict] = []
            after = ""
            max_pages = 10
            for _ in range(max_pages):
                params = {"limit": "100"}
                if after:
                    params["after"] = after
                resp, err = await _throttled(
                    client,
                    "get",
                    f"{_BASE_URL}/crm/v3/owners",
                    "HubSpot",
                    "search owners",
                    headers=headers,
                    params=params,
                )
                if err:
                    return err
                data = resp.json()
                for owner in data.get("results", []):
                    if _matches(owner):
                        collected.append(owner)
                if len(collected) >= want:
                    break
                after = (data.get("paging", {}).get("next") or {}).get("after", "")
                if not after:
                    break

            return format_entity_list(
                collected[:want], _fmt, noun="owner", empty_msg=empty_msg
            )

    async def get_hubspot_owner(owner_id: str) -> str:
        """Get a single HubSpot owner (team member) by ID."""
        # HubSpot owner IDs are always numeric integers. A non-numeric value
        # (e.g. a CRM record ObjectId like "6968ac502e2dbdb5d627edb1") will
        # cause HubSpot to return HTTP 400 "Unable to parse value for path
        # parameter: ownerId". Catch this early and return a useful message so
        # the agent can self-correct by calling search_hubspot_owners instead.
        # ``str()`` tolerates runtimes that hand the ID through as an integer
        # (same convention as the already-parsed inputs elsewhere in this
        # file), rebinding keeps the URL below on the cleaned value, and the
        # ``isascii()`` check rejects non-ASCII digits (e.g. "١٢٣") that
        # ``isdigit()`` alone would accept but HubSpot still 400s on.
        owner_id = str(owner_id).strip()
        if not (owner_id.isascii() and owner_id.isdigit()):
            return (
                f"Error: '{owner_id}' is not a valid HubSpot owner ID. "
                "HubSpot owner IDs are numeric (e.g. 12345678). "
                "Use search_hubspot_owners or list_hubspot_owners to find the correct numeric owner ID."
            )
        async with egress_client(timeout=30) as client:
            resp, err = await _throttled(
                client,
                "get",
                f"{_BASE_URL}/crm/v3/owners/{owner_id}",
                "HubSpot",
                f"get owner {owner_id}",
                headers=headers,
            )
            if err:
                return err
            o = resp.json()
            name = (
                f"{o.get('firstName', '')} {o.get('lastName', '')}".strip() or "Unnamed"
            )
            lines = [
                f"Owner ID: {o.get('id', owner_id)}",
                f"Name: {name}",
                f"Email: {o.get('email', 'N/A')}",
            ]
            if o.get("userId") is not None:
                lines.append(f"User ID: {o.get('userId')}")
            teams = o.get("teams") or []
            if isinstance(teams, list) and teams:
                named = sorted(
                    (t for t in teams if isinstance(t, dict) and t.get("name")),
                    key=lambda t: not t.get("primary"),
                )
                team_names = ", ".join(
                    t["name"] + (f" (team id {t['id']}{', primary' if t.get('primary') else ''})" if t.get("id") else "")
                    for t in named
                )
                if team_names:
                    lines.append(f"Teams: {team_names}")
            if o.get("archived"):
                lines.append("Status: archived")
            created = (o.get("createdAt") or "")[:10]
            updated = (o.get("updatedAt") or "")[:10]
            if created:
                lines.append(f"Created: {created}")
            if updated:
                lines.append(f"Updated: {updated}")
            return "\n".join(lines)

    # ── Associations ─────────────────────────────────────────────────────

    async def get_hubspot_associations(
        object_type: str,
        object_id: str,
        to_object_type: str,
    ) -> str:
        """Get associations between a record and another object type."""
        target_engagement = as_engagement_type(to_object_type)
        target = target_engagement or to_object_type
        async with egress_client(timeout=30) as client:
            resp, err = await _throttled(
                client,
                "get",
                f"{_BASE_URL}/crm/v4/objects/{object_type}/{object_id}/associations/{target}",
                "HubSpot",
                f"get associations from {object_type} to {target}",
                headers=headers,
                params={"limit": "500"},
            )
            if err:
                return err
            data = resp.json()
            results = data.get("results", [])
            if not results:
                return f"No {target} associated with {object_type} {object_id}."
            if target_engagement:
                ids = [str(r.get("toObjectId")) for r in results if r.get("toObjectId") is not None]
                lines = [f"{len(ids)} {target} associated with {object_type} {object_id}, newest first:"]
                lines.extend(await activity.describe_engagements(client, target_engagement, ids))
                return "\n".join(lines)
            lines = [f"Associated {target} for {object_type} {object_id}:"]
            for r in results:
                assoc_id = r.get("toObjectId", r.get("id", ""))
                labels = [t["label"] for t in r.get("associationTypes", []) if isinstance(t, dict) and t.get("label")]
                label_str = f" ({', '.join(str(lb) for lb in labels)})" if labels else ""
                lines.append(f"  ID: {assoc_id}{label_str}")
            if (data.get("paging") or {}).get("next"):
                lines.append("  (more than 500: batch_get_hubspot_associations or the timeline reads the rest)")
            return "\n".join(lines)

    async def _association_type(
        client: httpx.AsyncClient,
        from_type: str,
        to_type: str,
        label: str,
        association_category: str,
        association_type_id: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """The association type a label name or a category and type id names (``None``: none named)."""
        label = str(label or "").strip()
        type_id = str(association_type_id or "").strip()
        if not label and not type_id:
            return None, None
        if type_id:
            if not type_id.isdigit():
                return None, "Error: association_type_id must be a number."
            category = str(association_category or "").strip().upper() or "HUBSPOT_DEFINED"
            if category not in ASSOCIATION_CATEGORIES:
                return None, f"Error: association_category must be one of {', '.join(ASSOCIATION_CATEGORIES)}."
            return {"associationCategory": category, "associationTypeId": int(type_id)}, None
        resp, err = await _throttled(
            client,
            "get",
            f"{_BASE_URL}/crm/v4/associations/{from_type}/{to_type}/labels",
            "HubSpot",
            f"list the association labels from {from_type} to {to_type}",
            headers=headers,
        )
        if err:
            return None, err
        labels = resp.json().get("results", [])
        found = association_label(labels, label)
        if found is None:
            named = sorted({str(t.get("label")) for t in labels if isinstance(t, dict) and t.get("label")})
            return None, (
                f"Error: no association label '{label}' from {from_type} to {to_type}. "
                f"Labels defined: {', '.join(named) or 'none'}."
            )
        return found, None

    async def create_hubspot_association(
        from_type: str,
        from_id: str,
        to_type: str,
        to_id: str,
        label: str = "",
        association_category: str = "",
        association_type_id: str = "",
    ) -> str:
        """Associate two HubSpot records, with the default type or a labelled one (e.g. a deal's Primary company)."""
        async with egress_client(timeout=30) as client:
            typed, err = await _association_type(
                client, from_type, to_type, label, association_category, association_type_id
            )
            if err:
                return err
            if typed is None:
                _, err = await _throttled(
                    client,
                    "put",
                    f"{_BASE_URL}/crm/v4/objects/{from_type}/{from_id}/associations/default/{to_type}/{to_id}",
                    "HubSpot",
                    f"associate {from_type} {from_id} → {to_type} {to_id}",
                    headers=headers,
                )
                if err:
                    return err
                return f"Successfully associated {from_type} {from_id} with {to_type} {to_id}."
            _, err = await _throttled(
                client,
                "put",
                f"{_BASE_URL}/crm/v4/objects/{from_type}/{from_id}/associations/{to_type}/{to_id}",
                "HubSpot",
                f"associate {from_type} {from_id} → {to_type} {to_id} with {association_type_text(typed)}",
                headers=headers,
                json=[{k: typed[k] for k in ("associationCategory", "associationTypeId")}],
            )
            if err:
                return err
            return (
                f"Successfully associated {from_type} {from_id} with {to_type} {to_id} as "
                f"{association_type_text(typed)}; its other association types are kept "
                "(get_hubspot_associations reads them, delete_hubspot_association removes one)."
            )

    async def delete_hubspot_association(
        from_type: str,
        from_id: str,
        to_type: str,
        to_id: str,
        label: str = "",
        association_category: str = "",
        association_type_id: str = "",
    ) -> str:
        """Remove one association type (a label) between two records, or the whole association."""
        async with egress_client(timeout=30) as client:
            typed, err = await _association_type(
                client, from_type, to_type, label, association_category, association_type_id
            )
            if err:
                return err
            if typed is None:
                _, err = await _throttled(
                    client,
                    "delete",
                    f"{_BASE_URL}/crm/v4/objects/{from_type}/{from_id}/associations/{to_type}/{to_id}",
                    "HubSpot",
                    f"remove every association of {from_type} {from_id} with {to_type} {to_id}",
                    headers=headers,
                )
                if err:
                    return err
                return f"Removed every association between {from_type} {from_id} and {to_type} {to_id}."
            _, err = await _throttled(
                client,
                "post",
                f"{_BASE_URL}/crm/v4/associations/{from_type}/{to_type}/batch/labels/archive",
                "HubSpot",
                f"remove {association_type_text(typed)} from {from_type} {from_id} → {to_type} {to_id}",
                headers=headers,
                json={
                    "inputs": [
                        {
                            "from": {"id": str(from_id)},
                            "to": {"id": str(to_id)},
                            "types": [{k: typed[k] for k in ("associationCategory", "associationTypeId")}],
                        }
                    ]
                },
            )
            if err:
                return err
            return (
                f"Removed {association_type_text(typed)} from {from_type} {from_id} → {to_type} {to_id}; "
                "the records stay associated by their other types."
            )

    # ── Delete / Archive ──────────────────────────────────────────────────

    async def delete_hubspot_record(object_type: str, record_id: str) -> str:
        """Archive (soft-delete) a HubSpot CRM record."""
        async with egress_client(timeout=30) as client:
            _, err = await _throttled(
                client,
                "delete",
                f"{_BASE_URL}/crm/v3/objects/{object_type}/{record_id}",
                "HubSpot",
                f"delete {object_type} {record_id}",
                headers=headers,
            )
            if err:
                return err
            return f"{object_type.capitalize()} {record_id} archived successfully."

    # ── Tickets ────────────────────────────────────────────────────────

    async def search_hubspot_tickets(
        query: str = "",
        filters_json: str = "",
        sort_by: str = "",
        sort_direction: str = "DESCENDING",
        count: int = 10,
        after: str = "",
        raw_values: bool = False,
        properties: str = "",
    ) -> str | ToolResult:
        """Search and filter support tickets with optional sorting and structured filters."""
        extra = [n for n in split_names(properties) if n not in _TICKET_DEFAULT_KEYS]
        body = _build_search_body(
            properties=list(dict.fromkeys([*_TICKET_PROPS.split(","), *extra])),
            query=query,
            filters_json=filters_json,
            sort_by=sort_by,
            sort_direction=sort_direction,
            count=count,
            after=after,
        )
        async with egress_client(timeout=60) as client:
            results, total, paging, err = await activity.search_pages(
                client, "tickets", body, count, action="search tickets"
            )
            if err and not results:
                # Surface the search payload HubSpot rejected so a recurring
                # 400 (e.g. a stale custom property in an
                # assignment's filter) can be root-caused straight from
                # production logs without enabling DEBUG logging. The logged
                # body is redacted: property names, operators and sorts are
                # kept, while the free-text query and filter values (which
                # can carry customer data / PII) are stripped.
                logger.warning(
                    "search_hubspot_tickets failed; request body: %s",
                    _json.dumps(_redact_search_body(body), default=str),
                )
                return err
            await _labelled(client, "tickets", results, extra, raw_values)

        def _fmt(t: dict) -> str:
            p = t.get("properties", {})
            line = (
                f"{p.get('subject', 'Untitled')} | Priority: {_shown('tickets', p, 'hs_ticket_priority', raw_values) or 'N/A'} "
                f"| Stage: {_shown('tickets', p, 'hs_pipeline_stage', raw_values) or 'N/A'} "
                f"| Created: {(p.get('createdate') or 'N/A')[:10]} | ID: {t['id']}"
            )
            if p.get("hubspot_owner_id"):
                line += f" | Owner: {_shown('tickets', p, 'hubspot_owner_id', raw_values)}"
            extras = [f"{k}: {_shown('tickets', p, k, raw_values)}" for k in extra if p.get(k) not in (None, "")]
            if extras:
                line += " | " + " | ".join(extras)
            return line

        text = (
            format_entity_list(results, _fmt, noun="ticket", total=search_total(results, total))
            + search_shown_note(results, total, paging)
            + format_pagination_hint(paging)
        )
        if err:
            text += f"\nStopped early: {err.splitlines()[0]}"
        return _search_result(text, results)

    async def get_hubspot_ticket(
        ticket_id: str, associations: str = "", raw_values: bool = False, properties: str = ""
    ) -> str:
        """Get detailed information about a specific HubSpot ticket, custom properties included."""
        requested = [n for n in split_names(properties) if n not in _TICKET_DEFAULT_KEYS]
        params: dict[str, str] = {"properties": ",".join(dict.fromkeys([*_TICKET_PROPS.split(","), *requested]))}
        if associations:
            params["associations"] = associations
        async with egress_client(timeout=30) as client:
            resp, err = await _throttled(
                client,
                "get",
                f"{_BASE_URL}/crm/v3/objects/tickets/{ticket_id}",
                "HubSpot",
                "get ticket",
                headers=headers,
                params=params,
            )
            if err:
                return err
            t = resp.json()
            p = t.get("properties", {})
            extra = [k for k in p if k not in _TICKET_DEFAULT_KEYS]
            await _labelled(client, "tickets", [t], extra, raw_values)
            assoc_text = await _associations_text(client, t)
        text = (
            f"Ticket ID: {t['id']}\n"
            f"Subject: {p.get('subject', 'N/A')}\n"
            f"Content: {(p.get('content') or 'N/A')[:500]}\n"
            f"Pipeline: {_shown('tickets', p, 'hs_pipeline', raw_values) or 'N/A'}\n"
            f"Stage: {_shown('tickets', p, 'hs_pipeline_stage', raw_values) or 'N/A'}\n"
            f"Priority: {_shown('tickets', p, 'hs_ticket_priority', raw_values) or 'N/A'}\n"
            f"Owner: {_owner_text(p, raw_values)}\n"
            f"Created: {p.get('createdate', 'N/A')}\n"
            f"Last Modified: {p.get('hs_lastmodifieddate', 'N/A')}"
        )
        for key in extra:
            if p.get(key) is not None:
                text += f"\n{key}: {_shown('tickets', p, key, raw_values) if p[key] != '' else ''}"
        return text + assoc_text

    async def create_hubspot_ticket(
        subject: str,
        content: str = "",
        hs_pipeline: str = "",
        hs_pipeline_stage: str = "",
        hs_ticket_priority: str = "",
        properties_json: str = "",
    ) -> str:
        """Create a new support ticket in HubSpot.

        ``properties_json`` (optional) is a JSON object of any additional or
        custom ticket properties (e.g. an internal reason field). It is merged
        on top of the named fields, so it can also override them.
        """
        props: dict[str, Any] = {"subject": subject}
        if content:
            props["content"] = content
        if hs_pipeline:
            props["hs_pipeline"] = hs_pipeline
        if hs_pipeline_stage:
            props["hs_pipeline_stage"] = hs_pipeline_stage
        if hs_ticket_priority:
            props["hs_ticket_priority"] = hs_ticket_priority
        if properties_json:
            extra = _coerce_json_arg(properties_json)
            if not isinstance(extra, dict):
                return "Error: Invalid JSON for properties_json (expected an object)."
            props.update(extra)
        async with egress_client(timeout=30) as client:
            resp, err = await _throttled(
                client,
                "post",
                f"{_BASE_URL}/crm/v3/objects/tickets",
                "HubSpot",
                "create ticket",
                headers=headers,
                json={"properties": props},
            )
            if err:
                return err
            t = resp.json()
            return f"Ticket created successfully. ID: {t['id']}"

    async def update_hubspot_ticket(ticket_id: str, properties_json: str) -> str:
        """Update a ticket's properties. Pass properties as a JSON object, e.g.
        {\"hs_pipeline_stage\": \"4\", \"hs_ticket_priority\": \"HIGH\"}.

        Use ``list_hubspot_pipelines`` with object_type='tickets' to discover
        valid pipeline and stage IDs. Custom/internal properties (e.g. a reason
        field) can be set the same way by their internal property name.
        """
        props = _coerce_json_arg(properties_json)
        if not isinstance(props, dict):
            return "Error: Invalid JSON for properties_json (expected an object)."
        async with egress_client(timeout=30) as client:
            _, err = await _throttled(
                client,
                "patch",
                f"{_BASE_URL}/crm/v3/objects/tickets/{ticket_id}",
                "HubSpot",
                "update ticket",
                headers=headers,
                json={"properties": props},
            )
            if err:
                return err
            return f"Ticket {ticket_id} updated successfully."

    # ── Files and record attachments ─────────────────────────────

    async def _file_metadata(
        client: httpx.AsyncClient, file_id: str
    ) -> tuple[dict | None, str | None]:
        resp, err = await _throttled(
            client,
            "get",
            f"{_BASE_URL}/files/v3/files/{file_id}",
            "HubSpot",
            f"get file {file_id}",
            headers=headers,
        )
        if err:
            return None, err
        return resp.json(), None

    async def _record_attachments(
        client: httpx.AsyncClient, object_type: str, record_id: str
    ) -> tuple[list[tuple[str, dict, list[str]]], list[str], str | None]:
        """``(engagements carrying files, warnings, error)`` of one record.

        Each engagement is ``(engagement type, engagement, file ids)``: the
        files of a record are the ``hs_attachment_ids`` of its notes, emails,
        calls, meetings and tasks. An engagement type the token cannot read
        is a warning; the error only when none could be read.
        """
        found: list[tuple[str, dict, list[str]]] = []
        warnings: list[str] = []
        object_type = api_object_type(object_type)
        for engagement_type in _ENGAGEMENT_TYPES:
            resp, err = await _throttled(
                client,
                "get",
                f"{_BASE_URL}/crm/v4/objects/{object_type}/{record_id}/associations/{engagement_type}",
                "HubSpot",
                f"list {engagement_type} of {object_type} {record_id}",
                headers=headers,
                params={"limit": 500},
            )
            if err:
                warnings.append(f"{engagement_type}: {err}")
                continue
            ids = [
                str(r["toObjectId"])
                for r in resp.json().get("results", [])
                if isinstance(r, dict) and r.get("toObjectId")
            ][:_MAX_ENGAGEMENTS_PER_TYPE]
            for start in range(0, len(ids), 100):
                batch, err = await _throttled(
                    client,
                    "post",
                    f"{_BASE_URL}/crm/v3/objects/{engagement_type}/batch/read",
                    "HubSpot",
                    f"read {engagement_type} of {object_type} {record_id}",
                    headers=headers,
                    json={
                        "properties": list(
                            dict.fromkeys(
                                [_ENGAGEMENT_TITLE_PROPERTIES[engagement_type], *ENGAGEMENT_SPECS[engagement_type].properties]
                            )
                        ),
                        "inputs": [{"id": i} for i in ids[start : start + 100]],
                    },
                )
                if err:
                    warnings.append(f"{engagement_type}: {err}")
                    break
                for engagement in batch.json().get("results", []):
                    file_ids = _attachment_ids(engagement)
                    if file_ids:
                        found.append((engagement_type, engagement, file_ids))
        if not found and len(warnings) == len(_ENGAGEMENT_TYPES):
            return [], [], warnings[0].split(": ", 1)[1]
        return found, warnings, None

    async def search_hubspot_files(
        name: str = "",
        extension: str = "",
        file_type: str = "",
        limit: int = 20,
        after: str = "",
    ) -> str:
        """Search the HubSpot file manager (hidden record attachments are not listed)."""
        params: dict[str, Any] = {"limit": max(1, min(limit, 100)), "sort": "-createdAt"}
        if name:
            params["name"] = name
        if extension:
            params["extension"] = extension.strip().lstrip(".")
        if file_type:
            params["type"] = file_type.strip().upper()
        if after:
            params["after"] = after
        async with egress_client(timeout=30) as client:
            resp, err = await _throttled(
                client,
                "get",
                f"{_BASE_URL}/files/v3/files/search",
                "HubSpot",
                "search files",
                headers=headers,
                params=params,
            )
            if err:
                return err
        data = resp.json()
        files = [f for f in data.get("results", []) if isinstance(f, dict)]
        if not files:
            return "No files found in the HubSpot file manager."
        lines = [f"Files ({len(files)}):"]
        lines.extend(f"  {_format_file(f)}" for f in files)
        next_after = ((data.get("paging") or {}).get("next") or {}).get("after")
        lines.append(
            "\nTip: download_hubspot_file(file_id) stores one as a file."
            + format_pagination_hint(next_after)
        )
        return "\n".join(lines)

    async def list_hubspot_attachments(object_type: str, record_id: str) -> str:
        """List the files attached to a CRM record through its engagements."""
        object_type = api_object_type(object_type)
        async with egress_client(timeout=60) as client:
            found, warnings, err = await _record_attachments(client, object_type, record_id)
            if err:
                return err
            lines: list[str] = []
            described = 0
            for engagement_type, engagement, file_ids in found:
                lines.append(f"  {_engagement_label(engagement_type, engagement)}:")
                for fid in file_ids:
                    if described >= _MAX_ATTACHMENTS_DESCRIBED:
                        lines.append(f"    file id: {fid}")
                        continue
                    described += 1
                    meta, meta_err = await _file_metadata(client, fid)
                    lines.append(
                        f"    {_format_file(meta)}"
                        if meta
                        else f"    file id: {fid} ({meta_err.splitlines()[0]})"
                    )
        count = sum(len(ids) for _, _, ids in found)
        text = (
            f"Attachments on {object_type} {record_id} ({count} file(s)):\n" + "\n".join(lines)
            if found
            else f"No attachments on {object_type} {record_id}."
        )
        if warnings:
            text += "\nNot read: " + "; ".join(w.splitlines()[0] for w in warnings)
        if found:
            text += (
                "\n\nTip: download_hubspot_file(file_id, object_type, record_id) stores one as a file."
            )
        return text

    async def _carrying_engagement(
        client: httpx.AsyncClient, fid: str, engagement_kind: str, engagement_id: str
    ) -> tuple[tuple[str, dict] | None, str | ToolResult | None]:
        """The engagement *engagement_id* when it carries file *fid*."""
        type_ = as_engagement_type(engagement_kind)
        eid = str(engagement_id or "").strip()
        if not type_ or not eid.isdigit():
            return None, ToolResult(
                tool_call_id="",
                content="Error: pass engagement_type (notes, emails, calls, meetings, tasks) with a numeric engagement_id.",
                is_error=True,
            )
        resp, err = await _throttled(
            client,
            "get",
            f"{_BASE_URL}/crm/v3/objects/{type_}/{eid}",
            "HubSpot",
            f"read {type_} {eid}",
            headers=headers,
            params={"properties": ",".join(ENGAGEMENT_SPECS[type_].properties)},
        )
        if err:
            return None, err
        engagement = resp.json()
        if fid not in _attachment_ids(engagement):
            return None, ToolResult(
                tool_call_id="", content=f"Error: file {fid} is not attached to {type_} {eid}.", is_error=True
            )
        return (type_, engagement), None

    async def download_hubspot_file(
        file_id: str,
        object_type: str = "",
        record_id: str = "",
        engagement_type: str = "",
        engagement_id: str = "",
    ) -> str | ToolResult:
        """Download a file of the file manager or a record attachment."""
        fid = str(file_id or "").strip()
        object_type = api_object_type(object_type)
        if not fid.isdigit():
            return ToolResult(
                tool_call_id="",
                content=f"Error: '{file_id}' is not a HubSpot file id (digits).",
                is_error=True,
            )
        carrier: tuple[str, dict] | None = None
        async with egress_client(timeout=60) as client:
            if str(record_id or "").strip():
                found, _warnings, err = await _record_attachments(client, object_type, record_id)
                if err and object_type_scope is not None:
                    return err
                carrier = next(((t, e) for t, e, ids in found if fid in ids), None)
            # Under an object-type Scope(s) the file manager is out of reach: a
            # file comes through a record of an allowed type, attached to it.
            if object_type_scope is not None:
                if not str(record_id or "").strip():
                    return ToolResult(
                        tool_call_id="",
                        content=(
                            "Refused: under the Scope(s) set for this agent or assignment a "
                            "HubSpot file is downloaded through the record it is attached to - "
                            "pass object_type and record_id (see list_hubspot_attachments)."
                        ),
                        is_error=True,
                    )
                if carrier is None:
                    return ToolResult(
                        tool_call_id="",
                        content=(
                            f"Refused: file {fid} is not attached to {object_type} {record_id}; "
                            "under the Scope(s) set for this agent or assignment only the files "
                            "of a record of an allowed object type are reachable."
                        ),
                        is_error=True,
                    )
            if carrier is None and str(engagement_id or "").strip():
                carrier, refusal = await _carrying_engagement(client, fid, engagement_type, engagement_id)
                if refusal is not None:
                    return refusal
            engagement_text = ""
            if carrier is not None:
                await activity.owners(client)
                carrying = normalize_engagement(carrier[0], carrier[1], label=activity.label)
                engagement_text = "Attached to: " + format_engagement(
                    carrying, owner=activity.owner_label, body_chars=50_000, indent="  "
                )
            meta, err = await _file_metadata(client, fid)
            if err:
                return err
            name = _file_name(meta)
            file_url = meta.get("url") or meta.get("defaultHostingUrl")
            size = meta.get("size")
            if isinstance(size, int) and size > _MAX_FILE_BYTES:
                return "\n".join(
                    [
                        f"Error: '{name}' is {format_mb(size)}, over the "
                        f"{_MAX_FILE_BYTES // 1024 // 1024} MB download limit.",
                        *link_lines([("File URL", file_url)]),
                    ]
                )
            signed, err = await _throttled(
                client,
                "get",
                f"{_BASE_URL}/files/v3/files/{fid}/signed-url",
                "HubSpot",
                f"sign file {fid}",
                headers=headers,
            )
            if err:
                return err
        signed_url = signed.json().get("url") or ""
        if not signed_url:
            return f"Error: HubSpot returned no signed URL for file {fid}."
        # A signed URL is fetched without the token.
        async with egress_client(timeout=120) as cdn:
            fetched = await fetch_file(
                cdn,
                "get",
                signed_url,
                provider="HubSpot",
                action=f"download file {fid}",
                max_bytes=_MAX_FILE_BYTES,
                follow_redirects=True,
            )
        if isinstance(fetched, str):
            return "\n".join([fetched, *link_lines([("File URL", file_url)])])
        return await deliver_file(
            data=fetched.data,
            filename=name,
            content_type=file_content_type(fetched.content_type, name),
            source="HubSpot",
            persist_file_fn=persist_file_fn,
            links=[("File URL", file_url), ("Download URL", signed_url)],
            details=[
                f"File {fid} ({meta.get('access') or 'unknown access'}); the download URL is "
                f"signed and expires at {signed.json().get('expiresAt') or 'an unstated time'}.",
                *([engagement_text] if engagement_text else []),
            ],
        )

    async def _upload_file(
        client: httpx.AsyncClient,
        data: bytes,
        filename: str,
        content_type: str,
        *,
        folder_path: str,
        access: str,
    ) -> tuple[dict | None, str | None]:
        resp, err = await _throttled(
            client,
            "post",
            f"{_BASE_URL}/files/v3/files",
            "HubSpot",
            "upload the file",
            headers=upload_headers,
            data={
                "options": _json.dumps({"access": access}),
                "folderPath": folder_path,
                "fileName": filename,
            },
            files={"file": (filename, data, content_type)},
        )
        if err:
            return None, err
        return resp.json(), None

    async def upload_hubspot_file(
        file_id: str,
        folder_path: str = "",
        access: str = "PRIVATE",
        file_name: str = "",
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Upload a file of the run to the HubSpot file manager."""
        access_value = (access or "PRIVATE").strip().upper()
        if access_value not in _FILE_ACCESS:
            return ToolResult(
                tool_call_id="",
                content=f"Error: access must be one of {', '.join(_FILE_ACCESS)}.",
                is_error=True,
            )
        piped = await read_pipe_file(
            file_context, file_id, max_bytes=_MAX_FILE_BYTES, target="HubSpot"
        )
        if isinstance(piped, str):
            return ToolResult(tool_call_id="", content=piped, is_error=True)
        data, piped_name, content_type = piped
        file_id = file_id.strip()
        filename = (file_name or "").strip() or piped_name
        async with egress_client(timeout=120) as client:
            uploaded, err = await _upload_file(
                client,
                data,
                filename,
                content_type,
                folder_path=(folder_path or "").strip() or _DEFAULT_UPLOAD_FOLDER,
                access=access_value,
            )
        if err:
            return err
        # HubSpot's file line repeats the name: it goes in the block.
        return uploaded_file_answer(
            f"Uploaded file {file_id} to the HubSpot file manager (file id: {uploaded.get('id', '?')}).",
            target="HubSpot",
            filename=filename,
            details=[f"HubSpot file: {_format_file(uploaded)}"],
        )

    async def upload_hubspot_attachment(
        object_type: str,
        record_id: str,
        file_id: str,
        note: str = "",
        file_name: str = "",
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Attach a file of the run to a CRM record through a new note."""
        object_type = api_object_type(object_type)
        piped = await read_pipe_file(
            file_context, file_id, max_bytes=_MAX_FILE_BYTES, target="HubSpot"
        )
        if isinstance(piped, str):
            return ToolResult(tool_call_id="", content=piped, is_error=True)
        data, piped_name, content_type = piped
        file_id = file_id.strip()
        filename = (file_name or "").strip() or piped_name
        async with egress_client(timeout=120) as client:
            uploaded, err = await _upload_file(
                client,
                data,
                filename,
                content_type,
                folder_path=_DEFAULT_UPLOAD_FOLDER,
                access="PRIVATE",
            )
            if err:
                return err
            hubspot_file_id = str(uploaded.get("id") or "")
            created, err = await _throttled(
                client,
                "post",
                f"{_BASE_URL}/crm/v3/objects/notes",
                "HubSpot",
                f"create the note attaching file {file_id}",
                headers=headers,
                json={
                    "properties": {
                        "hs_timestamp": datetime.now(UTC).isoformat(),
                        "hs_note_body": (note or "").strip() or f"Attached {filename}",
                        "hs_attachment_ids": hubspot_file_id,
                    }
                },
            )
            if err:
                return f"{err}\n(The file was uploaded to the file manager as file {hubspot_file_id}.)"
            note_id = str(created.json().get("id") or "")
            _, err = await _throttled(
                client,
                "put",
                f"{_BASE_URL}/crm/v4/objects/notes/{note_id}/associations/default/{object_type}/{record_id}",
                "HubSpot",
                f"associate note {note_id} with {object_type} {record_id}",
                headers=headers,
            )
            if err:
                return (
                    f"{err}\n(Note {note_id} carrying file {hubspot_file_id} was created but is "
                    f"not associated with {object_type} {record_id}.)"
                )
        return uploaded_file_answer(
            f"Attached file {file_id} to {object_type} {record_id} through note {note_id} "
            f"(file id: {hubspot_file_id or '?'}).",
            target="HubSpot",
            filename=filename,
            details=[f"HubSpot file: {_format_file(uploaded)}"],
        )

    # ── Register handlers ────────────────────────────────────────────────

    _register("get_hubspot_contact", get_hubspot_contact)
    _register("search_hubspot_contacts", search_hubspot_contacts)
    _register("create_hubspot_contact", create_hubspot_contact)
    _register("update_hubspot_contact", update_hubspot_contact)
    _register("get_hubspot_company", get_hubspot_company)
    _register("search_hubspot_companies", search_hubspot_companies)
    _register("create_hubspot_company", create_hubspot_company)
    _register("update_hubspot_company", update_hubspot_company)
    _register("list_hubspot_deals", list_hubspot_deals)
    _register("get_hubspot_deal", get_hubspot_deal)
    _register("search_hubspot_deals", search_hubspot_deals)
    _register("create_hubspot_deal", create_hubspot_deal)
    _register("update_hubspot_deal", update_hubspot_deal)
    _register("list_hubspot_campaigns", list_hubspot_campaigns)
    _register("get_hubspot_campaign", get_hubspot_campaign)
    _register("list_hubspot_pipelines", list_hubspot_pipelines)
    _register("list_hubspot_owners", list_hubspot_owners)
    _register("search_hubspot_owners", search_hubspot_owners)
    _register("get_hubspot_owner", get_hubspot_owner)
    _register("get_hubspot_associations", get_hubspot_associations)
    _register("create_hubspot_association", create_hubspot_association)
    _register("delete_hubspot_association", delete_hubspot_association)
    _register("delete_hubspot_record", delete_hubspot_record)
    _register("search_hubspot_tickets", search_hubspot_tickets)
    _register("get_hubspot_ticket", get_hubspot_ticket)
    _register("create_hubspot_ticket", create_hubspot_ticket)
    _register("update_hubspot_ticket", update_hubspot_ticket)
    _register("search_hubspot_files", search_hubspot_files)
    _register("list_hubspot_attachments", list_hubspot_attachments)
    _register("download_hubspot_file", download_hubspot_file)
    _register("upload_hubspot_file", upload_hubspot_file)
    _register("upload_hubspot_attachment", upload_hubspot_attachment)
    for name, handler in activity.handlers().items():
        _register(name, handler)
    for name, handler in HubSpotLineItems(_throttled, headers=headers).handlers().items():
        _register(name, handler)
    for name, handler in HubSpotNotes(_throttled, headers=headers).handlers().items():
        _register(name, handler)

    # ── Tool definitions ─────────────────────────────────────────────────

    src = "integration:hubspot"

    definitions = [
        # ── Contacts ──
        ToolDefinition(
            name="get_hubspot_contact",
            personal_records=True,
            description=(
                "Get a HubSpot contact by ID: name, email, company, lifecycle stage and lead status (labels, raw ID "
                "alongside), owner (name, email, teams, assignment date), location, first and recent form conversion, "
                "last contacted / last activity, sales email and meeting dates, sequence enrollment; plus any "
                "properties named in properties. associations lists associated records; engagement types (emails, "
                "notes, calls, meetings, tasks) are read and shown, not given as bare IDs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "contact_id": {
                        "type": "string",
                        "description": "HubSpot contact ID",
                    },
                    "associations": {
                        "type": "string",
                        "description": "Comma-separated object types to include associations for (e.g. 'companies,deals,emails,meetings')",
                        "default": "",
                    },
                    "properties": {
                        "type": "string",
                        "description": "Comma-separated extra contact properties to include (list_hubspot_properties lists them)",
                        "default": "",
                    },
                    "raw_values": {
                        "type": "boolean",
                        "description": "Show raw values (stage IDs, owner IDs) instead of labels",
                        "default": False,
                    },
                },
                "required": ["contact_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="search_hubspot_contacts",
            personal_records=True,
            description=(
                "Search, filter, and list HubSpot contacts. Each line: name, email, company, job title, lifecycle stage, "
                "lead status, country, owner (name), created, last contacted, last activity, plus the properties named "
                "in properties. Enumerations and owners show their labels unless raw_values. "
                "Supports free-text search, structured property filters, and sorting by any property. "
                "To list all contacts, call with no query or filters. Up to 500 per call (paged 200 per request). "
                "Example: to find the most recently created contacts, set sort_by='createdate' and sort_direction='DESCENDING'. "
                'Example filters_json: \'[{"propertyName": "lifecyclestage", "operator": "EQ", "value": "lead"}]\'. '
                "Filter operators: EQ, NEQ, GT, GTE, LT, LTE, BETWEEN, IN, HAS_PROPERTY, CONTAINS_TOKEN."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free-text search (name, email, phone, or company). Optional if using filters.",
                        "default": "",
                    },
                    "filters_json": {
                        "type": "string",
                        "description": 'JSON array of filter objects, e.g. [{"propertyName": "createdate", "operator": "GTE", "value": "2025-01-01"}]',
                        "default": "",
                    },
                    "sort_by": {
                        "type": "string",
                        "description": "Property name to sort by (e.g. createdate, lastmodifieddate, firstname)",
                        "default": "",
                    },
                    "sort_direction": {
                        "type": "string",
                        "enum": ["ASCENDING", "DESCENDING"],
                        "description": "Sort direction",
                        "default": "DESCENDING",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max results to return (max 500, paged 200 per request)",
                        "default": 10,
                    },
                    "after": {
                        "type": "string",
                        "description": "Pagination cursor from previous response",
                        "default": "",
                    },
                    "properties": {
                        "type": "string",
                        "description": "Comma-separated extra contact properties to include (e.g. 'hs_analytics_source,hubspot_owner_assigneddate')",
                        "default": "",
                    },
                    "raw_values": {
                        "type": "boolean",
                        "description": "Show raw values (stage IDs, owner IDs) instead of labels",
                        "default": False,
                    },
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="create_hubspot_contact",
            description="Create a new contact in HubSpot CRM.",
            priority=0,
            input_schema={
                "type": "object",
                "properties": {
                    "email": {
                        "type": "string",
                        "description": "Contact email address (required)",
                    },
                    "firstname": {
                        "type": "string",
                        "description": "First name",
                        "default": "",
                    },
                    "lastname": {
                        "type": "string",
                        "description": "Last name",
                        "default": "",
                    },
                    "phone": {
                        "type": "string",
                        "description": "Phone number",
                        "default": "",
                    },
                    "company": {
                        "type": "string",
                        "description": "Company name",
                        "default": "",
                    },
                    "jobtitle": {
                        "type": "string",
                        "description": "Job title",
                        "default": "",
                    },
                    "lifecyclestage": {
                        "type": "string",
                        "description": "Lifecycle stage (e.g. subscriber, lead, opportunity, customer)",
                        "default": "",
                    },
                },
                "required": ["email"],
            },
            source=src,
        ),
        ToolDefinition(
            name="update_hubspot_contact",
            description="Update properties of an existing HubSpot contact. Pass a JSON object of properties to update.",
            input_schema={
                "type": "object",
                "properties": {
                    "contact_id": {
                        "type": "string",
                        "description": "HubSpot contact ID",
                    },
                    "properties_json": {
                        "type": "string",
                        "description": 'JSON object of properties to update, e.g. {"phone": "+1234", "jobtitle": "CTO"}',
                    },
                },
                "required": ["contact_id", "properties_json"],
            },
            source=src,
            priority=1,
        ),
        # ── Companies ──
        ToolDefinition(
            name="get_hubspot_company",
            description=(
                "Get a HubSpot company by ID: name, domain, industry, location, size, revenue, lifecycle stage "
                "(labels, raw ID alongside), owner (name, email, teams), last contacted; plus any properties named in "
                "properties. associations lists associated records; engagement types (emails, notes, calls, "
                "meetings, tasks) are read and shown, not given as bare IDs."
            ),
            priority=0,
            input_schema={
                "type": "object",
                "properties": {
                    "company_id": {
                        "type": "string",
                        "description": "HubSpot company ID",
                    },
                    "associations": {
                        "type": "string",
                        "description": "Comma-separated object types to include associations for (e.g. 'contacts,deals,notes')",
                        "default": "",
                    },
                    "properties": {
                        "type": "string",
                        "description": "Comma-separated extra company properties to include (list_hubspot_properties lists them)",
                        "default": "",
                    },
                    "raw_values": {
                        "type": "boolean",
                        "description": "Show raw values (stage IDs, owner IDs) instead of labels",
                        "default": False,
                    },
                },
                "required": ["company_id"],
            },
            source=src,
        ),
        ToolDefinition(
            name="search_hubspot_companies",
            description=(
                "Search, filter, and list HubSpot companies. Each line: name, domain, industry, country, lifecycle "
                "stage, owner (name), created, plus the properties named in properties. "
                "Supports free-text search, structured property filters, and sorting by any property. "
                "To list all companies, call with no query or filters. Up to 500 per call (paged 200 per request). "
                'Example: to find companies by industry, set filters_json=\'[{"propertyName": "industry", "operator": "EQ", "value": "Technology"}]\'. '
                "Filter operators: EQ, NEQ, GT, GTE, LT, LTE, BETWEEN, IN, HAS_PROPERTY, CONTAINS_TOKEN."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free-text search (company name or domain). Optional if using filters.",
                        "default": "",
                    },
                    "filters_json": {
                        "type": "string",
                        "description": 'JSON array of filter objects, e.g. [{"propertyName": "industry", "operator": "EQ", "value": "Technology"}]',
                        "default": "",
                    },
                    "sort_by": {
                        "type": "string",
                        "description": "Property name to sort by (e.g. createdate, name, annualrevenue)",
                        "default": "",
                    },
                    "sort_direction": {
                        "type": "string",
                        "enum": ["ASCENDING", "DESCENDING"],
                        "description": "Sort direction",
                        "default": "DESCENDING",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max results to return (max 500, paged 200 per request)",
                        "default": 10,
                    },
                    "after": {
                        "type": "string",
                        "description": "Pagination cursor from previous response",
                        "default": "",
                    },
                    "properties": {
                        "type": "string",
                        "description": "Comma-separated extra company properties to include",
                        "default": "",
                    },
                    "raw_values": {
                        "type": "boolean",
                        "description": "Show raw values (stage IDs, owner IDs) instead of labels",
                        "default": False,
                    },
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="create_hubspot_company",
            description="Create a new company in HubSpot CRM.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Company name (required)",
                    },
                    "domain": {
                        "type": "string",
                        "description": "Company domain (e.g. acme.com)",
                        "default": "",
                    },
                    "industry": {
                        "type": "string",
                        "description": "Industry",
                        "default": "",
                    },
                    "phone": {
                        "type": "string",
                        "description": "Phone number",
                        "default": "",
                    },
                    "city": {"type": "string", "description": "City", "default": ""},
                    "state": {
                        "type": "string",
                        "description": "State/Region",
                        "default": "",
                    },
                    "country": {
                        "type": "string",
                        "description": "Country",
                        "default": "",
                    },
                },
                "required": ["name"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="update_hubspot_company",
            description="Update properties of an existing HubSpot company. Pass a JSON object of properties to update.",
            input_schema={
                "type": "object",
                "properties": {
                    "company_id": {
                        "type": "string",
                        "description": "HubSpot company ID",
                    },
                    "properties_json": {
                        "type": "string",
                        "description": 'JSON object of properties to update, e.g. {"industry": "Technology"}',
                    },
                },
                "required": ["company_id", "properties_json"],
            },
            source=src,
            priority=1,
        ),
        # ── Deals ──
        ToolDefinition(
            name="list_hubspot_deals",
            description="List deals from HubSpot CRM with pagination support. Returns deal name, amount, stage, pipeline, close date, and owner.",
            input_schema={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Number of deals to return (max 100, default 20)",
                        "default": 20,
                    },
                    "after": {
                        "type": "string",
                        "description": "Pagination cursor from a previous response",
                        "default": "",
                    },
                },
                "required": [],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="get_hubspot_deal",
            description="Get detailed information about a specific HubSpot deal by ID, optionally including associated records (contacts, companies).",
            priority=0,
            input_schema={
                "type": "object",
                "properties": {
                    "deal_id": {"type": "string", "description": "HubSpot deal ID"},
                    "associations": {
                        "type": "string",
                        "description": "Comma-separated object types to include associations for (e.g. 'contacts,companies')",
                        "default": "",
                    },
                    "properties": {
                        "type": "string",
                        "description": "Comma-separated list of additional deal properties to include (e.g. 'partner_sourced,partner'). These are merged with the default properties (dealname, amount, dealstage, etc.).",
                        "default": "",
                    },
                    "raw_values": {
                        "type": "boolean",
                        "description": "Show raw values (stage IDs, owner IDs) instead of labels",
                        "default": False,
                    },
                },
                "required": ["deal_id"],
            },
            source=src,
        ),
        ToolDefinition(
            name="search_hubspot_deals",
            description=(
                "Search, filter, and list HubSpot deals. Returns name, amount, stage, pipeline, and close date. "
                "Supports free-text search, structured property filters, and sorting by any property. "
                "To list all deals, call with no query or filters. "
                "Example: to find the most recently created deal, set sort_by='createdate' and sort_direction='DESCENDING' and count=1. "
                'Example: to find deals closing this month, use filters_json=\'[{"propertyName": "closedate", "operator": "GTE", "value": "2026-02-01"}, '
                '{"propertyName": "closedate", "operator": "LTE", "value": "2026-02-28"}]\'. '
                "Filter operators: EQ, NEQ, GT, GTE, LT, LTE, BETWEEN, IN, HAS_PROPERTY, CONTAINS_TOKEN."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free-text search (deal name). Optional if using filters.",
                        "default": "",
                    },
                    "filters_json": {
                        "type": "string",
                        "description": 'JSON array of filter objects, e.g. [{"propertyName": "dealstage", "operator": "EQ", "value": "closedwon"}]',
                        "default": "",
                    },
                    "sorts_json": {
                        "type": "string",
                        "description": 'Advanced HubSpot sorts JSON array, e.g. [{"propertyName": "createdate", "direction": "DESCENDING"}]. Overrides sort_by/sort_direction when provided.',
                        "default": "",
                    },
                    "sort_by": {
                        "type": "string",
                        "description": "Property name to sort by (e.g. createdate, hs_lastmodifieddate, amount, closedate)",
                        "default": "",
                    },
                    "sort_direction": {
                        "type": "string",
                        "enum": ["ASCENDING", "DESCENDING"],
                        "description": "Sort direction",
                        "default": "DESCENDING",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max results to return (max 500, paged 200 per request)",
                        "default": 10,
                    },
                    "after": {
                        "type": "string",
                        "description": "Pagination cursor from previous response",
                        "default": "",
                    },
                    "properties": {
                        "type": "string",
                        "description": "Comma-separated list of additional deal properties to include (e.g. 'partner_sourced,partner'). These are merged with the default properties (dealname, amount, dealstage, etc.).",
                        "default": "",
                    },
                    "raw_values": {
                        "type": "boolean",
                        "description": "Show raw values (stage IDs, owner IDs) instead of labels",
                        "default": False,
                    },
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="create_hubspot_deal",
            description="Create a new deal in HubSpot CRM.",
            input_schema={
                "type": "object",
                "properties": {
                    "dealname": {
                        "type": "string",
                        "description": "Deal name (required)",
                    },
                    "amount": {
                        "type": "string",
                        "description": "Deal amount (numeric string)",
                        "default": "",
                    },
                    "dealstage": {
                        "type": "string",
                        "description": "Deal stage ID (use list_hubspot_pipelines to get valid stage IDs)",
                        "default": "",
                    },
                    "pipeline": {
                        "type": "string",
                        "description": "Pipeline ID (use list_hubspot_pipelines to get valid pipeline IDs)",
                        "default": "",
                    },
                    "closedate": {
                        "type": "string",
                        "description": "Expected close date (ISO format, e.g. 2025-12-31)",
                        "default": "",
                    },
                },
                "required": ["dealname"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="update_hubspot_deal",
            description="Update properties of an existing HubSpot deal. Pass a JSON object of properties to update.",
            input_schema={
                "type": "object",
                "properties": {
                    "deal_id": {"type": "string", "description": "HubSpot deal ID"},
                    "properties_json": {
                        "type": "string",
                        "description": 'JSON object of properties to update, e.g. {"amount": "50000", "dealstage": "closedwon"}',
                    },
                },
                "required": ["deal_id", "properties_json"],
            },
            source=src,
            priority=1,
        ),
        # ── Campaigns ──
        ToolDefinition(
            name="list_hubspot_campaigns",
            description="List marketing email campaigns from HubSpot with name, subject, type, and status.",
            priority=2,
            input_schema={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Number of campaigns to return (max 100)",
                        "default": 20,
                    },
                    "after": {
                        "type": "string",
                        "description": "Pagination cursor from a previous response",
                        "default": "",
                    },
                },
                "required": [],
            },
            source=src,
        ),
        ToolDefinition(
            name="get_hubspot_campaign",
            description="Get detailed information about a specific HubSpot marketing email campaign including statistics.",
            input_schema={
                "type": "object",
                "properties": {
                    "campaign_id": {
                        "type": "string",
                        "description": "Marketing email / campaign ID",
                    },
                },
                "required": ["campaign_id"],
            },
            source=src,
            priority=1,
        ),
        # ── Utility ──
        ToolDefinition(
            name="list_hubspot_pipelines",
            description="List deal or ticket pipelines and their stages. Use this to discover valid stage IDs for creating/updating deals.",
            priority=2,
            input_schema={
                "type": "object",
                "properties": {
                    "object_type": {
                        "type": "string",
                        "description": "Object type: 'deals' or 'tickets'",
                        "default": "deals",
                    },
                },
                "required": [],
            },
            source=src,
        ),
        ToolDefinition(
            name="list_hubspot_owners",
            description="List HubSpot owners (team members) who can be assigned to contacts, companies, or deals.",
            priority=2,
            input_schema={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Number of owners to return (max 100)",
                        "default": 50,
                    },
                },
                "required": [],
            },
            source=src,
        ),
        ToolDefinition(
            name="search_hubspot_owners",
            description=(
                "Search HubSpot owners (team members) by name or email. "
                "Matches a case-insensitive substring against first/last name "
                "and email (a full email is matched exactly). Leave query empty "
                "to list owners."
            ),
            priority=2,
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Name or email (or substring) to match. "
                            "Empty lists all owners."
                        ),
                        "default": "",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of owners to return (max 100)",
                        "default": 50,
                        "minimum": 1,
                        "maximum": 100,
                    },
                },
                "required": [],
            },
            source=src,
        ),
        ToolDefinition(
            name="get_hubspot_owner",
            description=(
                "Get a single HubSpot owner (team member) by ID — name, email, "
                "user ID, teams, and timestamps. Use after list_hubspot_owners "
                "or with an owner ID from a contact/company/deal."
            ),
            priority=2,
            input_schema={
                "type": "object",
                "properties": {
                    "owner_id": {
                        "type": "string",
                        "description": (
                            "Numeric HubSpot owner ID to retrieve (e.g. '12345678'). "
                            "Must be a numeric string — not a CRM record ID. "
                            "Use search_hubspot_owners or list_hubspot_owners to look up the ID first."
                        ),
                    },
                },
                "required": ["owner_id"],
            },
            source=src,
        ),
        # ── Associations ──
        ToolDefinition(
            name="get_hubspot_associations",
            description=(
                "Get records associated with a given HubSpot record. "
                "Example: get all contacts associated with a deal, or all deals associated with a company. "
                "Object types: contacts, companies, deals, tickets; to_object_type may also be an engagement type "
                "(emails, notes, calls, meetings, tasks, communications), returned readable (time, direction, owner, "
                "subject, body preview), newest first."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "object_type": {
                        "type": "string",
                        "description": "Source object type (e.g. 'deals', 'contacts', 'companies', 'tickets')",
                    },
                    "object_id": {"type": "string", "description": "Source record ID"},
                    "to_object_type": {
                        "type": "string",
                        "description": "Target object type to get associations for (e.g. 'contacts', 'companies', 'deals')",
                    },
                },
                "required": ["object_type", "object_id", "to_object_type"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="create_hubspot_association",
            description=(
                "Associate two HubSpot records (e.g. a contact to a company, a deal to a contact). Without label or "
                "association_type_id: the default association type. With a label name (e.g. 'Primary' from deals to "
                "companies: the deal's primary company) or association_category + association_type_id: that type is "
                "added, the records' other types kept. To move a label, add it on the new record and remove it from "
                "the old one with delete_hubspot_association."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "from_type": {
                        "type": "string",
                        "description": "Source object type (e.g. 'contacts', 'companies', 'deals')",
                    },
                    "from_id": {"type": "string", "description": "Source record ID"},
                    "to_type": {"type": "string", "description": "Target object type"},
                    "to_id": {"type": "string", "description": "Target record ID"},
                    **_ASSOCIATION_TYPE_ARGUMENTS,
                },
                "required": ["from_type", "from_id", "to_type", "to_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="delete_hubspot_association",
            description=(
                "Remove one association type (a label, e.g. 'Primary' from a deal to a company) between two HubSpot "
                "records, the records staying associated by their other types; without label or "
                "association_type_id, remove every association between them."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "from_type": {"type": "string", "description": "Source object type (e.g. 'deals')"},
                    "from_id": {"type": "string", "description": "Source record ID"},
                    "to_type": {"type": "string", "description": "Target object type (e.g. 'companies')"},
                    "to_id": {"type": "string", "description": "Target record ID"},
                    **_ASSOCIATION_TYPE_ARGUMENTS,
                },
                "required": ["from_type", "from_id", "to_type", "to_id"],
            },
            source=src,
            priority=1,
        ),
        # ── Delete ──
        ToolDefinition(
            name="delete_hubspot_record",
            description="Archive (soft-delete) a HubSpot CRM record. Works for contacts, companies, deals, tickets, and other object types.",
            input_schema={
                "type": "object",
                "properties": {
                    "object_type": {
                        "type": "string",
                        "description": "Object type (e.g. 'contacts', 'companies', 'deals', 'tickets')",
                    },
                    "record_id": {
                        "type": "string",
                        "description": "Record ID to archive",
                    },
                },
                "required": ["object_type", "record_id"],
            },
            source=src,
            priority=1,
        ),
        # ── Tickets ──
        ToolDefinition(
            name="search_hubspot_tickets",
            priority=0,
            description=(
                "Search and filter HubSpot support tickets. Supports free-text search, structured property filters "
                "(e.g. filter by priority, pipeline stage), and sorting by any property. "
                "Filter operators: EQ, NEQ, GT, GTE, LT, LTE, BETWEEN, IN, HAS_PROPERTY, CONTAINS_TOKEN."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free-text search (ticket subject). Optional if using filters.",
                        "default": "",
                    },
                    "filters_json": {
                        "type": "string",
                        "description": "JSON array of filter objects",
                        "default": "",
                    },
                    "sort_by": {
                        "type": "string",
                        "description": "Property name to sort by (e.g. createdate, hs_lastmodifieddate, hs_ticket_priority)",
                        "default": "",
                    },
                    "sort_direction": {
                        "type": "string",
                        "enum": ["ASCENDING", "DESCENDING"],
                        "description": "Sort direction",
                        "default": "DESCENDING",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max results to return (max 500, paged 200 per request)",
                        "default": 10,
                    },
                    "after": {
                        "type": "string",
                        "description": "Pagination cursor from previous response",
                        "default": "",
                    },
                    "raw_values": {
                        "type": "boolean",
                        "description": "Show raw values (stage IDs, owner IDs) instead of labels",
                        "default": False,
                    },
                    "properties": {
                        "type": "string",
                        "description": "Extra ticket properties to return, custom ones included, comma-separated",
                        "default": "",
                    },
                },
                "required": [],
            },
            source=src,
        ),
        ToolDefinition(
            name="get_hubspot_ticket",
            description="Get detailed information about a specific HubSpot support ticket by ID, optionally including associated records.",
            priority=0,
            input_schema={
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string", "description": "HubSpot ticket ID"},
                    "associations": {
                        "type": "string",
                        "description": "Comma-separated object types to include associations for (e.g. 'contacts,companies')",
                        "default": "",
                    },
                    "properties": {
                        "type": "string",
                        "description": "Extra ticket properties to return, custom ones included, comma-separated",
                        "default": "",
                    },
                    "raw_values": {
                        "type": "boolean",
                        "description": "Show raw values (stage IDs, owner IDs) instead of labels",
                        "default": False,
                    },
                },
                "required": ["ticket_id"],
            },
            source=src,
        ),
        ToolDefinition(
            name="create_hubspot_ticket",
            description=(
                "Create a new support ticket in HubSpot. Set the pipeline and stage "
                "with hs_pipeline / hs_pipeline_stage (use list_hubspot_pipelines with "
                "object_type='tickets' to discover valid IDs). Use properties_json to "
                "set any additional or custom ticket properties."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "description": "Ticket subject / title (required)",
                    },
                    "content": {
                        "type": "string",
                        "description": "Ticket description / body",
                        "default": "",
                    },
                    "hs_pipeline": {
                        "type": "string",
                        "description": "Pipeline ID (use list_hubspot_pipelines with object_type='tickets')",
                        "default": "",
                    },
                    "hs_pipeline_stage": {
                        "type": "string",
                        "description": "Pipeline stage ID (use list_hubspot_pipelines with object_type='tickets')",
                        "default": "",
                    },
                    "hs_ticket_priority": {
                        "type": "string",
                        "description": "Priority (LOW, MEDIUM, or HIGH)",
                        "default": "",
                    },
                    "properties_json": {
                        "type": "string",
                        "description": 'JSON object of any additional/custom ticket properties, e.g. {"hs_ticket_category": "billing"}',
                        "default": "",
                    },
                },
                "required": ["subject"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="update_hubspot_ticket",
            description=(
                "Update properties of an existing HubSpot support ticket. Pass a JSON "
                "object of properties to update. Use this to change the ticket stage "
                "(hs_pipeline_stage), priority (hs_ticket_priority), or write any "
                "custom/internal property (e.g. a reason field) by its internal name. "
                "Use list_hubspot_pipelines with object_type='tickets' to discover valid "
                "stage IDs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string", "description": "HubSpot ticket ID"},
                    "properties_json": {
                        "type": "string",
                        "description": 'JSON object of properties to update, e.g. {"hs_pipeline_stage": "4", "hs_ticket_priority": "HIGH"}',
                    },
                },
                "required": ["ticket_id", "properties_json"],
            },
            source=src,
            priority=1,
        ),
        # ── Files and record attachments ──
        ToolDefinition(
            name="search_hubspot_files",
            description=(
                "Search the HubSpot file manager by name, extension or type, newest first. "
                "Each file comes with its id, size, access and URL. Files attached to CRM "
                "records are hidden from the file manager: use list_hubspot_attachments for them."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Text the file name contains", "default": ""},
                    "extension": {"type": "string", "description": "File extension (e.g. 'pdf')", "default": ""},
                    "file_type": {
                        "type": "string",
                        "description": "IMG, DOCUMENT, AUDIO, MOVIE or OTHER",
                        "default": "",
                    },
                    "limit": {"type": "integer", "description": "Max files (default 20, max 100)", "default": 20},
                    "after": {"type": "string", "description": "Pagination cursor from a previous call", "default": ""},
                },
                "required": [],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="list_hubspot_attachments",
            description=(
                "List the files attached to a CRM record (contact, company, deal, ticket...) "
                "through its notes, emails, calls, meetings and tasks: each file's id, name, "
                "size, type and URL, grouped by engagement."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "object_type": {
                        "type": "string",
                        "description": "Record object type (e.g. 'contacts', 'companies', 'deals', 'tickets')",
                    },
                    "record_id": {"type": "string", "description": "Record ID"},
                },
                "required": ["object_type", "record_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="download_hubspot_file",
            workspace_ingest=True,
            description=(
                "Download a HubSpot file (a file-manager file or a record attachment) into the "
                "conversation through a signed link: a download card, its links and a file_id "
                "to forward to any tool that takes a file; its readable content comes with the "
                "result. Pass object_type and record_id for a record attachment (required when "
                "a Scope(s) restricts the object types), or engagement_type and engagement_id: "
                "the full text of the note, email, call, meeting or task carrying the file comes "
                "with it. Include the returned [[FILE:...]] marker verbatim so the user gets a "
                "download card."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "HubSpot file ID"},
                    "object_type": {
                        "type": "string",
                        "description": "Object type of the record the file is attached to",
                        "default": "",
                    },
                    "record_id": {
                        "type": "string",
                        "description": "ID of the record the file is attached to",
                        "default": "",
                    },
                    "engagement_type": {
                        "type": "string",
                        "description": "Type of the engagement carrying the file (notes, emails, calls, meetings, tasks)",
                        "default": "",
                    },
                    "engagement_id": {
                        "type": "string",
                        "description": "ID of the engagement carrying the file",
                        "default": "",
                    },
                },
                "required": ["file_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="upload_hubspot_file",
            description=(
                "Upload a file of this conversation to the HubSpot file manager byte-for-byte: "
                "any file attached by the user, generated, or downloaded by another tool, by "
                "its file_id. To attach it to a CRM record use upload_hubspot_attachment."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "file_id of a file of this conversation"},
                    "folder_path": {
                        "type": "string",
                        "description": f"Destination folder path (default '{_DEFAULT_UPLOAD_FOLDER}')",
                        "default": "",
                    },
                    "access": {
                        "type": "string",
                        "description": "PRIVATE (default), PUBLIC_NOT_INDEXABLE or PUBLIC_INDEXABLE",
                        "default": "PRIVATE",
                    },
                    "file_name": {
                        "type": "string",
                        "description": "Optional file name (default: the file's own name)",
                        "default": "",
                    },
                },
                "required": ["file_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="upload_hubspot_attachment",
            description=(
                "Attach a file of this conversation to a CRM record byte-for-byte, as HubSpot "
                "does from its UI: the file is uploaded privately and a note carrying it is added "
                "to the record's timeline. Takes any file attached by the user, generated, or "
                "downloaded by another tool, by its file_id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "object_type": {
                        "type": "string",
                        "description": "Record object type (e.g. 'contacts', 'companies', 'deals', 'tickets')",
                    },
                    "record_id": {"type": "string", "description": "Record ID"},
                    "file_id": {"type": "string", "description": "file_id of a file of this conversation"},
                    "note": {
                        "type": "string",
                        "description": "Body of the note carrying the file (default: 'Attached' followed by the file name)",
                        "default": "",
                    },
                    "file_name": {
                        "type": "string",
                        "description": "Optional file name (default: the file's own name)",
                        "default": "",
                    },
                },
                "required": ["object_type", "record_id", "file_id"],
            },
            source=src,
            priority=1,
        ),
        *activity_definitions(src),
        *line_item_definitions(src),
        *note_definitions(src),
    ]
    definitions = [replace(d, description=d.description + scopes_sentence(d.name)) for d in definitions]
    if object_type_scope is not None:
        return apply_object_type_scope(definitions, executor, object_type_scope)
    return definitions
