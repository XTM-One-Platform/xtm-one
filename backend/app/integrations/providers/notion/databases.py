"""Notion database tools: query, list, schema, templates, create, update."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import quote, unquote

from app.tools.base import ToolDefinition

from ._common import (
    _BASE_URL,
    _DATABASE_ROW_PARENT_TYPES,
    _PAGE_NOT_DATABASE_SENTINEL,
    _PROVIDER,
    _QUERY_FULL_CONTENT_THRESHOLD,
    _QUERY_PREVIEW_THRESHOLD,
    _READ_PAGE_MAX_CHARS,
    _RESOLUTION_FAILED_SENTINEL,
    NotionContext,
    _block_to_text,
    _extract_title,
    _is_page_not_database_400,
    _parse_json_arg,
    _resolution_failed_error,
    format_entity_list,
    logger,
)
from .formatting import (
    _format_database_schema,
    _list_empty_properties,
    _page_row_json,
    _summarize_properties,
)

# What ``query_notion_database`` answers with: text for a reader, rows as data.
_QUERY_OUTPUT_FORMATS = ("text", "json")

# ── Filter type-mismatch diagnosis helpers ────────────────────────────────────


def _is_filter_type_mismatch_400(raw_resp: Any) -> bool:
    """Return True if *raw_resp* is a Notion 400 caused by a property-type mismatch.

    Notion's error message for this case contains both ``"property type"`` and
    ``"does not match"``, e.g.::

        "The property type in the database does not match the property type
        of the filter provided: database property status does not match
        filter select"
    """
    if raw_resp is None:
        return False
    try:
        if raw_resp.status_code != 400:
            return False
        msg = (raw_resp.json().get("message") or "").lower()
        return "does not match" in msg and "property type" in msg
    except Exception:
        return False


def _is_filter_undefined_type_400(raw_resp: Any) -> bool:
    """Return True if *raw_resp* is a Notion 400 caused by a missing filter type key.

    Notion emits this when a filter condition has a ``property`` key but no
    type-discriminator key at all, e.g.::

        {"property": "Name"}   ← missing 'title', 'rich_text', 'number', etc.

    The resulting error looks like::

        "body.filter.and[0].title/rich_text/number/checkbox should be defined,
         instead was 'undefined'"

    The predicate requires the message to reference ``body.filter`` — Notion
    reports missing *non-filter* fields with the same "should be defined /
    undefined" wording (e.g. ``body.sorts[0].direction should be defined``),
    and those must not be misdiagnosed as a missing filter type key.
    """
    if raw_resp is None:
        return False
    try:
        if raw_resp.status_code != 400:
            return False
        msg = (raw_resp.json().get("message") or "").lower()
        return "body.filter" in msg and "should be defined" in msg and "undefined" in msg
    except Exception:
        return False


def _is_filter_properties_invalid_400(raw_resp: Any) -> bool:
    """Return True if *raw_resp* is a Notion 400 caused by an invalid filter_properties attribute.

    Notion emits this when the ``filter_properties`` query-string parameter
    contains a property name that does not exist in the database schema, e.g.::

        "The schema for the data source with id X is malformed and contains
         the following errors: filter_properties contains an invalid attribute: Now"

    The predicate is intentionally narrow: it requires both ``"filter_properties"``
    and ``"invalid attribute"`` in the message to avoid matching unrelated 400s.
    """
    if raw_resp is None:
        return False
    try:
        if raw_resp.status_code != 400:
            return False
        msg = (raw_resp.json().get("message") or "").lower()
        return "filter_properties" in msg and "invalid attribute" in msg
    except Exception:
        return False


def _resolve_filter_properties(
    requested: list[str], schema_props: dict
) -> tuple[list[str], list[str]]:
    """``(property ids, unknown entries)`` for a ``filter_properties`` list.

    The query takes property IDS; a model names the properties it sees, and
    guesses some ("Name" for a title called otherwise). Each entry is matched
    to a property of the data source schema by id, else by name (case and
    surrounding spaces ignored); what matches nothing is returned apart, for
    the answer to name, rather than failing the whole query.
    """
    by_id: dict[str, str] = {}
    by_name: dict[str, str] = {}
    for name, prop in (schema_props or {}).items():
        prop_id = str((prop or {}).get("id") or "") if isinstance(prop, dict) else ""
        if not prop_id:
            continue
        by_id[unquote(prop_id)] = prop_id
        by_name[str(name).strip().casefold()] = prop_id
    ids: list[str] = []
    unknown: list[str] = []
    for entry in requested:
        prop_id = by_id.get(unquote(entry)) or by_name.get(entry.strip().casefold())
        if prop_id is None:
            unknown.append(entry)
        elif prop_id not in ids:
            ids.append(prop_id)
    return ids, unknown


#: How many of a schema's property names a note about unknown entries lists.
_NOTE_PROPERTY_NAMES = 40


def _unknown_filter_properties_note(unknown: list[str], schema_props: dict) -> str:
    """The line a query answer opens with when ``filter_properties`` named properties the database lacks."""
    names = sorted(str(name) for name in schema_props)
    shown = ", ".join(names[:_NOTE_PROPERTY_NAMES]) + (", ..." if len(names) > _NOTE_PROPERTY_NAMES else "")
    return (
        f"Note: filter_properties left out {', '.join(unknown)}: not a property of this database "
        f"(its properties: {shown})."
    )


def _validate_filter_missing_type(filter_dict: dict, path: str = "filter") -> str | None:
    """Return an error string if any filter condition is missing its type key.

    Notion requires each simple condition to have exactly one type key
    (e.g. ``title``, ``rich_text``, ``number``, ``checkbox``, ``select``, etc.)
    alongside the ``property`` key.  A condition with only
    ``{"property": "Name"}`` and no type key causes Notion to reject the
    entire request with a 400::

        body.filter.and[0].title/rich_text/number/checkbox should be defined,
        instead was 'undefined'

    This pre-flight check catches such malformed conditions before the HTTP
    call so the agent gets an actionable error immediately.

    Returns ``None`` if the filter is structurally valid, or an actionable
    error string (suitable for returning to the LLM) if a condition is
    missing its type key.
    """
    if not isinstance(filter_dict, dict):
        return None

    # Compound filter — recursively validate each sub-condition. Both branches
    # are checked when the dict (incorrectly) carries "and" AND "or" at the
    # same level — a common LLM mistake — so a malformed entry in the second
    # branch cannot slip past the pre-flight check.
    has_compound = False
    for compound_key in ("and", "or"):
        sub_conditions = filter_dict.get(compound_key)
        if sub_conditions is None:
            continue
        has_compound = True
        if not isinstance(sub_conditions, list):
            return (
                f"Error: filter_json '{path}.{compound_key}' must be a list of "
                f"filter conditions, not {type(sub_conditions).__name__}. "
                "See the Notion filter API docs for the correct structure."
            )
        for i, sub in enumerate(sub_conditions):
            # A non-object entry (string, number, list, null) can never be
            # a valid condition — fail fast with the exact path instead of
            # letting Notion answer with an opaque 400 later.
            if not isinstance(sub, dict):
                return (
                    f"Error: filter condition at {path}.{compound_key}[{i}] "
                    f"must be a JSON object, not {type(sub).__name__}. "
                    "Each entry of an 'and'/'or' group must be a filter "
                    'condition like {"property": "NAME", "TYPE": {...}}.'
                )
            err = _validate_filter_missing_type(sub, path=f"{path}.{compound_key}[{i}]")
            if err:
                return err
    if has_compound:
        return None  # compound filter with (structurally) valid sub-conditions

    # Simple condition — a ``property`` key without any type key is malformed.
    if filter_dict.get("property") is not None and _extract_filter_type_key(filter_dict) is None:
        prop = filter_dict.get("property", "<unknown>")
        return (
            f"Error: filter condition at {path} for property '{prop}' is missing "
            "the filter type key (e.g. 'title', 'rich_text', 'number', 'checkbox', "
            "'select', 'multi_select', 'date', 'status', etc.). "
            "Notion would reject this with a 400 'should be defined' error. "
            "Call get_notion_database first to see the property types, then add "
            "the correct type key to the filter condition, e.g.: "
            f'{{\"property\": \"{prop}\", \"TYPE\": {{\"contains\": \"value\"}}}}.'
        )

    return None


def _extract_filter_property_name(filter_dict: dict) -> str | None:
    """Return the ``property`` value from a simple Notion filter dict."""
    if not isinstance(filter_dict, dict):
        return None
    prop = filter_dict.get("property")
    return str(prop) if prop else None


def _extract_filter_type_key(filter_dict: dict) -> str | None:
    """Return the filter-type discriminator key (e.g. ``'select'``, ``'status'``).

    Notion filter objects have the shape::

        {"property": "NAME", "TYPE": {<condition>}}

    The type key is the first key that is not ``"property"``, ``"timestamp"``,
    or a compound operator (``"and"``/``"or"``).
    """
    if not isinstance(filter_dict, dict):
        return None
    return next(
        (k for k in filter_dict if k not in {"property", "timestamp", "and", "or"}),
        None,
    )


# Condition operators that are only valid for *some* property types. When the
# corrected filter swaps the type key, an operator carried over from the wrong
# type guess can itself be invalid (e.g. ``equals`` from a ``select`` guess is
# rejected for a ``multi_select`` property, which only supports ``contains``).
# Only the known-invalid combinations are rewritten; everything else passes
# through unchanged.
_OPERATOR_ADJUSTMENTS: dict[str, dict[str, str]] = {
    "multi_select": {"equals": "contains", "does_not_equal": "does_not_contain"},
    "select": {"contains": "equals", "does_not_contain": "does_not_equal"},
    "status": {"contains": "equals", "does_not_contain": "does_not_equal"},
}


def _adjust_condition_operators(actual_type: str, condition: Any) -> Any:
    """Rewrite condition operators that are invalid for *actual_type*."""
    mapping = _OPERATOR_ADJUSTMENTS.get(actual_type)
    if not mapping or not isinstance(condition, dict):
        return condition
    return {mapping.get(op, op): value for op, value in condition.items()}


def _build_filter_mismatch_hint(
    filter_dict: dict,
    schema_props: dict,
    notion_msg: str,
) -> str:
    """Build an actionable error message for a filter property-type mismatch.

    Parameters
    ----------
    filter_dict:
        The parsed filter dict that caused the 400.
    schema_props:
        The ``properties`` dict from the Notion data source schema response.
    notion_msg:
        The raw ``message`` field from Notion's 400 JSON body.
    """
    prop_name = _extract_filter_property_name(filter_dict)
    filter_type_key = _extract_filter_type_key(filter_dict)

    if not prop_name or not filter_type_key:
        # Compound filter (``and``/``or``) — give a generic but actionable message.
        return (
            f"Error: Notion rejected the filter — bad request. {notion_msg}\n"
            "Tip: call get_notion_database first to verify all property names and "
            "their exact types, then rebuild the filter using the actual type key."
        )

    # Find the actual property type (case-insensitive match on the property name).
    actual_type: str | None = None
    matched_name: str | None = None
    for name, prop in schema_props.items():
        if name.lower() == prop_name.lower():
            actual_type = prop.get("type") if isinstance(prop, dict) else None
            matched_name = name
            break

    canonical_name = matched_name or prop_name

    if not actual_type:
        return (
            f"Error: Notion filter type mismatch — property '{prop_name}' was not "
            f"found in the database schema. {notion_msg}. "
            "Call get_notion_database to verify the exact property name and type, then retry."
        )

    # Build a corrected filter suggestion. The condition operator is adjusted
    # when the original one is invalid for the actual type (e.g. a ``select``
    # guess used ``equals`` but the property is ``multi_select``, which only
    # supports ``contains``).
    filter_value = _adjust_condition_operators(
        actual_type, filter_dict.get(filter_type_key)
    )
    corrected: dict = {"property": canonical_name, actual_type: filter_value}
    corrected_json = json.dumps(corrected)

    return (
        f"Error: Notion filter type mismatch — property '{canonical_name}' "
        f"has type '{actual_type}' in this database, but the filter used '{filter_type_key}'. "
        f"Corrected filter: {corrected_json}. "
        "Call get_notion_database first to verify all property types before filtering."
    )


def register(ctx: NotionContext, executor) -> list[ToolDefinition]:
    _client = ctx.client
    safe_request = ctx.request
    _fetch_all_blocks = ctx.fetch_all_blocks
    _fetch_page_preview = ctx.fetch_page_preview
    _resolve_relation_titles = ctx.resolve_relation_titles
    _resolve_data_source_id = ctx.resolve_data_source_id

    async def _data_source_properties(client: Any, ds_id: str) -> dict:
        """The ``properties`` of a data source's schema; empty when it cannot be read."""
        resp, err = await safe_request(
            client,
            "get",
            f"{_BASE_URL}/data_sources/{ds_id}",
            _PROVIDER,
            "retrieve schema for filter_properties",
        )
        if err or resp is None:
            return {}
        try:
            props = resp.json().get("properties") or {}
        except Exception:
            return {}
        return props if isinstance(props, dict) else {}

    async def query_notion_database(
        database_id: str,
        filter_json: str = "",
        sorts_json: str = "",
        count: int = 20,
        filter_properties: str = "",
        output_format: str = "text",
    ) -> str:
        """Query a Notion database. Optionally provide filter and/or sorts as JSON strings.

        Filter example: {"property": "Status", "select": {"equals": "Done"}}
        Sorts example: [{"property": "Created", "direction": "descending"}]
        If you get a 400 error, check the filter syntax against the Notion API docs.

        ``filter_properties`` (optional): comma-separated property names or IDs
        to include in results (improves speed for large schemas).

        Note: The ``database_id`` can be either a database ID or a data_source ID.
        If a database ID is given, it is automatically resolved to the primary
        data_source ID (required by Notion API 2025-09-03).

        **Smart content expansion**: When the query returns ≤5 results, full
        page content is automatically included (eliminating the need for
        follow-up ``get_notion_page`` calls).  When 6–20 results are
        returned, a content preview (~500 chars) is included per page.

        ``output_format="json"`` answers with data instead of text, for a flow
        or a script that reads the rows: ``{"results": [...], "has_more": ...}``
        where each row carries its id, url, timestamps and each property's
        value as plain JSON (``_page_row_json``): a date with both ends, a
        number or a boolean typed, people as ``{id, name}``, related pages by
        id, files by name, and the properties Notion cut at 25 references
        named in ``truncated``. No page content is fetched. A failure still
        answers with the ``Error: ...`` text every format shares.
        """
        fmt = str(output_format or "text").strip().lower()
        if fmt not in _QUERY_OUTPUT_FORMATS:
            return (
                "Error: output_format must be one of "
                f"{', '.join(_QUERY_OUTPUT_FORMATS)}, got {output_format!r}."
            )
        # ``filter_json``/``sorts_json`` may arrive as either a string or
        # a parsed dict/list (handled by ``_parse_json_arg`` below). Use
        # ``json.dumps`` for the log line — it produces a stable,
        # round-trippable representation for both shapes (a plain
        # ``str()`` on a deeply nested dict yields Python repr that's
        # noisy and inconsistent with the actual wire payload).
        # ``default=str`` keeps the call total-function-safe against
        # unexpected non-serialisable types — we never want logging to
        # mask the real error from the LLM.
        def _short_log(value: object) -> str:
            if not value:
                return "(none)"
            try:
                return json.dumps(value, default=str)[:200]
            except Exception:
                return repr(value)[:200]

        logger.info(
            "query_notion_database: database_id=%s filter=%s sorts=%s count=%d",
            database_id,
            _short_log(filter_json),
            _short_log(sorts_json),
            count,
        )
        payload: dict = {"page_size": min(count, 100)}
        if filter_json is not None and filter_json != "":
            parsed_filter, parse_err = _parse_json_arg(
                filter_json, what="filter_json", expected="object"
            )
            if parse_err:
                return parse_err
            # Notion's filter must be a JSON object (single condition or a
            # compound ``and``/``or`` group). A list or scalar would
            # produce an opaque 400 from Notion that the LLM would then
            # retry — fast-fail with an actionable error instead.
            if not isinstance(parsed_filter, dict):
                return (
                    "Error: filter_json must be a JSON object describing "
                    "a single filter condition or a compound "
                    "``and``/``or`` group, not a "
                    f"{type(parsed_filter).__name__}."
                )
            # Pre-flight: reject any condition missing a type key before we
            # hit Notion's opaque "should be defined, instead was 'undefined'"
            # 400 — this gives the agent an actionable error immediately.
            missing_type_err = _validate_filter_missing_type(parsed_filter)
            if missing_type_err:
                return missing_type_err
            payload["filter"] = parsed_filter
        if sorts_json is not None and sorts_json != "":
            parsed_sorts, parse_err = _parse_json_arg(
                sorts_json, what="sorts_json", expected="array"
            )
            if parse_err:
                return parse_err
            # Notion's sorts must be a JSON array of sort objects. A dict
            # or scalar would 400 from Notion; fail fast and clearly.
            if not isinstance(parsed_sorts, list):
                return (
                    "Error: sorts_json must be a JSON array of sort "
                    "objects, e.g. "
                    '[{"property": "Created", "direction": "descending"}]'
                    f"; got {type(parsed_sorts).__name__}."
                )
            payload["sorts"] = parsed_sorts

        # Build query params for filter_properties (performance optimization)
        prop_list: list[str] = []
        if filter_properties:
            prop_list = [p.strip() for p in filter_properties.split(",") if p.strip()]

        async with _client() as client:
            # API 2025-09-03: query endpoint moved to /v1/data_sources/{id}/query
            ds_id = await _resolve_data_source_id(client, database_id)
            if ds_id == _PAGE_NOT_DATABASE_SENTINEL:
                return (
                    f"Error: '{database_id}' is a Notion page, not a database. "
                    "Use get_notion_page to read this page's content instead of "
                    "query_notion_database."
                )
            if ds_id == _RESOLUTION_FAILED_SENTINEL:
                return _resolution_failed_error(database_id)

            # ``stale_ds_refreshed`` guards the single stale-retry: when the
            # POST query returns 404 for a resolved ds_id (the data source was
            # rotated after the initial resolution), re-resolve once and retry.
            # The retry is skipped when ds_id == database_id (the input was
            # already a data_source_id and re-resolving the same stale ID would
            # just 404 again — the resolution-failed sentinel above already
            # handles that path).
            stale_ds_refreshed = False
            # ``filter_properties`` rejected as given: resolved once against
            # the schema (names to ids, unknown entries dropped and named).
            fp_resolved = False
            fp_note = ""

            all_results: list[dict] = []
            more_in_notion = False
            while len(all_results) < count:
                fp_qs = ""
                if prop_list:
                    # An id from the schema is already percent-encoded: encoded once, never twice.
                    fp_qs = "?" + "&".join(
                        f"filter_properties[]={quote(unquote(p), safe='')}" for p in prop_list
                    )

                # expected_statuses=set() opts into safe_request's
                # failed-response contract: without it a non-2xx returns
                # (None, err) and the 400 diagnosis below
                # (_is_filter_undefined_type_400 / _is_filter_type_mismatch_400)
                # could never inspect resp.status_code. The empty set keeps
                # every non-2xx in the warning log.
                resp, err = await safe_request(
                    client,
                    "post",
                    f"{_BASE_URL}/data_sources/{ds_id}/query{fp_qs}",
                    _PROVIDER,
                    "query database",
                    json=payload,
                    expected_statuses=set(),
                )
                # Stale ds_id: the data source was rotated after the initial
                # resolution.  Re-resolve from the parent database and retry
                # exactly once — mirrors the kb_sync stale-retry pattern.
                # Only attempt this when (a) the POST returned 404,
                # (b) ds_id differs from database_id (the input was a real
                # database_id whose data source rotated — when they are equal
                # the input was already a data_source_id and the
                # resolution-failed sentinel above already handled that path),
                # (c) we haven't already retried this iteration, and
                # (d) there is no active pagination cursor (a cursor from the
                # stale data source cannot be replayed against a different one).
                if (
                    err
                    and resp is not None
                    and resp.status_code == 404
                    and ds_id != database_id
                    and not stale_ds_refreshed
                    and not payload.get("start_cursor")
                ):
                    logger.info(
                        "query_notion_database: POST 404 for resolved "
                        "ds_id=%s (database_id=%s), re-resolving and "
                        "retrying once",
                        ds_id,
                        database_id,
                    )
                    stale_ds_refreshed = True
                    fresh_ds_id = await _resolve_data_source_id(client, database_id)
                    if fresh_ds_id == _RESOLUTION_FAILED_SENTINEL:
                        # The parent database itself is gone (deleted, moved,
                        # or unshared mid-run) — return the same actionable
                        # guidance as the pre-flight sentinel check instead
                        # of the raw POST 404.
                        return _resolution_failed_error(database_id)
                    if fresh_ds_id not in (
                        _PAGE_NOT_DATABASE_SENTINEL,
                        ds_id,
                    ):
                        ds_id = fresh_ds_id
                        # Re-issue the POST with the fresh ds_id.
                        resp, err = await safe_request(
                            client,
                            "post",
                            f"{_BASE_URL}/data_sources/{ds_id}/query{fp_qs}",
                            _PROVIDER,
                            "query database (stale retry)",
                            json=payload,
                            expected_statuses=set(),
                        )
                if err:
                    logger.warning(
                        "query_notion_database: error for %s: %s",
                        database_id,
                        err,
                    )
                    # On a missing-type 400 (``should be defined, instead was
                    # 'undefined'``), the pre-flight check should have caught
                    # this before the HTTP call, but guard defensively in case
                    # the payload was mutated or the check path was bypassed.
                    if _is_filter_undefined_type_400(resp) and isinstance(
                        payload.get("filter"), dict
                    ):
                        try:
                            notion_msg = resp.json().get("message", "")
                        except Exception:
                            notion_msg = ""
                        return (
                            f"Error: Notion rejected the filter because a condition "
                            f"is missing its type key — {notion_msg}. "
                            "Each filter condition must include a type key such as "
                            "'title', 'rich_text', 'number', 'checkbox', 'select', "
                            "'multi_select', 'date', or 'status' alongside the "
                            "'property' key. "
                            "Call get_notion_database first to see the property "
                            "types, then rebuild the filter, e.g.: "
                            '{"property": "NAME", "TYPE": {"contains": "value"}}.'
                        )
                    # On an invalid filter_properties value (400 with
                    # "filter_properties contains an invalid attribute"),
                    # give an actionable error so the agent removes or fixes
                    # the invalid property name instead of retrying blindly.
                    if _is_filter_properties_invalid_400(resp):
                        try:
                            notion_msg = resp.json().get("message", "")
                        except Exception:
                            notion_msg = ""
                        if not fp_resolved:
                            fp_resolved = True
                            schema_props = await _data_source_properties(client, ds_id)
                            if schema_props:
                                prop_list, unknown = _resolve_filter_properties(
                                    prop_list, schema_props
                                )
                                if unknown:
                                    fp_note = _unknown_filter_properties_note(
                                        unknown, schema_props
                                    )
                                logger.info(
                                    "query_notion_database: filter_properties for %s "
                                    "resolved to %d property id(s), %d unknown",
                                    database_id,
                                    len(prop_list),
                                    len(unknown),
                                )
                                continue
                        return (
                            f"Error: Notion rejected the query because "
                            f"'filter_properties' contains an invalid property "
                            f"name — {notion_msg}. "
                            "The 'filter_properties' parameter must only list "
                            "property names that exist in the database schema. "
                            "Call get_notion_database first to see all valid "
                            "property names, then retry without the invalid "
                            "filter_properties value."
                        )
                    # On a property-type mismatch (400), fetch the DB schema and
                    # return an actionable corrected-filter error so the agent can
                    # self-correct on its next attempt instead of retrying blindly.
                    if _is_filter_type_mismatch_400(resp) and isinstance(
                        payload.get("filter"), dict
                    ):
                        try:
                            notion_msg = resp.json().get("message", "")
                        except Exception:
                            notion_msg = ""
                        prop_name = _extract_filter_property_name(payload["filter"])
                        filter_type_key = _extract_filter_type_key(payload["filter"])
                        if not prop_name or not filter_type_key:
                            # Compound (``and``/``or``) filter — the schema can't
                            # pinpoint the offending sub-condition, so skip the
                            # extra fetch and return the generic-but-actionable
                            # hint directly.
                            return _build_filter_mismatch_hint(
                                payload["filter"], {}, notion_msg
                            )
                        # Fallback hint used whenever the schema is unavailable or
                        # unusable — still names the property and the wrong type
                        # key so the agent never falls back to blind retries.
                        fallback_hint = (
                            f"Error: Notion filter type mismatch — property "
                            f"'{prop_name}' does not have type '{filter_type_key}' "
                            f"in this database. {notion_msg}. "
                            "Call get_notion_database to see the correct property "
                            "type, then retry."
                        )
                        schema_resp, schema_err = await safe_request(
                            client,
                            "get",
                            f"{_BASE_URL}/data_sources/{ds_id}",
                            _PROVIDER,
                            "retrieve schema for filter diagnosis",
                        )
                        if schema_err or schema_resp is None:
                            # Schema fetch failed — still give a helpful hint with
                            # the property name and expected type key.
                            return fallback_hint
                        try:
                            schema_props = schema_resp.json().get("properties") or {}
                            enriched = _build_filter_mismatch_hint(
                                payload["filter"], schema_props, notion_msg
                            )
                            logger.info(
                                "query_notion_database: enriched type-mismatch "
                                "error for %s",
                                database_id,
                            )
                            return enriched
                        except Exception as exc:
                            # Malformed schema response — degrade to the fallback
                            # hint instead of the raw passthrough error so the
                            # guidance is never lost.
                            logger.debug(
                                "query_notion_database: failed to build mismatch hint "
                                "for %s: %r",
                                database_id,
                                exc,
                            )
                            return fallback_hint
                    return err
                data = resp.json()
                results = data.get("results", [])
                all_results.extend(results)
                logger.info(
                    "query_notion_database: fetched %d results (total=%d, has_more=%s)",
                    len(results),
                    len(all_results),
                    data.get("has_more"),
                )
                more_in_notion = bool(data.get("has_more") and data.get("next_cursor"))
                if not more_in_notion:
                    break
                payload["start_cursor"] = data["next_cursor"]

            if fmt == "json":
                rows = all_results[:count]
                return json.dumps(
                    {
                        "results": [_page_row_json(r) for r in rows if r.get("object") == "page"],
                        "has_more": more_in_notion or len(all_results) > count,
                        "note": fp_note or None,
                    },
                    default=str,
                )

            if not all_results:
                logger.info(
                    "query_notion_database: no entries found in %s", database_id
                )
                return "\n\n".join(
                    part for part in (fp_note, "No entries found in this database.") if part
                )

            trimmed = all_results[:count]
            result_count = len(trimmed)

            # ── Auto-expand: fetch page content to save follow-up iterations ──
            page_ids = [r["id"] for r in trimmed if r.get("object") == "page"]
            content_map: dict[str, str] = {}

            if page_ids and result_count <= _QUERY_FULL_CONTENT_THRESHOLD:
                # Few results → fetch full page content in parallel
                logger.info(
                    "query_notion_database: auto-expanding %d pages with full content",
                    len(page_ids),
                )

                async def _read_full(pid: str) -> tuple[str, str]:
                    # A row of the query result, not a reference the run addressed.
                    meta_resp, meta_err = await safe_request(
                        client,
                        "get",
                        f"{_BASE_URL}/pages/{pid}",
                        _PROVIDER,
                        "read page for expansion",
                        report_stale=False,
                    )
                    if meta_err:
                        return (pid, "")
                    meta = meta_resp.json()
                    blocks = await _fetch_all_blocks(client, pid)
                    parts: list[str] = []
                    for block in blocks:
                        depth = block.get("_depth", 0)
                        text = _block_to_text(block)
                        if text:
                            indent = "  " * depth
                            parts.append(f"{indent}{text}")
                    content = "\n".join(parts)
                    if len(content) > _READ_PAGE_MAX_CHARS:
                        content = content[:_READ_PAGE_MAX_CHARS] + "…"
                    # Also resolve relations for richer property display
                    parent = meta.get("parent", {})
                    if parent.get("type") in _DATABASE_ROW_PARENT_TYPES:
                        props = meta.get("properties", {})
                        resolved = await _resolve_relation_titles(client, props)
                        prop_detail = _summarize_properties(
                            props, resolved_relations=resolved
                        )
                        empty = _list_empty_properties(props)
                        extra = ""
                        if prop_detail and prop_detail != "No properties":
                            extra += f"\n  Properties (detailed): {prop_detail}"
                        if empty:
                            extra += f"\n  Unset properties: {empty}"
                        return (
                            pid,
                            extra + ("\n  Content:\n" + content if content else ""),
                        )
                    return (pid, "\n  Content:\n" + content if content else "")

                results_list = await asyncio.gather(
                    *[_read_full(pid) for pid in page_ids]
                )
                content_map = dict(results_list)
            elif page_ids and result_count <= _QUERY_PREVIEW_THRESHOLD:
                # Moderate results → fetch content previews in parallel
                logger.info(
                    "query_notion_database: auto-expanding %d pages with previews",
                    len(page_ids),
                )
                preview_tasks = {
                    pid: _fetch_page_preview(client, pid) for pid in page_ids
                }
                previews = dict(
                    zip(
                        preview_tasks.keys(),
                        await asyncio.gather(*preview_tasks.values()),
                        strict=True,
                    )
                )
                content_map = {
                    pid: f"\n  Preview: {p}" for pid, p in previews.items() if p
                }

            # ── Format output ──────────────────────────────────────────────
            lines = []
            for r in trimmed:
                title = _extract_title(r)
                props = _summarize_properties(r.get("properties", {}))
                url = r.get("url", "")
                entry = f"• {title}\n  ID: {r['id']}\n  URL: {url}\n  {props}"
                extra = content_map.get(r["id"], "")
                if extra:
                    entry += extra
                lines.append(entry)
            logger.info(
                "query_notion_database: returning %d entries for %s (auto-expand=%s)",
                result_count,
                database_id,
                (
                    "full"
                    if result_count <= _QUERY_FULL_CONTENT_THRESHOLD
                    else (
                        "preview"
                        if result_count <= _QUERY_PREVIEW_THRESHOLD
                        else "none"
                    )
                ),
            )
            return "\n\n".join([fp_note, *lines] if fp_note else lines)

    async def list_notion_databases(count: int = 20) -> str:
        """List all databases the integration has access to."""
        logger.info("list_notion_databases: count=%d", count)
        page_size = min(count, 100)
        # API 2025-09-03: filter value changed from "database" to "data_source"
        payload: dict = {
            "filter": {"value": "data_source", "property": "object"},
            "page_size": page_size,
            "sort": {"timestamp": "last_edited_time", "direction": "descending"},
        }
        all_results: list[dict] = []
        async with _client() as client:
            while len(all_results) < count:
                resp, err = await safe_request(
                    client,
                    "post",
                    f"{_BASE_URL}/search",
                    _PROVIDER,
                    "list databases",
                    json=payload,
                )
                if err:
                    logger.warning("list_notion_databases: error: %s", err)
                    return err
                data = resp.json()
                results = data.get("results", [])
                all_results.extend(results)
                logger.info(
                    "list_notion_databases: fetched %d results (total=%d, has_more=%s)",
                    len(results),
                    len(all_results),
                    data.get("has_more"),
                )
                if not data.get("has_more") or not data.get("next_cursor"):
                    break
                payload["start_cursor"] = data["next_cursor"]

        trimmed = all_results[:count]
        logger.info("list_notion_databases: returning %d databases", len(trimmed))

        def _fmt_db(r: dict) -> str:
            title = _extract_title(r)
            obj_type = r.get("object", "data_source")
            return f"• {title}\n  ID: {r['id']}\n  Type: {obj_type}\n  URL: {r.get('url', '')}"

        return format_entity_list(trimmed, _fmt_db, noun="database")

    async def get_notion_database(database_id: str) -> str:
        """Retrieve a database's schema (properties/columns). Useful for understanding the structure before querying.

        Note: The ``database_id`` can be either a database ID or a data_source ID.
        For API 2025-09-03, GET /v1/databases/{id} returns a list of data_sources;
        the actual schema is fetched from GET /v1/data_sources/{data_source_id}.
        """
        logger.info("retrieve_notion_database: database_id=%s", database_id)
        async with _client() as client:
            # Strategy: try as data_source first (1 call), then as database
            # with resolution (max 2 more calls). Total: max 3 HTTP calls.

            # 1. Try the data_sources endpoint directly (in case it's already
            #    a data_source_id). The common case is a real database_id, so
            #    this probe is EXPECTED to 404 — ``expected_statuses={404}``
            #    keeps the routine miss out of the warning stream (it showed
            #    up on every get_notion_database call in production logs)
            #    while any other failure still logs.
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/data_sources/{database_id}",
                _PROVIDER,
                "retrieve data source",
                expected_statuses={404},
            )
            if not err:
                logger.info(
                    "retrieve_notion_database: resolved as data_source %s", database_id
                )
                return _format_database_schema(resp.json(), is_data_source=True)

            # 2. Try the databases endpoint — also resolves data_source_id from the response
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/databases/{database_id}",
                _PROVIDER,
                "retrieve database",
                expected_statuses={400},
            )
            if err:
                # Notion returns 400 "is a page, not a database" when the ID
                # belongs to a page.  Return an actionable message immediately
                # instead of propagating the opaque 400.
                if _is_page_not_database_400(resp):
                    return (
                        f"Error: '{database_id}' is a Notion page, not a database. "
                        "Use get_notion_page to read this page's content instead of "
                        "get_notion_database."
                    )
                logger.warning(
                    "retrieve_notion_database: failed for %s: %s",
                    database_id,
                    err,
                )
                return err
            db = resp.json()

            # 3. In 2025-09-03, GET /databases/{id} returns data_sources list, not schema.
            #    Fetch the actual schema from the first data_source.
            data_sources = db.get("data_sources", [])
            if data_sources:
                first_ds_id = data_sources[0]["id"]
                logger.info(
                    "retrieve_notion_database: database %s has %d data_source(s), fetching schema from %s",
                    database_id,
                    len(data_sources),
                    first_ds_id,
                )
                ds_resp, ds_err = await safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/data_sources/{first_ds_id}",
                    _PROVIDER,
                    "retrieve data source schema",
                )
                if not ds_err:
                    return _format_database_schema(ds_resp.json(), is_data_source=True)
                logger.warning(
                    "retrieve_notion_database: failed to fetch data_source %s: %s",
                    first_ds_id,
                    ds_err,
                )

            # Fallback: format whatever the databases endpoint returned (legacy or partial)
            return _format_database_schema(db, is_data_source=False)

    async def list_notion_templates(
        database_id: str, name: str = "", count: int = 100
    ) -> str:
        """List available templates for a Notion database / data source.

        ``database_id`` can be a database ID or a data_source ID (auto-resolved).
        ``name`` (optional): filter templates by name substring (case-insensitive).
        ``count`` (optional): max results (default 100, max 100).

        Returns template IDs, names, and which one is the default.
        Use a template_id with ``create_notion_page`` to create pages pre-populated
        from a template.
        """
        logger.info(
            "list_notion_templates: database_id=%s name=%r count=%d",
            database_id,
            name,
            count,
        )
        async with _client() as client:
            ds_id = await _resolve_data_source_id(client, database_id)
            if not ds_id:
                return (
                    f"Error: Could not resolve data source for '{database_id}'. "
                    "Make sure the database is shared with the integration."
                )
            if ds_id == _PAGE_NOT_DATABASE_SENTINEL:
                return (
                    f"Error: '{database_id}' is a Notion page, not a database. "
                    "Templates are only available for databases."
                )
            if ds_id == _RESOLUTION_FAILED_SENTINEL:
                return _resolution_failed_error(database_id)

            params: dict[str, str] = {"page_size": str(min(count, 100))}
            if name:
                params["name"] = name

            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/data_sources/{ds_id}/templates",
                _PROVIDER,
                "list templates",
                params=params,
            )
            if err:
                logger.warning("list_notion_templates: error: %s", err)
                return err

            data = resp.json()
            templates = data.get("templates", [])
            if not templates:
                return "No templates found for this database."

            lines: list[str] = []
            for t in templates:
                default_tag = " ⭐ (default)" if t.get("is_default") else ""
                lines.append(
                    f"• {t.get('name', 'Untitled')}{default_tag}\n  ID: {t['id']}"
                )

            logger.info(
                "list_notion_templates: found %d template(s) for data_source %s",
                len(templates),
                ds_id,
            )
            return f"Templates ({len(templates)}):\n" + "\n".join(lines)

    async def create_notion_database(
        parent_page_id: str, title: str, properties_json: str
    ) -> str:
        """Create a new database under a parent page.

        properties_json: JSON object defining columns, e.g.:
        {"Name": {"title": {}}, "Status": {"select": {"options": [{"name": "To Do"}, {"name": "Done"}]}}}

        Relation columns may reference the target database by
        ``database_id`` — it is automatically resolved to the
        ``data_source_id`` required by API 2025-09-03.
        """
        logger.info(
            "create_notion_database: parent=%s title=%r",
            parent_page_id,
            title,
        )
        props, parse_err = _parse_json_arg(
            properties_json, what="properties_json", expected="object"
        )
        if parse_err:
            return parse_err
        if not isinstance(props, dict) or not props:
            return (
                "Error: properties_json must be a non-empty JSON object "
                "describing the database columns."
            )
        async with _client() as client:
            # API 2025-09-03: relation columns must reference the target
            # database's data source — rewrite legacy
            # {"relation": {"database_id": ...}} shapes before sending
            # (Notion rejects them with "relation.data_source_id should
            # be defined, instead was `undefined`").
            relation_err = await ctx.normalize_relation_properties(client, props)
            if relation_err:
                return relation_err
            # API 2025-09-03: properties go under initial_data_source
            body: dict = {
                "parent": {"type": "page_id", "page_id": parent_page_id},
                "title": [{"type": "text", "text": {"content": title}}],
                "initial_data_source": {"properties": props},
            }
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/databases",
                _PROVIDER,
                "create database",
                json=body,
            )
            # If initial_data_source format fails, fall back to legacy format
            if err:
                logger.info(
                    "create_notion_database: initial_data_source format failed, trying legacy format",
                )
                body = {
                    "parent": {"type": "page_id", "page_id": parent_page_id},
                    "title": [{"type": "text", "text": {"content": title}}],
                    "properties": props,
                }
                resp, err = await safe_request(
                    client,
                    "post",
                    f"{_BASE_URL}/databases",
                    _PROVIDER,
                    "create database (legacy)",
                    json=body,
                )
            if err:
                logger.warning("create_notion_database: error: %s", err)
                return err
            db = resp.json()
            logger.info(
                "create_notion_database: created database %s (%s)",
                db["id"],
                db.get("url", ""),
            )
            return f"Database created: {db.get('url', '')} (ID: {db['id']})"

    async def update_notion_database(
        database_id: str, title: str = "", properties_json: str = ""
    ) -> str:
        """Update a database title and/or properties (columns).

        properties_json: JSON object with property updates.
        To add a column: {"New Column": {"rich_text": {}}}
        To rename: {"Old Name": {"name": "New Name"}}

        Note: In API 2025-09-03, property updates go to the data_source endpoint,
        while database-level updates (title) go to the database endpoint.
        """
        logger.info(
            "update_notion_database: database_id=%s title=%r has_props=%s",
            database_id,
            title,
            bool(properties_json),
        )
        if not title and not properties_json:
            return "Error: Provide at least a title or properties_json to update."

        results: list[str] = []
        async with _client() as client:
            # Property updates go to the data_source endpoint (API 2025-09-03)
            if properties_json:
                props, parse_err = _parse_json_arg(
                    properties_json, what="properties_json", expected="object"
                )
                if parse_err:
                    return parse_err
                # Match the validation already present in
                # ``create_notion_database`` so both column-management
                # surfaces reject the same wrong shapes consistently
                # (empty dict / list / scalar). An empty dict would no-op
                # at Notion; a list or scalar would 400 with an opaque
                # error the LLM would then retry.
                if not isinstance(props, dict) or not props:
                    return (
                        "Error: properties_json must be a non-empty JSON "
                        "object describing the property updates, e.g. "
                        '{"New Column": {"rich_text": {}}}.'
                    )

                # Same relation-schema rewrite as create_notion_database:
                # adding/modifying a relation column via the legacy
                # {"relation": {"database_id": ...}} shape must resolve the
                # target's data_source_id first (API 2025-09-03).
                relation_err = await ctx.normalize_relation_properties(
                    client, props
                )
                if relation_err:
                    return relation_err

                ds_id = await _resolve_data_source_id(client, database_id)
                if ds_id == _PAGE_NOT_DATABASE_SENTINEL:
                    return (
                        f"Error: '{database_id}' is a Notion page, not a database. "
                        "Use update_notion_page to modify a page."
                    )
                if ds_id == _RESOLUTION_FAILED_SENTINEL:
                    return _resolution_failed_error(database_id)
                ds_payload: dict = {"properties": props}
                if title:
                    ds_payload["title"] = [{"type": "text", "text": {"content": title}}]

                _, err = await safe_request(
                    client,
                    "patch",
                    f"{_BASE_URL}/data_sources/{ds_id}",
                    _PROVIDER,
                    "update database properties",
                    json=ds_payload,
                )
                # Fall back to the legacy /databases/{id} endpoint when the
                # modern data_sources PATCH fails. ``resolve_data_source_id``
                # returns a DIFFERENT id for the common case (the input is a
                # real database_id), so the previous ``ds_id == database_id``
                # guard skipped the fallback exactly when it was most useful
                # and only fired when the input was already a data_source_id
                # (where the legacy database endpoint cannot help). Retry on
                # any error against the original ``database_id``; keep the
                # original (modern) error if the legacy attempt also fails - it
                # is the more relevant diagnostic for the canonical endpoint.
                if err:
                    logger.info(
                        "update_notion_database: data_sources endpoint failed "
                        "(%s), trying legacy /databases endpoint for %s",
                        err,
                        database_id,
                    )
                    _, legacy_err = await safe_request(
                        client,
                        "patch",
                        f"{_BASE_URL}/databases/{database_id}",
                        _PROVIDER,
                        "update database properties (legacy)",
                        json=ds_payload,
                    )
                    if not legacy_err:
                        err = None
                if err:
                    logger.warning("update_notion_database: error: %s", err)
                    return err
                results.append("properties updated")

            elif title:
                # Title-only update goes to the database endpoint
                payload: dict = {
                    "title": [{"type": "text", "text": {"content": title}}]
                }
                _, err = await safe_request(
                    client,
                    "patch",
                    f"{_BASE_URL}/databases/{database_id}",
                    _PROVIDER,
                    "update database title",
                    json=payload,
                )
                if err:
                    logger.warning("update_notion_database: error: %s", err)
                    return err
                results.append("title updated")

        logger.info(
            "update_notion_database: %s for %s",
            ", ".join(results),
            database_id,
        )
        return f"Database {database_id} updated ({', '.join(results)})."

    executor.register("query_notion_database", query_notion_database)
    executor.register("list_notion_databases", list_notion_databases)
    executor.register("get_notion_database", get_notion_database)
    executor.register("list_notion_templates", list_notion_templates)
    executor.register("create_notion_database", create_notion_database)
    executor.register("update_notion_database", update_notion_database)

    return [
        # ── Databases ──────────────────────────────────────────────────
        ToolDefinition(
            name="query_notion_database",
            priority=0,
            description=(
                "Query entries in a Notion database. Optionally filter and sort results. "
                "Use get_notion_database first to understand the schema and property types. "
                "Accepts either a database_id or data_source_id (auto-resolved). "
                'Filter example: {"property": "Status", "select": {"equals": "Done"}}. '
                'Sorts example: [{"property": "Created", "direction": "descending"}]. '
                "Use filter_properties to only return specific columns (faster for large schemas). "
                "**Smart content expansion:** When ≤5 results are returned, full page content "
                "is automatically included (no need to call get_notion_page). When 6–20 results, "
                "content previews (~500 chars) are included. This means you usually do NOT need "
                "a follow-up get_notion_page call after querying a database."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "database_id": {
                        "type": "string",
                        "description": "Notion database ID or data_source ID",
                    },
                    "filter_json": {
                        "type": "string",
                        "description": "Notion filter object as JSON (optional)",
                        "default": "",
                    },
                    "sorts_json": {
                        "type": "string",
                        "description": "Notion sorts array as JSON (optional)",
                        "default": "",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                    "filter_properties": {
                        "type": "string",
                        "description": "Comma-separated property names/IDs to include in results (optional, improves speed)",
                        "default": "",
                    },
                    "output_format": {
                        "type": "string",
                        "enum": list(_QUERY_OUTPUT_FORMATS),
                        "description": (
                            "'text' (default) for reading; 'json' returns the rows as data - "
                            "{results: [{id, url, created_time, last_edited_time, in_trash, "
                            "properties: {name: value}, truncated: [names cut at 25 references]}], "
                            "has_more} - with full date ranges, people as {id, name}, related "
                            "pages by id, files by name, and no page content."
                        ),
                        "default": "text",
                    },
                },
                "required": ["database_id"],
            },
            source="integration:notion",
        ),
        ToolDefinition(
            name="list_notion_databases",
            description="List all Notion databases the integration has access to, with titles, IDs, and URLs.",
            priority=2,
            input_schema={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                },
                "required": [],
            },
            source="integration:notion",
        ),
        ToolDefinition(
            name="get_notion_database",
            priority=2,
            description=(
                "Retrieve a database's schema — its properties (columns), their types, and available options "
                "(select/multi-select/status values, relation targets). Call this BEFORE querying a database "
                "to understand its structure and build correct filters. "
                "Accepts either a database_id or data_source_id (auto-resolved)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "database_id": {
                        "type": "string",
                        "description": "Notion database ID or data_source ID",
                    },
                },
                "required": ["database_id"],
            },
            source="integration:notion",
        ),
        ToolDefinition(
            name="list_notion_templates",
            description=(
                "List available templates for a Notion database / data source. "
                "Returns template IDs, names, and which one is the default. "
                "Use a template_id with create_notion_page or update_notion_page "
                "to create or update pages pre-populated from a template."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "database_id": {
                        "type": "string",
                        "description": "Notion database ID or data_source ID (auto-resolved)",
                    },
                    "name": {
                        "type": "string",
                        "description": "Filter templates by name substring (case-insensitive, optional)",
                        "default": "",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max results (default 100, max 100)",
                        "default": 100,
                    },
                },
                "required": ["database_id"],
            },
            source="integration:notion",
            priority=1,
        ),
        ToolDefinition(
            name="create_notion_database",
            description=(
                "Create a new database under a parent page. Define columns via properties_json. "
                'Example: {"Name": {"title": {}}, "Status": {"select": {"options": [{"name": "To Do"}, {"name": "Done"}]}}}. '
                'Relation columns: {"Related": {"relation": {"database_id": "TARGET_DB_ID"}}} '
                "(the database_id is auto-resolved to the data_source_id required by the Notion API)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "parent_page_id": {
                        "type": "string",
                        "description": "Parent page ID where the database will be created",
                    },
                    "title": {"type": "string", "description": "Database title"},
                    "properties_json": {
                        "type": "string",
                        "description": "Database properties (columns) as JSON",
                    },
                },
                "required": ["parent_page_id", "title", "properties_json"],
            },
            source="integration:notion",
            priority=1,
        ),
        ToolDefinition(
            name="update_notion_database",
            description=(
                "Update a database's title and/or properties (add/rename/modify columns). "
                'To add a column: {"New Col": {"rich_text": {}}}. To rename: {"Old Name": {"name": "New Name"}}. '
                'Relation columns: {"Related": {"relation": {"database_id": "TARGET_DB_ID"}}} '
                "(auto-resolved to the required data_source_id)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "database_id": {
                        "type": "string",
                        "description": "Notion database ID or data_source ID (auto-resolved)",
                    },
                    "title": {
                        "type": "string",
                        "description": "New title (optional)",
                        "default": "",
                    },
                    "properties_json": {
                        "type": "string",
                        "description": "Properties to update/add as JSON (optional)",
                        "default": "",
                    },
                },
                "required": ["database_id"],
            },
            source="integration:notion",
            priority=1,
        ),
    ]
