# SPDX-License-Identifier: Apache-2.0

"""Metabase integration — Business Intelligence & Analytics.

API docs: https://www.metabase.com/docs/latest/api
Auth: API key (X-Api-Key header) or session token (X-Metabase-Session)
Base URL: configured per instance (e.g. https://metabase.company.com/api)

Two query languages are supported, matching Metabase's GUI:

* **Native (SQL)** — raw SQL with optional ``{{variable}}`` template tags.
  Used via :func:`run_metabase_query` and ``query_type='native'`` on
  ``create_metabase_question``. Optional clauses use ``[[...{{var}}...]]``
  syntax which is *only* expanded when the template tag is properly
  declared — otherwise the literal ``[[…]]`` is sent to the database and
  produces ``syntax error at or near "["``.
* **MBQL (structured / "GUI builder" questions)** — JSON queries built
  from a source table/card/metric plus filters/aggregations/breakouts.
  Used via :func:`run_metabase_mbql_query` and ``query_type='query'`` on
  ``create_metabase_question`` (pass ``mbql_query`` for full support).
  Inspired by the official Metabase MCP server's ``construct_query`` DSL
  (``src/metabase/agent_api/reference.md``).

Tool categories:
    1. Schema Introspection — database metadata, schemas, tables, fields
    2. Questions / Cards — CRUD + run saved questions (native SQL **and** MBQL)
    3. Dashboards — CRUD + card management
    4. Collections — CRUD + item browsing
    5. Data Querying — native SQL **and** MBQL execution + dry-run validation
    6. Metrics & Segments — reusable aggregations and filters (MBQL building blocks)
    7. Alerts — list + create
    8. Search & Discovery — full-text search, database listing
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.egress import egress_client
from app.integrations.file_delivery import deliver_file, fetch_file, file_content_type
from app.integrations.http import (
    format_entity_list,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)
_PROVIDER = "Metabase"
_MAX_ROWS = 100
_TIMEOUT_READ = 30
_TIMEOUT_QUERY = 60
_TIMEOUT_DASHBOARD = 120
_TIMEOUT_EXPORT = 180
#: ``/api/card/:id/query/:export-format`` formats that are a file.
_EXPORT_FORMATS = ("csv", "xlsx", "json")
#: Largest question export stored as a file.
_MAX_EXPORT_BYTES = 50 * 1024 * 1024

# Matches `{{var}}` and `{{ var }}` (Metabase template-tag placeholders) but not
# `{{{json}}}` and similar — the inner group is the tag name.
_TEMPLATE_TAG_RE = re.compile(r"\{\{\s*([A-Za-z0-9_\-]+)\s*\}\}")

# Matches every Postgres column-not-found wording that Metabase forwards:
#   column "foo" does not exist                    (bare unknown column)
#   column t.foo does not exist                    (alias-qualified — NOT quoted)
#   column events.foo does not exist               (table-qualified — NOT quoted)
#   column "foo" of relation "bar" does not exist  (INSERT/UPDATE target)
#   column __mb_source.Narr Target does not exist  (qualified name with a space)
# The qualified forms are unquoted in Postgres output, so the pattern accepts
# a double-quoted identifier, a dotted bare identifier chain, or a qualifier
# followed by a column name holding any character but a newline.
_COLUMN_NOT_FOUND_RE = re.compile(
    r'column\s+("[^"]+"|[\w$]+(?:\.[\w$]+){0,2}|(?:[\w$]+\.){1,2}[^\n]{1,256}?)\s+'
    r'(?:of\s+relation\s+"[^"]+"\s+)?does\s+not\s+exist',
    re.IGNORECASE,
)

# The alias Metabase gives the source stage of an MBQL query built over a saved
# question or a nested query: ``SELECT "__mb_source"."COL" FROM (...) AS
# "__mb_source"`` (``source`` in older Metabase versions). A missing column
# qualified by it is a column of the query's source, not of a database table.
_SOURCE_STAGE_ALIASES = frozenset({"__mb_source", "source"})

_CARD_SOURCE_RE = re.compile(r"card__(\d+)")

# Caps for the schema hint appended to column-not-found errors, keeping the
# enriched error message bounded even on very large / very wide databases.
_SCHEMA_HINT_MAX_TABLES = 30
_SCHEMA_HINT_MAX_COLUMNS_PER_TABLE = 50

# Fallback guidance when the schema-hint metadata fetch itself fails.
_SCHEMA_HINT_FALLBACK = (
    " To fix: call get_metabase_database_metadata or "
    "get_metabase_table_metadata to inspect the real column names."
)


def _extract_optional_clauses(sql: str) -> list[str]:
    """Return the bodies of every top-level ``[[…]]`` block in *sql*.

    Uses a stack-based scanner so nested ``[[…[[…]]…]]`` works correctly
    (Metabase does accept nesting in practice). Each returned body is the
    *full* substring between matched ``[[`` / ``]]`` pairs — including any
    inner nested blocks — so the downstream "no `{{var}}` inside" check sees
    template-tag references at any depth.
    """
    bodies: list[str] = []
    stack: list[int] = []
    i = 0
    n = len(sql)
    while i < n - 1:
        pair = sql[i : i + 2]
        if pair == "[[":
            stack.append(i + 2)
            i += 2
            continue
        if pair == "]]" and stack:
            start = stack.pop()
            if not stack:
                # Only emit top-level blocks; nested ones are included in the
                # outer body, so the template-tag scan still sees them.
                bodies.append(sql[start : i])
            i += 2
            continue
        i += 1
    return bodies


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _explain_sql_bracket_error(error_msg: str) -> str:
    """When Postgres / a DB reports ``syntax error at or near "["``, append a
    hint pointing the LLM at the most common cause: an unclosed Metabase
    optional-clause ``[[...{{var}}...]]`` whose template-tag is missing or
    misconfigured.

    Metabase only strips ``[[…]]`` when the contained ``{{var}}`` resolves to a
    declared template-tag (and that tag has either a value or is optional).
    Otherwise the literal brackets are forwarded to the database — which then
    blows up with the same Postgres error we see in production. Surfacing this
    actively prevents the LLM from blindly retrying the same broken question.
    """
    msg = error_msg.lower()
    if 'at or near "["' not in msg and "at or near '['" not in msg:
        return error_msg
    hint = (
        " Hint: this is almost always a Metabase optional-clause / template-tag "
        "mismatch. The question SQL contains `[[ ... {{var}} ... ]]` but the "
        "template-tag for `var` is missing, misnamed, or not declared as "
        "type='dimension'/'text'/'number'/'date'. Metabase only strips the "
        "`[[ ]]` brackets when the inner `{{var}}` resolves to a declared tag. "
        "Fix: (1) use `read_metabase_question_raw` to inspect the SQL and "
        "template-tags; (2) for every `{{var}}` in the SQL, ensure a matching "
        "entry exists in `template-tags` with the right `type` / `name` / `id` / "
        "`dimension` / `widget-type`; (3) re-save via `update_metabase_question` "
        "with the corrected `template_tags`. Prefer MBQL via "
        "`run_metabase_mbql_query` / `create_metabase_question(query_type='query', mbql_query=...)` "
        "when you do not need raw SQL — MBQL avoids this whole class of bug."
    )
    return error_msg + hint


def _is_column_not_found_error(error_msg: str) -> bool:
    """Return True when *error_msg* contains a Postgres 'column "X" does not exist' clause.

    This pattern is produced when the agent hallucinates a column name that is
    not in the underlying schema.  Callers use this to trigger a schema-hint
    fetch so the agent can ground its next query in the real column names.
    """
    return bool(_COLUMN_NOT_FOUND_RE.search(error_msg))


def _missing_column(error_msg: str) -> tuple[str, str] | None:
    """The ``(qualifier, column)`` a column-not-found error names, or ``None``.

    The qualifier is ``""`` for a bare (quoted) column. A qualified name is
    split at the last dot before its first space, so ``__mb_source.Narr
    Target`` reads as ``("__mb_source", "Narr Target")`` and
    ``public.events.user_idd`` as ``("public.events", "user_idd")``.
    """
    match = _COLUMN_NOT_FOUND_RE.search(error_msg)
    if not match:
        return None
    ref = match.group(1)
    if ref.startswith('"'):
        return "", ref[1:-1]
    qualifier, dot, _ = ref.split(" ", 1)[0].rpartition(".")
    if not dot:
        return "", ref
    return qualifier, ref[len(qualifier) + 1 :]


def _card_source_id(mbql: dict[str, Any] | None) -> int | None:
    """The saved question an MBQL stage reads from (``"card__N"``), if any."""
    source = (mbql or {}).get("source-table")
    if not isinstance(source, str):
        return None
    match = _CARD_SOURCE_RE.fullmatch(source.strip())
    return int(match.group(1)) if match else None


def _card_field_ref(column: dict[str, Any]) -> str:
    """The field ref an MBQL stage over a saved question uses for *column*."""
    return json.dumps(
        ["field", column.get("name"), {"base-type": column.get("base_type") or "type/*"}]
    )


def _format_card_columns(columns: list[dict[str, Any]], limit: int | None = None) -> str:
    """One ``display name -> field ref`` line per result column of a saved question."""
    named = [c for c in columns if isinstance(c, dict) and c.get("name")]
    shown = named if limit is None else named[:limit]
    lines = [
        f"  • {c.get('display_name') or c['name']} → {_card_field_ref(c)}"
        for c in shown
    ]
    if len(named) > len(shown):
        lines.append(f"  … (+{len(named) - len(shown)} more)")
    return "\n".join(lines) if lines else "  (no columns)"


def _source_column_hint(
    column: str,
    card_id: int | None,
    card_name: str = "",
    card_columns: list[dict[str, Any]] | None = None,
) -> str:
    """The hint for a missing column of an MBQL query's source stage.

    Over a saved question or a nested query a field is referenced by the
    column's NAME in that source's results; the display name the query
    results are labelled with is not a column. With the saved question's
    result columns at hand, the hint lists each with the exact ref to use and
    names the column whose display name the agent wrote, if any.
    """
    rule = (
        " Over a saved question or a nested query, reference a column by its "
        'name in that source\'s results, not its display name: ["field", '
        '"COLUMN_NAME", {"base-type": "BASE_TYPE"}].'
    )
    if card_id is None:
        return f' Hint: "{column}" is not a column of this query\'s source stage.' + rule
    if not card_columns:
        return (
            f' Hint: "{column}" is not a column of saved question {card_id}.'
            + rule
            + f" Call get_metabase_table_metadata(card_id={card_id}) to list its columns."
        )
    title = f' "{card_name}"' if card_name else ""
    wanted = column.casefold()
    matches = [
        c
        for c in card_columns
        if isinstance(c, dict)
        and c.get("name")
        and str(c.get("display_name") or "").casefold() == wanted
    ]
    match_note = ""
    if matches:
        match_note = (
            f' "{column}" is the display name of column "{matches[0]["name"]}": '
            f"use {_card_field_ref(matches[0])}."
        )
    return (
        f' Hint: "{column}" is not a column of saved question {card_id}{title}.'
        + match_note
        + rule
        + f" Columns of saved question {card_id} (display name → field ref):\n"
        + _format_card_columns(card_columns, _SCHEMA_HINT_MAX_COLUMNS_PER_TABLE)
    )


def _check_dataset_failed(data: Any) -> str | None:
    """Inspect a ``/api/dataset`` or ``/api/card/{id}/query`` response body for
    an embedded failure.

    Metabase streams query results: on the wire we get HTTP 202 (or 200) even
    when the underlying query errored. The body then carries
    ``{"status": "failed", "error": "..."}`` and we must surface that — the
    current code path just returned "no results" which masked real bugs in
    upstream questions.
    """
    if not isinstance(data, dict):
        return None
    status = data.get("status")
    if status and status != "completed":
        err = (
            data.get("error")
            or data.get("error_type")
            or data.get("message")
            or "Query failed"
        )
        return f"Error: Metabase query did not complete (status={status}). {err}"
    return None


def _format_query_results(data: dict, max_rows: int = _MAX_ROWS) -> str:
    """Extract columns and rows from a Metabase query response."""
    failure = _check_dataset_failed(data)
    if failure:
        return _explain_sql_bracket_error(failure)
    cols = [
        c.get("display_name", c.get("name", "?"))
        for c in data.get("data", {}).get("cols", [])
    ]
    rows = data.get("data", {}).get("rows", [])
    if not rows:
        return "Query returned no results."
    lines = [" | ".join(cols)]
    lines.append("-" * len(lines[0]))
    for row in rows[:max_rows]:
        lines.append(" | ".join(str(v) if v is not None else "" for v in row))
    truncated = f" (showing {max_rows} of {len(rows)})" if len(rows) > max_rows else ""
    row_count = data.get("row_count", len(rows))
    return f"Results ({row_count} rows{truncated}):\n\n" + "\n".join(lines)


def _parse_result_time(value: Any) -> datetime | None:
    """An ISO-8601 time from a query result as an aware datetime, or ``None``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _format_age(age: timedelta) -> str:
    minutes = max(int(age.total_seconds()), 0) // 60
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        value, unit = minutes, "minute"
    elif minutes < 48 * 60:
        value, unit = minutes // 60, "hour"
    else:
        value, unit = minutes // (24 * 60), "day"
    return f"{value} {unit}{'' if value == 1 else 's'}"


def _result_freshness(data: Any, ignore_cache: bool = False) -> str:
    """When Metabase computed a saved question's result and whether its result
    cache served it, as one line the agent can quote.

    ``cached`` is the time the stored result was computed, ``null`` when this
    run computed it; older Metabase versions answer a boolean, some with the
    time in ``updated_at``. ``started_at`` is when this run began on
    Metabase's own clock, so the age does not depend on ours.
    """
    body = data if isinstance(data, dict) else {}
    cached = body.get("cached")
    ran_at = _parse_result_time(body.get("started_at")) or datetime.now(UTC)
    if not cached:
        bypassed = " (cache bypassed)" if ignore_cache else ""
        return f"Freshness: computed {ran_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC, not cached{bypassed}."
    rerun = " Run again with ignore_cache=true for the current value."
    computed_at = _parse_result_time(cached) or _parse_result_time(body.get("updated_at"))
    if computed_at is None:
        return "Freshness: cached, computed at a time this Metabase version does not report." + rerun
    return (
        f"Freshness: computed {computed_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC, cached "
        f"({_format_age(ran_at - computed_at)} old)." + rerun
    )


def _parse_json_arg(raw: str, field_name: str) -> tuple[Any, str | None]:
    """Parse a JSON-encoded string argument coming from a tool call.

    Returns ``(value, None)`` on success, or ``(None, error_string)`` ready to
    be returned directly to the LLM on failure. Used everywhere we accept JSON
    blobs (MBQL queries, template tags, visualization settings) so the parsing
    + error wording stays consistent across tools.

    .. important::
       Returns ``(None, None)`` when *raw* is the empty string. Callers MUST
       treat this ``None`` as "argument not provided" — distinct from a parsed
       JSON ``null`` (which also decodes to Python ``None`` but only when *raw*
       is the string ``"null"``). The standard call site pattern is::

           parsed, err = _parse_json_arg(raw, "field")
           if err:
               return err
           if parsed is not None:   # field WAS provided (and parsed cleanly)
               ...

       If you ever need to distinguish "not provided" from a literal JSON
       ``null``, switch to a sentinel object (``_MISSING = object()``) — but
       no current call site requires that.
    """
    if not raw:
        return None, None
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as exc:
        return None, (
            f"Invalid JSON in {field_name}: {exc.msg} at line {exc.lineno} col "
            f"{exc.colno}. Got: {raw[:200]}{'...' if len(raw) > 200 else ''}"
        )


def _normalise_mbql(parsed_mbql: Any) -> dict[str, Any] | None:
    """Return a *fresh* dict representing the inner MBQL query stage.

    Accepts either the raw query stage (``{"source-table": ...}``) or the
    full dataset_query envelope (``{"type": "query", "query": {...}}``) — the
    latter is unwrapped. Always returns a shallow copy so subsequent
    ``query["source-table"] = ...`` mutations cannot leak back to the
    caller's dict.

    Returns ``None`` if *parsed_mbql* is not a dict at all.
    """
    if not isinstance(parsed_mbql, dict):
        return None
    if (
        "type" in parsed_mbql
        and "query" in parsed_mbql
        and isinstance(parsed_mbql.get("query"), dict)
    ):
        return {**parsed_mbql["query"]}
    return {**parsed_mbql}


def _validate_template_tag_coverage(
    sql: str, template_tags: dict[str, Any] | None
) -> str | None:
    """Pre-flight check for the ``syntax error at or near "["`` family of bugs.

    Returns a human-readable diagnostic when the SQL references template tags
    that are not declared (or vice-versa), so the LLM gets a clear actionable
    error *before* we POST a broken question to Metabase that would later fail
    at query time. Returns ``None`` when the SQL is consistent with the tag
    set.
    """
    if not sql:
        return None
    referenced = {m.group(1) for m in _TEMPLATE_TAG_RE.finditer(sql)}
    # Stack-based extraction handles nested `[[…[[…]]…]]` correctly — Metabase
    # accepts nesting in practice and our previous regex silently missed it.
    optional_bodies = _extract_optional_clauses(sql)
    optional_tags: set[str] = set()
    for body in optional_bodies:
        optional_tags.update(_TEMPLATE_TAG_RE.findall(body))

    declared = set((template_tags or {}).keys())
    missing = sorted(referenced - declared)
    unused = sorted(declared - referenced)
    bare_optional = [
        body for body in optional_bodies if not _TEMPLATE_TAG_RE.search(body)
    ]

    # Only `missing` and `bare_optional` actually cause the
    # `syntax error at or near "["` family at query time — they are
    # hard-blocking. Declared-but-unused tags are harmless from Metabase's
    # perspective (the substitution engine ignores them) and the legitimate
    # workflow of "add a dimension tag BEFORE wiring its `[[AND {{var}}]]`
    # clause into the SQL" must not be rejected. We log unused tags as a
    # warning so they are still discoverable, but do not block the save.
    blocking: list[str] = []
    if missing:
        blocking.append(
            f"SQL references template tags that are NOT declared in `template_tags`: "
            f"{missing}. Either declare them (with `type`, `name`, `id`, and "
            f"`dimension`/`widget-type` for field filters) or remove the `{{{{...}}}}` "
            f"placeholders from the SQL."
        )
    if bare_optional:
        blocking.append(
            f"Found {len(bare_optional)} `[[…]]` optional clause(s) with NO `{{{{var}}}}` "
            f"inside: {bare_optional[:3]}{'...' if len(bare_optional) > 3 else ''}. "
            f"Metabase only strips `[[ ]]` when it contains a template-tag reference, "
            f"otherwise the brackets leak to the database and trigger "
            f'`syntax error at or near "["`. Remove the brackets or add a `{{{{var}}}}` '
            f"inside."
        )
    if unused:
        logger.info(
            "Metabase template-tag validation: declared tags not referenced "
            "by the SQL (harmless): %s",
            unused,
        )
    if not blocking:
        return None
    return (
        "Template-tag validation failed:\n  • "
        + "\n  • ".join(blocking)
        + "\n\nThis is the exact mismatch that produces "
        '`syntax error at or near "["` at query time. Fix before saving.'
    )


def _format_fields(fields: list[dict]) -> str:
    """Format table fields into a concise schema description.

    Includes field ID and base_type — both are required for constructing
    template-tag dimension references in native SQL field filters.
    """
    lines: list[str] = []
    for f in fields:
        field_id = f.get("id", "?")
        name = f.get("name", "?")
        display = f.get("display_name", name)
        db_type = f.get("database_type", "?")
        base_type = f.get("base_type", "?")
        semantic = f.get("semantic_type") or ""
        fk = ""
        if f.get("fk_target_field_id"):
            fk_table = (
                f.get("target", {}).get("table", {}).get("name", "?")
                if f.get("target")
                else "?"
            )
            fk = f" → FK({fk_table})"
        sem_str = f" [{semantic}]" if semantic else ""
        label = f"  • ID:{field_id} {display} ({name}) — {db_type} (base: {base_type}){sem_str}{fk}"
        lines.append(label)
    return "\n".join(lines) if lines else "  (no fields)"


def _serialize_dashcard(dc: dict[str, Any]) -> dict[str, Any]:
    """Serialize an existing dashcard for the ``PUT /api/dashboard/:id/cards`` payload.

    Metabase requires the full card list on every PUT and validates that on a
    tabbed dashboard *every* card carries a ``dashboard_tab_id``
    (see ``metabase/dashboards/api.clj`` ``update-dashboard``). We also forward
    ``series``/``visualization_settings``/``parameter_mappings`` so updates do
    not silently strip them.
    """
    return {
        "id": dc.get("id"),
        "card_id": dc.get("card_id") or dc.get("card", {}).get("id"),
        "row": dc.get("row", 0),
        "col": dc.get("col", 0),
        "size_x": dc.get("size_x", 6),
        "size_y": dc.get("size_y", 4),
        "dashboard_tab_id": dc.get("dashboard_tab_id"),
        "series": dc.get("series", []) or [],
        "parameter_mappings": dc.get("parameter_mappings", []) or [],
        "visualization_settings": dc.get("visualization_settings", {}) or {},
    }


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    """Build Metabase tools.

    The token format is: ``base_url||api_key`` (pipe-separated).
    """
    base_url, api_key = parse_compound_token(token, 2)
    if not base_url:
        logger.warning(
            "Metabase token missing base URL (expected 'base_url||api_key'). "
            "Cannot build tools without a base URL."
        )
        return []
    base_url = base_url.rstrip("/")
    headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
    persist_file_fn = _kwargs.get("persist_file_fn")

    # ── 1. Schema Introspection ────────────────────────────────────────────

    async def get_metabase_database_metadata(database_id: int) -> str:
        """Get full metadata for a database: all tables and their fields."""
        async with egress_client(timeout=_TIMEOUT_DASHBOARD) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/api/database/{database_id}/metadata",
                _PROVIDER,
                "get database metadata",
                headers=headers,
                params={"include_hidden": "false"},
            )
            if err:
                return err
            data = resp.json()
            lines = [
                f"Database: {data.get('name', '?')} (ID: {data.get('id', '')}, engine: {data.get('engine', '?')})",
                "",
            ]
            for table in data.get("tables", []):
                lines.append(
                    f"Table: {table.get('name', '?')} (ID: {table.get('id', '')}, schema: {table.get('schema', 'public')})"
                )
                lines.append(_format_fields(table.get("fields", [])))
                lines.append("")
            return (
                "\n".join(lines)
                if len(lines) > 2
                else "No tables found in this database."
            )

    async def list_metabase_database_schemas(database_id: int) -> str:
        """List schema names in a database."""
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/api/database/{database_id}/schemas",
                _PROVIDER,
                "list schemas",
                headers=headers,
            )
            if err:
                return err
            schemas = resp.json()
            if not schemas:
                return "No schemas found in this database."
            return f"Schemas ({len(schemas)}):\n" + "\n".join(
                f"  • {s}" for s in schemas
            )

    async def list_metabase_schema_tables(database_id: int, schema: str = "") -> str:
        """List tables in a specific schema of a database."""
        url = (
            f"{base_url}/api/database/{database_id}/schema/{schema}"
            if schema
            else f"{base_url}/api/database/{database_id}/schema"
        )
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await safe_request(
                client,
                "get",
                url,
                _PROVIDER,
                "list schema tables",
                headers=headers,
            )
            if err:
                return err
            tables = resp.json()
            return format_entity_list(
                tables,
                lambda t: f"{t.get('name', '?')} | ID: {t.get('id', '')} | Schema: {t.get('schema', 'public')} | Rows: {t.get('rows', '?')}",
                noun="table",
                empty_msg="No tables found in this schema.",
            )

    # Saved-question result columns, keyed by card id, for the lifetime of this
    # tool set (one agentic loop) like ``_schema_hint_cache`` below. Failed
    # reads are not cached so a transient error can recover on retry.
    _card_columns_cache: dict[int, tuple[str, int | None, list[dict[str, Any]]]] = {}

    async def _load_card_columns(
        card_id: int,
    ) -> tuple[tuple[str, int | None, list[dict[str, Any]]] | None, str | None]:
        """``((name, database id, result columns), None)`` of a saved question, or ``(None, error)``."""
        cached = _card_columns_cache.get(card_id)
        if cached is not None:
            return cached, None
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/api/card/{card_id}",
                _PROVIDER,
                "read question columns",
                headers=headers,
            )
        if err:
            return None, err
        card = resp.json()
        if not isinstance(card, dict):
            return None, f"Error: Metabase returned no question for ID {card_id}."
        columns = card.get("result_metadata") or []
        if not isinstance(columns, list):
            columns = []
        query = card.get("dataset_query")
        database_id = query.get("database") if isinstance(query, dict) else None
        loaded = (str(card.get("name") or ""), database_id, columns)
        _card_columns_cache[card_id] = loaded
        return loaded, None

    async def get_metabase_table_metadata(table_id: int = 0, card_id: int = 0) -> str:
        """Get detailed metadata for a table (field names, types, FKs, semantic
        types), or the result columns of a saved question with ``card_id``."""
        if card_id:
            loaded, err = await _load_card_columns(card_id)
            if err:
                return err
            name, database_id, columns = loaded
            return "\n".join([
                f"Saved question: {name or 'Untitled'} (ID: {card_id}) — "
                f'use as "source-table": "card__{card_id}"',
                f"Database ID: {database_id if database_id is not None else '?'}",
                "",
                "Columns (display name → field ref; a column is referenced by its "
                "name, never its display name):",
                _format_card_columns(columns),
            ])
        if not table_id:
            return (
                "Pass table_id for a database table, or card_id for a saved "
                "question used as a card__ID source."
            )
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/api/table/{table_id}/query_metadata",
                _PROVIDER,
                "get table metadata",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            lines = [
                f"Table: {data.get('name', '?')} (ID: {data.get('id', '')})",
                f"Schema: {data.get('schema', 'public')}",
                f"Database: {data.get('db', {}).get('name', '?')} (ID: {data.get('db_id', '')})",
                f"Description: {data.get('description') or 'N/A'}",
                f"Rows: {data.get('rows', '?')}",
                "",
                "Fields:",
                _format_fields(data.get("fields", [])),
            ]
            return "\n".join(lines)

    # Successful schema hints, keyed by database_id. The cache lives for the
    # lifetime of this tool set (one agentic loop), so an agent that retries a
    # corrected query and fails on a *different* phantom column does not
    # re-fetch the full database metadata on every attempt. Failed fetches are
    # intentionally NOT cached so a transient error can recover on retry.
    _schema_hint_cache: dict[int, str] = {}

    async def _fetch_schema_hint_for_column_error(database_id: int) -> str:
        """Return a compact column-list suffix to append to a column-not-found error.

        Calls ``/api/database/{id}/metadata`` and formats
        ``schema.table: col1, col2, ...`` lines (capped at
        :data:`_SCHEMA_HINT_MAX_TABLES` tables and
        :data:`_SCHEMA_HINT_MAX_COLUMNS_PER_TABLE` columns per table) so the
        agent can pick the real column name on its next attempt instead of
        hallucinating again. This only lists table/column *names* the
        integration's API key can already read via
        ``get_metabase_database_metadata`` — no new data is exposed.

        If the metadata fetch itself fails the method degrades gracefully —
        it returns a short directive to call ``get_metabase_database_metadata``
        manually, so the caller always gets *some* actionable guidance.
        """
        cached = _schema_hint_cache.get(database_id)
        if cached is not None:
            return cached
        try:
            async with egress_client(timeout=_TIMEOUT_READ) as client:
                resp, fetch_err = await safe_request(
                    client,
                    "get",
                    f"{base_url}/api/database/{database_id}/metadata",
                    _PROVIDER,
                    "fetch schema for column hint",
                    headers=headers,
                    params={"include_hidden": "false"},
                )
            if fetch_err:
                return _SCHEMA_HINT_FALLBACK
            data = resp.json()
            if not isinstance(data, dict):
                return _SCHEMA_HINT_FALLBACK
            table_lines: list[str] = []
            tables = data.get("tables") or []
            if not isinstance(tables, list):
                return _SCHEMA_HINT_FALLBACK
            for table in tables:
                if not isinstance(table, dict):
                    continue
                schema = table.get("schema") or "public"
                tname = table.get("name", "?")
                fields = table.get("fields") or []
                if not isinstance(fields, list):
                    continue
                # Store only the first N column names but keep counting, so a
                # very wide table never allocates a huge intermediate list on
                # this error path while the "+N more" marker stays accurate.
                col_names: list[str] = []
                total_cols = 0
                for f in fields:
                    if not (isinstance(f, dict) and f.get("name")):
                        continue
                    total_cols += 1
                    if len(col_names) < _SCHEMA_HINT_MAX_COLUMNS_PER_TABLE:
                        col_names.append(f["name"])
                if not col_names:
                    continue
                col_suffix = ""
                if total_cols > _SCHEMA_HINT_MAX_COLUMNS_PER_TABLE:
                    col_suffix = f", … (+{total_cols - _SCHEMA_HINT_MAX_COLUMNS_PER_TABLE} more)"
                table_lines.append(f"  {schema}.{tname}: {', '.join(col_names)}{col_suffix}")
                if len(table_lines) > _SCHEMA_HINT_MAX_TABLES:
                    # One extra line is enough to detect truncation below —
                    # don't format the rest of a potentially huge metadata
                    # payload on this error path.
                    break
            if not table_lines:
                return (
                    " To fix: call get_metabase_database_metadata to inspect the schema."
                )
            capped = table_lines[:_SCHEMA_HINT_MAX_TABLES]
            suffix = (
                " (truncated — call get_metabase_database_metadata for the full list)"
                if len(table_lines) > _SCHEMA_HINT_MAX_TABLES
                else ""
            )
            hint = (
                f" Hint: the column does not exist in database {database_id}. "
                f"Available columns (schema.table: columns){suffix}:\n"
                + "\n".join(capped)
                + "\nUse get_metabase_table_metadata for full type/FK details."
            )
            _schema_hint_cache[database_id] = hint
            return hint
        except Exception:
            logger.warning(
                "Metabase schema-hint fetch failed for database %s",
                database_id,
                exc_info=True,
            )
            return _SCHEMA_HINT_FALLBACK

    async def _source_column_hint_for(column: str, card_id: int | None) -> str:
        """:func:`_source_column_hint` with the saved question's columns read."""
        if card_id is None:
            return _source_column_hint(column, None)
        try:
            loaded, _err = await _load_card_columns(card_id)
        except Exception:
            logger.warning(
                "Metabase source-column hint fetch failed for question %s",
                card_id,
                exc_info=True,
            )
            loaded = None
        if loaded is None:
            return _source_column_hint(column, card_id)
        name, _database_id, columns = loaded
        return _source_column_hint(column, card_id, name, columns)

    async def _enrich_query_error(
        error_msg: str, database_id: int, mbql: dict[str, Any] | None = None
    ) -> str:
        """Apply every deterministic error enrichment to a failed query result.

        Combines the bracket-syntax hint (:func:`_explain_sql_bracket_error`)
        with the column-not-found hint, so all query-running tools return the
        same grounded guidance regardless of which failure path (HTTP error vs
        in-body ``status: failed``) produced *error_msg*. A missing column
        qualified by the source-stage alias belongs to the MBQL stage's source
        (*mbql*), so it is grounded in that source's columns instead of the
        database's tables.
        """
        enriched = _explain_sql_bracket_error(error_msg)
        missing = _missing_column(error_msg)
        if missing is None:
            return enriched
        qualifier, column = missing
        if mbql is not None and qualifier.lower() in _SOURCE_STAGE_ALIASES:
            return enriched + await _source_column_hint_for(column, _card_source_id(mbql))
        return enriched + await _fetch_schema_hint_for_column_error(database_id)

    # ── 2. Questions / Cards ───────────────────────────────────────────────

    async def list_metabase_questions(count: int = 20, filter: str = "all") -> str:
        """List saved questions (cards) in Metabase."""
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/api/card",
                _PROVIDER,
                "list questions",
                headers=headers,
                params={"f": filter},
            )
            if err:
                return err
            cards = resp.json()
            return format_entity_list(
                cards[:count],
                lambda c: (
                    f"{c.get('name', 'Untitled')} | ID: {c.get('id', '')} "
                    f"| Display: {c.get('display', '?')} "
                    f"| Collection: {c.get('collection', {}).get('name', 'root') if c.get('collection') else 'root'}"
                ),
                noun="question",
                total=len(cards),
                empty_msg="No saved questions found.",
            )

    async def read_metabase_question(card_id: int) -> str:
        """Read a question's full configuration.

        All metadata (name, description, display, type, IDs, template tags,
        visualization settings, timestamps) is rendered as ``key: value``
        lines at the top of the response. The SQL body (for native questions)
        and the MBQL query stage (for structured questions) are placed at the
        END inside a fenced code block — so multi-line SQL cannot bleed into
        other fields and confuse round-trip parsing. When you want to update
        the question, copy the *entire* block between the ```sql / ``` fences.
        """
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/api/card/{card_id}",
                _PROVIDER,
                "read question",
                headers=headers,
            )
            if err:
                return err
            c = resp.json()
            query = c.get("dataset_query", {})
            query_type = query.get("type", "?")
            import json as _json

            template_tags = query.get("native", {}).get("template-tags", {})
            tags_section = "Template tags: none"
            if template_tags:
                tag_lines = [f"Template tags: {len(template_tags)} defined"]
                for tname, tdef in template_tags.items():
                    ttype = tdef.get("type", "?")
                    widget = tdef.get("widget-type", "")
                    dim = tdef.get("dimension", [])
                    display_name = tdef.get("display-name", tname)
                    tag_id = tdef.get("id", "?")
                    parts = [f"type={ttype}"]
                    if widget:
                        parts.append(f"widget={widget}")
                    if dim:
                        parts.append(f"dimension={_json.dumps(dim)}")
                    parts.append(f"id={tag_id}")
                    tag_lines.append(
                        f"  • {display_name} ({tname}): {', '.join(parts)}"
                    )
                tags_section = "\n".join(tag_lines)

            viz = c.get("visualization_settings", {})
            viz_section = "Visualization settings: {}"
            if viz:
                viz_section = (
                    f"Visualization settings: {_json.dumps(viz, indent=2, default=str)}"
                )

            # Header: metadata only. Body sections come after, fenced.
            lines = [
                f"Question: {c.get('name', 'Untitled')} (ID: {c.get('id', '')})",
                f"Description: {c.get('description') or 'N/A'}",
                f"Display: {c.get('display', '?')}",
                f"Type: {c.get('type', 'question')}",
                f"Query type: {query_type}",
                f"Database ID: {query.get('database', '?')}",
                tags_section,
                viz_section,
                f"Collection: {c.get('collection', {}).get('name', 'root') if c.get('collection') else 'root'} (ID: {c.get('collection_id') or 'root'})",
                f"Created: {c.get('created_at', '?')}",
                f"Updated: {c.get('updated_at', '?')}",
                f"Creator: {c.get('creator', {}).get('common_name', '?') if c.get('creator') else '?'}",
            ]

            # Body: fenced so the LLM can copy the exact bytes without
            # mis-parsing multi-line SQL or accidentally clipping JSON.
            if query_type == "native":
                sql = query.get("native", {}).get("query", "")
                native_header = (
                    "Native SQL (copy the EXACT body between the fences when "
                    + "updating via `update_metabase_question(native_query=...)`):"
                )
                lines.extend([
                    "",
                    native_header,
                    "```sql",
                    sql if sql else "-- (empty)",
                    "```",
                ])
            elif query_type == "query":
                mbql = query.get("query", {})
                mbql_header = (
                    "MBQL query stage (copy the EXACT JSON between the fences "
                    + "when updating via `update_metabase_question(mbql_query=...)`):"
                )
                lines.extend([
                    "",
                    mbql_header,
                    "```json",
                    _json.dumps(mbql, indent=2, default=str),
                    "```",
                ])

            return "\n".join(lines)

    async def read_metabase_question_raw(card_id: int) -> str:
        """Read a question's raw dataset_query and visualization_settings JSON.
        Use this to inspect the exact template-tags, native SQL, query structure,
        and visualization formatting that Metabase has stored — essential for
        debugging field filter and number formatting issues."""
        import json as _json

        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/api/card/{card_id}",
                _PROVIDER,
                "read question raw",
                headers=headers,
            )
            if err:
                return err
            c = resp.json()
            dq = c.get("dataset_query", {})
            params = c.get("parameters", [])
            viz = c.get("visualization_settings", {})
            result: dict[str, Any] = {
                "id": c.get("id"),
                "name": c.get("name"),
                "dataset_query": dq,
            }
            if params:
                result["parameters"] = params
            if viz:
                result["visualization_settings"] = viz
            return _json.dumps(result, indent=2, default=str)

    async def run_metabase_question(
        card_id: int,
        parameters: str = "",
        ignore_cache: bool = False,
    ) -> str:
        """Run a saved question and return tabular results, led by their freshness.

        parameters: optional JSON string of filter values, e.g.
        '[{"type":"category","value":"US","target":["dimension",["field",42,null]]}]'
        ignore_cache: recompute instead of serving Metabase's cached result.
        """
        body: dict[str, Any] = {}
        parsed_params, parse_err = _parse_json_arg(parameters, "parameters")
        if parse_err:
            return parse_err
        if parsed_params is not None:
            body["parameters"] = parsed_params
        if ignore_cache:
            body["ignore_cache"] = True

        async with egress_client(timeout=_TIMEOUT_QUERY) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}/api/card/{card_id}/query",
                _PROVIDER,
                "run question",
                headers=headers,
                json=body if body else None,
            )
            if err:
                return _explain_sql_bracket_error(err)
            data = resp.json()
            if _check_dataset_failed(data):
                return _format_query_results(data)
            return f"{_result_freshness(data, ignore_cache)}\n{_format_query_results(data)}"

    async def export_metabase_question(
        card_id: int,
        export_format: str = "csv",
        parameters: str = "",
        format_rows: bool = False,
        pivot_results: bool = False,
    ) -> str | ToolResult:
        """Export a saved question's full results as a CSV, XLSX or JSON file."""
        fmt = (export_format or "csv").strip().lower().lstrip(".")
        if fmt not in _EXPORT_FORMATS:
            return f"Error: invalid export_format '{export_format}'. Must be one of: {', '.join(_EXPORT_FORMATS)}."
        parsed_params, parse_err = _parse_json_arg(parameters, "parameters")
        if parse_err:
            return parse_err
        # Form-encoded: the one body every Metabase version reads here
        # (JSON only since 0.53, query parameters only before it).
        form = {
            "format_rows": "true" if format_rows else "false",
            "pivot_results": "true" if pivot_results else "false",
        }
        if parsed_params is not None:
            form["parameters"] = json.dumps(parsed_params)
        web_url = f"{base_url}/question/{card_id}"
        async with egress_client(timeout=_TIMEOUT_EXPORT) as client:
            fetched = await fetch_file(
                client,
                "post",
                f"{base_url}/api/card/{card_id}/query/{fmt}",
                provider=_PROVIDER,
                action=f"export question {card_id} as {fmt}",
                max_bytes=_MAX_EXPORT_BYTES,
                headers={"X-Api-Key": api_key},
                data=form,
            )
        if isinstance(fetched, str):
            return f"{_explain_sql_bracket_error(fetched)}\nWeb URL: {web_url}"
        # A query that fails once the export has started answers 2xx with
        # the failure as a JSON object (a JSON export itself is an array).
        if "json" in fetched.content_type.lower() and fetched.data[:64].lstrip()[:1] == b"{":
            try:
                body = json.loads(fetched.data)
            except ValueError:
                body = None
            failure = _check_dataset_failed(body) if isinstance(body, dict) else None
            if failure:
                return f"{_explain_sql_bracket_error(failure)}\nWeb URL: {web_url}"
        filename = fetched.filename or f"metabase-question-{card_id}.{fmt}"
        return await deliver_file(
            data=fetched.data,
            filename=filename,
            content_type=file_content_type(fetched.content_type, filename),
            source="Metabase",
            persist_file_fn=persist_file_fn,
            links=[("Web URL", web_url)],
            details=[f"Question {card_id} exported as {fmt.upper()} (all rows, not the preview cap)."],
        )

    async def create_metabase_question(
        name: str,
        database_id: int,
        query_type: str = "native",
        native_query: str = "",
        source_table_id: int = 0,
        mbql_query: str = "",
        display: str = "table",
        description: str = "",
        collection_id: int = 0,
        card_type: str = "question",
        template_tags: str = "",
        visualization_settings: str = "",
    ) -> str:
        """Create a new saved question (card).

        Two query languages are supported, mirroring Metabase's GUI:

        - **Native (SQL)** — set ``query_type='native'`` and provide
          ``native_query``. Use ``template_tags`` JSON for parameterised
          queries with ``{{variable}}`` placeholders. We pre-validate the
          SQL/template-tag pair so the agent catches
          ``syntax error at or near "["`` bugs *before* saving instead of
          discovering them at query time.
        - **MBQL (structured / "GUI builder" question)** — set
          ``query_type='query'`` and either:

          * provide just ``source_table_id`` for a bare "select all" question, or
          * provide a full ``mbql_query`` JSON object containing the query
            stage (``source-table``, ``filter``, ``aggregation``, ``breakout``,
            ``order-by``, ``limit``, ``joins``, ``expressions`` …).
            ``mbql_query`` is forwarded verbatim into the question's
            ``dataset_query.query`` so anything Metabase's MBQL accepts works.

        ``visualization_settings`` is an optional JSON object stored on the
        card (e.g. number formatting, axis labels). Defaults to ``{}``.
        ``card_type``: ``'question'`` (default), ``'model'``, or ``'metric'``.
        """
        dataset_query: dict[str, Any] = {"database": database_id, "type": query_type}

        parsed_tags, tags_err = _parse_json_arg(template_tags, "template_tags")
        if tags_err:
            return tags_err
        parsed_mbql, mbql_err = _parse_json_arg(mbql_query, "mbql_query")
        if mbql_err:
            return mbql_err
        parsed_viz, viz_err = _parse_json_arg(
            visualization_settings, "visualization_settings"
        )
        if viz_err:
            return viz_err

        if query_type == "native":
            if not native_query:
                return "native_query is required when query_type='native'."
            coverage_error = _validate_template_tag_coverage(native_query, parsed_tags)
            if coverage_error:
                return coverage_error
            native_payload: dict[str, Any] = {"query": native_query}
            if parsed_tags is not None:
                native_payload["template-tags"] = parsed_tags
            dataset_query["native"] = native_payload
        elif query_type == "query":
            if parsed_mbql is not None:
                mbql = _normalise_mbql(parsed_mbql)
                if mbql is None:
                    return (
                        "mbql_query must be a JSON object (the inner query stage). "
                        "Example: '{\"source-table\": 42, \"aggregation\": [[\"count\"]], "
                        '"breakout": [["field", 101, null]]}\''
                    )
                if "source-table" not in mbql and source_table_id:
                    mbql["source-table"] = source_table_id
                if "source-table" not in mbql and "source-query" not in mbql:
                    return (
                        "mbql_query must include a `source-table` (int) or "
                        "`source-query` (nested query). Pass source_table_id "
                        "or embed it in the JSON."
                    )
                dataset_query["query"] = mbql
            elif source_table_id:
                dataset_query["query"] = {"source-table": source_table_id}
            else:
                return (
                    "When query_type='query' you must provide either "
                    "`mbql_query` (full MBQL JSON) or `source_table_id`."
                )
        else:
            return (
                f"Invalid query_type '{query_type}'. Use 'native' for SQL or "
                f"'query' for MBQL (structured)."
            )

        body: dict[str, Any] = {
            "name": name,
            "dataset_query": dataset_query,
            "display": display,
            "visualization_settings": parsed_viz if isinstance(parsed_viz, dict) else {},
        }
        if card_type != "question":
            body["type"] = card_type
        if description:
            body["description"] = description
        if collection_id:
            body["collection_id"] = collection_id

        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}/api/card",
                _PROVIDER,
                "create question",
                headers=headers,
                json=body,
            )
            if err:
                return err
            card = resp.json()
            return (
                f"Created question '{card.get('name', '')}' (ID: {card.get('id', '')})"
            )

    async def update_metabase_question(
        card_id: int,
        name: str = "",
        description: str = "",
        display: str = "",
        collection_id: int = 0,
        native_query: str = "",
        mbql_query: str = "",
        archived: bool = False,
        card_type: str = "",
        visualization_settings: str = "",
        template_tags: str = "",
    ) -> str:
        """Update an existing question in-place. Preferred over delete+recreate.

        template_tags: optional JSON string of template tag definitions.
        When provided with native_query, tags are set on the new query.
        When provided alone, tags are merged into the existing query's tags.
        This is required for adding field filter variables to native SQL
        questions (e.g. for dashboard-level date filtering).

        .. important::
           ``template_tags`` only apply to **native SQL** questions. Calling
           this with ``template_tags`` and no ``native_query`` on a **MBQL**
           (structured) question is refused — the previous behaviour silently
           wiped the MBQL body and saved an empty SQL string. To attach a
           dashboard filter to an MBQL field, use
           :func:`map_metabase_dashboard_filter` with ``target_type='dimension'``
           instead.

        Native SQL updates are pre-validated against ``template_tags`` (after
        merging with existing tags) so the agent catches
        ``syntax error at or near "["`` mismatches before they hit prod, and
        an empty / whitespace-only resulting SQL is refused so a malformed
        update can never wipe the question body.

        mbql_query: optional JSON object **fully replacing** the question's
        MBQL query stage (``source-table``, ``filter``, ``aggregation``,
        ``breakout``, ``order-by``, ``limit``, ``joins``, ``expressions``…).
        Switches the question to ``type='query'`` automatically.

        .. important::
           Unlike ``template_tags`` and ``visualization_settings`` (which are
           merged with the existing values), ``mbql_query`` is a **full
           replacement** of the query stage. If you only need to tweak a
           single clause (e.g. add a filter to an existing aggregation), call
           ``read_metabase_question_raw`` first to fetch the current
           ``dataset_query.query``, patch the JSON locally, and pass the
           complete updated query back here. This asymmetry is intentional —
           silently merging top-level MBQL keys would create surprising
           interactions between the existing and new ``aggregation`` /
           ``breakout`` arrays.

        visualization_settings: JSON string of settings to merge into the
        existing visualization_settings. Keys you provide are added/overwritten;
        keys you omit are preserved. For column_settings, nested column keys
        are also merged (not replaced).
        """
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            needs_existing = (
                native_query
                or mbql_query
                or template_tags
                or visualization_settings
            )
            existing_card: dict[str, Any] = {}
            if needs_existing:
                read_resp, read_err = await safe_request(
                    client,
                    "get",
                    f"{base_url}/api/card/{card_id}",
                    _PROVIDER,
                    "read question for update",
                    headers=headers,
                )
                if read_err:
                    return read_err
                existing_card = read_resp.json()

            existing_dq = existing_card.get("dataset_query", {})

            parsed_tags, tags_err = _parse_json_arg(template_tags, "template_tags")
            if tags_err:
                return tags_err
            parsed_mbql, mbql_err = _parse_json_arg(mbql_query, "mbql_query")
            if mbql_err:
                return mbql_err
            parsed_viz, viz_err = _parse_json_arg(
                visualization_settings, "visualization_settings"
            )
            if viz_err:
                return viz_err

            body: dict[str, Any] = {}
            if name:
                body["name"] = name
            if description:
                body["description"] = description
            if display:
                body["display"] = display
            if collection_id:
                body["collection_id"] = collection_id
            if parsed_mbql is not None:
                mbql = _normalise_mbql(parsed_mbql)
                if mbql is None:
                    return (
                        "mbql_query must be a JSON object (the inner query stage). "
                        "See the create_metabase_question docs for the schema."
                    )
                # MBQL updates are a FULL replacement of the query stage — by
                # design. If you need to tweak a single
                # clause, call read_metabase_question_raw first, patch the JSON,
                # and pass the complete updated query.
                db_id = existing_dq.get("database")
                body["dataset_query"] = {
                    "database": db_id,
                    "type": "query",
                    "query": mbql,
                }
            elif native_query or parsed_tags is not None:
                db_id = existing_dq.get("database")
                existing_type = existing_dq.get("type")
                existing_native = existing_dq.get("native", {}) or {}
                existing_sql = existing_native.get("query") or ""

                # ─── Safety net (production bug, review thread) ────
                # If the existing question is structured (MBQL) and the caller
                # passes ONLY template_tags (no native_query), the legacy code
                # path silently rewrote the card as `type=native` with an empty
                # SQL body — wiping the question. Refuse instead, with a clear
                # remediation pointer.
                if (
                    parsed_tags is not None
                    and not native_query
                    and existing_type == "query"
                ):
                    return (
                        f"Refusing to apply template_tags to question {card_id}: "
                        f"it is a structured (MBQL) question, not native SQL. "
                        f"template_tags only apply to native SQL questions and "
                        f"applying them here would replace the MBQL structure "
                        f"with an empty SQL body. To update the question's "
                        f"structure, pass `mbql_query` instead. To bind a "
                        f"dashboard filter to an MBQL field, use "
                        f"`map_metabase_dashboard_filter` with target_type="
                        f"'dimension'."
                    )

                sql = native_query or existing_sql

                # Defensive: if the resulting SQL is empty or whitespace-only,
                # refuse to save — that would wipe the question. This also
                # catches the rare case of an LLM passing `native_query=' '`
                # or `'\\n'` thinking it is a no-op.
                if not sql.strip():
                    return (
                        f"Refusing to save question {card_id} with empty SQL — "
                        f"that would wipe the question body. Either pass a "
                        f"non-empty `native_query`, switch to `mbql_query` if "
                        f"the question is structured, or use "
                        f"`delete_metabase_question` if you really want to "
                        f"remove it."
                    )

                merged_tags = dict(existing_native.get("template-tags", {}))
                if parsed_tags is not None:
                    merged_tags.update(parsed_tags)
                coverage_error = _validate_template_tag_coverage(sql, merged_tags)
                if coverage_error:
                    return coverage_error
                native_payload: dict[str, Any] = {"query": sql}
                if merged_tags:
                    native_payload["template-tags"] = merged_tags
                body["dataset_query"] = {
                    "database": db_id,
                    "type": "native",
                    "native": native_payload,
                }
            if archived:
                body["archived"] = True
            if card_type:
                body["type"] = card_type
            if parsed_viz is not None:
                if not isinstance(parsed_viz, dict):
                    return (
                        "visualization_settings must be a JSON object."
                    )
                existing_viz = dict(existing_card.get("visualization_settings", {}))
                for key, val in parsed_viz.items():
                    if key == "column_settings" and isinstance(val, dict):
                        existing_col = existing_viz.get("column_settings", {})
                        if isinstance(existing_col, dict):
                            for col_key, col_val in val.items():
                                if (
                                    col_key in existing_col
                                    and isinstance(existing_col[col_key], dict)
                                    and isinstance(col_val, dict)
                                ):
                                    existing_col[col_key] = {
                                        **existing_col[col_key],
                                        **col_val,
                                    }
                                else:
                                    existing_col[col_key] = col_val
                            existing_viz["column_settings"] = existing_col
                        else:
                            existing_viz["column_settings"] = val
                    else:
                        existing_viz[key] = val
                body["visualization_settings"] = existing_viz
            if not body:
                return "No fields provided to update."

            resp, err = await safe_request(
                client,
                "put",
                f"{base_url}/api/card/{card_id}",
                _PROVIDER,
                "update question",
                headers=headers,
                json=body,
            )
            if err:
                return err
            card = resp.json()
            return (
                f"Updated question '{card.get('name', '')}' (ID: {card.get('id', '')})"
            )

    async def delete_metabase_question(card_id: int) -> str:
        """Delete a saved question."""
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            _, err = await safe_request(
                client,
                "delete",
                f"{base_url}/api/card/{card_id}",
                _PROVIDER,
                "delete question",
                headers=headers,
            )
            if err:
                return err
            return f"Deleted question ID {card_id}."

    async def copy_metabase_question(card_id: int, collection_id: int = 0) -> str:
        """Duplicate a saved question, optionally into a different collection."""
        body: dict[str, Any] = {}
        if collection_id:
            body["collection_id"] = collection_id
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}/api/card/{card_id}/copy",
                _PROVIDER,
                "copy question",
                headers=headers,
                json=body,
            )
            if err:
                return err
            card = resp.json()
            return f"Copied question to '{card.get('name', '')}' (new ID: {card.get('id', '')})"

    async def list_metabase_question_dashboards(card_id: int) -> str:
        """List dashboards that contain a given question."""
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/api/card/{card_id}/dashboards",
                _PROVIDER,
                "list question dashboards",
                headers=headers,
            )
            if err:
                return err
            dashboards = resp.json()
            return format_entity_list(
                dashboards,
                lambda d: f"{d.get('name', 'Untitled')} | ID: {d.get('id', '')}",
                noun="dashboard",
                empty_msg=f"Question {card_id} is not used in any dashboard.",
            )

    # ── 3. Dashboards ─────────────────────────────────────────────────────

    async def list_metabase_dashboards(count: int = 20) -> str:
        """List dashboards in Metabase."""
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/api/dashboard",
                _PROVIDER,
                "list dashboards",
                headers=headers,
            )
            if err:
                return err
            dashboards = resp.json()
            return format_entity_list(
                dashboards[:count],
                lambda d: (
                    f"{d.get('name', 'Untitled')} | ID: {d.get('id', '')} "
                    f"| Collection: {d.get('collection', {}).get('name', 'root') if d.get('collection') else 'root'}"
                ),
                noun="dashboard",
                total=len(dashboards),
            )

    async def read_metabase_dashboard(dashboard_id: int) -> str:
        """Read a dashboard's details including its cards/questions."""
        async with egress_client(timeout=_TIMEOUT_DASHBOARD) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/api/dashboard/{dashboard_id}",
                _PROVIDER,
                "read dashboard",
                headers=headers,
            )
            if err:
                return err
            d = resp.json()
            cards = d.get("dashcards", d.get("ordered_cards", []))
            tabs = d.get("tabs", [])
            params = d.get("parameters", [])
            lines = [
                f"Dashboard: {d.get('name', '')} (ID: {d.get('id', '')})",
                f"Description: {d.get('description') or 'N/A'}",
                f"Collection: {d.get('collection', {}).get('name', 'root') if d.get('collection') else 'root'} (ID: {d.get('collection_id') or 'root'})",
                f"Created: {d.get('created_at', '?')}",
                f"Updated: {d.get('updated_at', '?')}",
                f"Cards: {len(cards)}",
            ]
            if tabs:
                lines.append(f"Tabs: {', '.join(t.get('name', '?') for t in tabs)}")
            if params:
                lines.append("")
                lines.append(f"Filters/Parameters ({len(params)}):")
                for p in params:
                    lines.append(
                        f"  • {p.get('name', '?')} (ID: '{p.get('id', '')}', "
                        f"type: {p.get('type', '?')}, slug: {p.get('slug', '?')})"
                    )
            lines.append("")
            lines.append("Cards:")
            for card in cards:
                c = card.get("card", {})
                if c.get("name"):
                    tab_name = ""
                    if card.get("dashboard_tab_id") and tabs:
                        tab = next(
                            (
                                t
                                for t in tabs
                                if t.get("id") == card["dashboard_tab_id"]
                            ),
                            None,
                        )
                        tab_name = f" [Tab: {tab.get('name', '?')}]" if tab else ""
                    mappings = card.get("parameter_mappings", [])
                    mapping_str = ""
                    if mappings:
                        mapped = [
                            f"{m.get('parameter_id', '?')}→field:{m.get('target', ['?'])[1][1] if len(m.get('target', [])) > 1 and isinstance(m.get('target', [None])[1], list) and len(m['target'][1]) > 1 else '?'}"
                            for m in mappings
                        ]
                        mapping_str = f" filters: [{', '.join(mapped)}]"
                    lines.append(
                        f"  • {c.get('name')} (Card ID: {c.get('id', '')}, "
                        f"DashCard ID: {card.get('id', '')}, "
                        f"display: {c.get('display', '?')}, "
                        f"pos: row={card.get('row', '?')} col={card.get('col', '?')} "
                        f"size={card.get('size_x', '?')}x{card.get('size_y', '?')})"
                        f"{tab_name}{mapping_str}"
                    )
            return "\n".join(lines)

    async def create_metabase_dashboard(
        name: str,
        description: str = "",
        collection_id: int = 0,
    ) -> str:
        """Create a new empty dashboard."""
        body: dict[str, Any] = {"name": name}
        if description:
            body["description"] = description
        if collection_id:
            body["collection_id"] = collection_id

        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}/api/dashboard",
                _PROVIDER,
                "create dashboard",
                headers=headers,
                json=body,
            )
            if err:
                return err
            dash = resp.json()
            return (
                f"Created dashboard '{dash.get('name', '')}' (ID: {dash.get('id', '')})"
            )

    async def update_metabase_dashboard(
        dashboard_id: int,
        name: str = "",
        description: str = "",
        collection_id: int = 0,
        archived: bool = False,
        width: str = "",
        cache_ttl: int = 0,
    ) -> str:
        """Update a dashboard's properties."""
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        if description:
            body["description"] = description
        if collection_id:
            body["collection_id"] = collection_id
        if archived:
            body["archived"] = True
        if width:
            body["width"] = width
        if cache_ttl:
            body["cache_ttl"] = cache_ttl
        if not body:
            return "No fields provided to update."

        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await safe_request(
                client,
                "put",
                f"{base_url}/api/dashboard/{dashboard_id}",
                _PROVIDER,
                "update dashboard",
                headers=headers,
                json=body,
            )
            if err:
                return err
            dash = resp.json()
            return (
                f"Updated dashboard '{dash.get('name', '')}' (ID: {dash.get('id', '')})"
            )

    async def delete_metabase_dashboard(dashboard_id: int) -> str:
        """Delete a dashboard."""
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            _, err = await safe_request(
                client,
                "delete",
                f"{base_url}/api/dashboard/{dashboard_id}",
                _PROVIDER,
                "delete dashboard",
                headers=headers,
            )
            if err:
                return err
            return f"Deleted dashboard ID {dashboard_id}."

    async def add_card_to_metabase_dashboard(
        dashboard_id: int,
        card_id: int,
        row: int = 0,
        col: int = 0,
        size_x: int = 6,
        size_y: int = 4,
        dashboard_tab_id: int = 0,
    ) -> str:
        """Add a question card to a dashboard at a given position.

        First reads the current dashboard state, then appends the new card
        and PUTs the full card list back (Metabase requires the full list).
        """
        async with egress_client(timeout=_TIMEOUT_DASHBOARD) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/api/dashboard/{dashboard_id}",
                _PROVIDER,
                "read dashboard for card add",
                headers=headers,
            )
            if err:
                return err
            dash = resp.json()
            existing = dash.get("dashcards", dash.get("ordered_cards", []))
            existing_tabs = dash.get("tabs", []) or []

            existing_cards = [
                _serialize_dashcard(dc)
                for dc in existing
                if dc.get("id") is not None
            ]

            target_tab_id: int | None
            if dashboard_tab_id:
                target_tab_id = dashboard_tab_id
            elif existing_tabs:
                target_tab_id = existing_tabs[0].get("id")
            else:
                target_tab_id = None

            if existing_tabs and target_tab_id is None:
                return (
                    f"Dashboard {dashboard_id} has tabs; you must pass dashboard_tab_id. "
                    f"Available tab IDs: {[t.get('id') for t in existing_tabs]}"
                )
            if (
                existing_tabs
                and target_tab_id is not None
                and not any(t.get("id") == target_tab_id for t in existing_tabs)
            ):
                return (
                    f"dashboard_tab_id {target_tab_id} is not on dashboard {dashboard_id}. "
                    f"Available tab IDs: {[t.get('id') for t in existing_tabs]}"
                )

            new_card: dict[str, Any] = {
                "id": -1,
                "card_id": card_id,
                "row": row,
                "col": col,
                "size_x": size_x,
                "size_y": size_y,
                "dashboard_tab_id": target_tab_id,
                "series": [],
                "parameter_mappings": [],
                "visualization_settings": {},
            }
            all_cards = [*existing_cards, new_card]

            tabs_payload = (
                [{"id": t["id"], "name": t["name"]} for t in existing_tabs]
                if existing_tabs
                else None
            )

            body: dict[str, Any] = {"cards": all_cards}
            if tabs_payload:
                body["tabs"] = tabs_payload

            _, err2 = await safe_request(
                client,
                "put",
                f"{base_url}/api/dashboard/{dashboard_id}/cards",
                _PROVIDER,
                "add card to dashboard",
                headers=headers,
                json=body,
            )
            if err2:
                return err2
            tab_suffix = f" (tab {target_tab_id})" if target_tab_id else ""
            return f"Added question {card_id} to dashboard {dashboard_id} at row={row}, col={col}, size={size_x}x{size_y}{tab_suffix}."

    async def copy_metabase_dashboard(
        dashboard_id: int,
        name: str = "",
        collection_id: int = 0,
        deep_copy: bool = True,
    ) -> str:
        """Duplicate a dashboard. deep_copy=True also copies the questions."""
        body: dict[str, Any] = {"is_deep_copy": deep_copy}
        if name:
            body["name"] = name
        if collection_id:
            body["collection_id"] = collection_id

        async with egress_client(timeout=_TIMEOUT_QUERY) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}/api/dashboard/{dashboard_id}/copy",
                _PROVIDER,
                "copy dashboard",
                headers=headers,
                json=body,
            )
            if err:
                return err
            dash = resp.json()
            return f"Copied dashboard to '{dash.get('name', '')}' (new ID: {dash.get('id', '')})"

    async def remove_card_from_metabase_dashboard(
        dashboard_id: int,
        dashcard_id: int,
    ) -> str:
        """Remove a card from a dashboard by its dashcard ID.

        Reads the current dashboard, filters out the target dashcard,
        and PUTs the remaining list back.
        """
        async with egress_client(timeout=_TIMEOUT_DASHBOARD) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/api/dashboard/{dashboard_id}",
                _PROVIDER,
                "read dashboard for card removal",
                headers=headers,
            )
            if err:
                return err
            dash = resp.json()
            dashcards = dash.get("dashcards", dash.get("ordered_cards", []))

            before_count = len(dashcards)
            remaining = [dc for dc in dashcards if dc.get("id") != dashcard_id]
            if len(remaining) == before_count:
                return f"DashCard ID {dashcard_id} not found on dashboard {dashboard_id}. Use read_metabase_dashboard to list dashcard IDs."

            cards_payload = [_serialize_dashcard(dc) for dc in remaining]

            existing_tabs = dash.get("tabs", [])
            tabs_payload = (
                [{"id": t["id"], "name": t["name"]} for t in existing_tabs]
                if existing_tabs
                else None
            )

            put_body: dict[str, Any] = {"cards": cards_payload}
            if tabs_payload:
                put_body["tabs"] = tabs_payload

            _, err2 = await safe_request(
                client,
                "put",
                f"{base_url}/api/dashboard/{dashboard_id}/cards",
                _PROVIDER,
                "remove card from dashboard",
                headers=headers,
                json=put_body,
            )
            if err2:
                return err2
            return f"Removed dashcard {dashcard_id} from dashboard {dashboard_id}."

    async def add_metabase_dashboard_filter(
        dashboard_id: int,
        name: str,
        slug: str,
        filter_type: str,
        parameter_id: str = "",
    ) -> str:
        """Add a filter parameter to a dashboard.

        This creates the filter definition on the dashboard itself.
        After creating the filter, use map_metabase_dashboard_filter
        to wire it to specific cards.

        filter_type examples: 'date/single', 'date/range', 'date/relative',
        'date/month-year', 'date/quarter-year', 'string/=', 'string/!=',
        'string/contains', 'number/=', 'number/!=', 'number/>=', 'number/<=',
        'number/between', 'category', 'id', 'location/city', 'location/state',
        'location/zip_code', 'location/country'.
        """
        async with egress_client(timeout=_TIMEOUT_DASHBOARD) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/api/dashboard/{dashboard_id}",
                _PROVIDER,
                "read dashboard for filter add",
                headers=headers,
            )
            if err:
                return err
            dash = resp.json()
            existing_params = dash.get("parameters", []) or []

            pid = parameter_id or slug
            if any(p.get("id") == pid for p in existing_params):
                return f"A parameter with ID '{pid}' already exists on dashboard {dashboard_id}."

            new_param: dict[str, Any] = {
                "id": pid,
                "name": name,
                "slug": slug,
                "type": filter_type,
            }
            updated_params = [*existing_params, new_param]

            _, err2 = await safe_request(
                client,
                "put",
                f"{base_url}/api/dashboard/{dashboard_id}",
                _PROVIDER,
                "add dashboard filter",
                headers=headers,
                json={"parameters": updated_params},
            )
            if err2:
                return err2
            return f"Added filter '{name}' (ID: '{pid}', type: {filter_type}) to dashboard {dashboard_id}."

    async def map_metabase_dashboard_filter(
        dashboard_id: int,
        dashcard_id: int,
        parameter_id: str,
        card_id: int,
        target_type: str = "dimension",
        target_field_id: int = 0,
        target_variable: str = "",
    ) -> str:
        """Connect a dashboard filter to a specific card (question) on that dashboard.

        This wires a dashboard-level parameter (filter) to a field or variable
        in a card so the filter actually affects that card's results.

        Steps to use:
        1. Use read_metabase_dashboard to find the dashboard's parameters
           (each has an 'id' like 'abc123') and its dashcards (each has an 'id').
        2. Use get_metabase_table_metadata to find the target field ID.
        3. Call this tool with the IDs.

        target_type:
          - 'dimension': map to a database field (most common). Provide target_field_id.
          - 'variable': map to a template tag variable in a native query. Provide target_variable.
        """
        async with egress_client(timeout=_TIMEOUT_DASHBOARD) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/api/dashboard/{dashboard_id}",
                _PROVIDER,
                "read dashboard for filter mapping",
                headers=headers,
            )
            if err:
                return err
            dash = resp.json()
            dashcards = dash.get("dashcards", dash.get("ordered_cards", []))

            target_dc = next(
                (dc for dc in dashcards if dc.get("id") == dashcard_id), None
            )
            if target_dc is None:
                return f"DashCard ID {dashcard_id} not found on dashboard {dashboard_id}. Use read_metabase_dashboard to list dashcard IDs."

            params = dash.get("parameters", [])
            param_exists = any(p.get("id") == parameter_id for p in params)
            if not param_exists:
                param_ids = [p.get("id") for p in params]
                return f"Parameter '{parameter_id}' not found on dashboard. Available parameter IDs: {param_ids}"

            if target_type == "dimension":
                if not target_field_id:
                    return "target_field_id is required when target_type='dimension'."
                target = ["dimension", ["field", target_field_id, None]]
            elif target_type == "variable":
                if not target_variable:
                    return "target_variable is required when target_type='variable'."
                target = ["variable", ["template-tag", target_variable]]
            else:
                return f"Invalid target_type '{target_type}'. Use 'dimension' or 'variable'."

            new_mapping = {
                "parameter_id": parameter_id,
                "card_id": card_id,
                "target": target,
            }

            existing_mappings = target_dc.get("parameter_mappings", []) or []
            updated_mappings = [
                m
                for m in existing_mappings
                if not (
                    m.get("parameter_id") == parameter_id
                    and m.get("card_id") == card_id
                )
            ]
            updated_mappings.append(new_mapping)

            cards_payload = []
            for dc in dashcards:
                entry = _serialize_dashcard(dc)
                if dc.get("id") == dashcard_id:
                    entry["parameter_mappings"] = updated_mappings
                cards_payload.append(entry)

            existing_tabs = dash.get("tabs", [])
            tabs_payload = (
                [{"id": t["id"], "name": t["name"]} for t in existing_tabs]
                if existing_tabs
                else None
            )

            put_body: dict[str, Any] = {"cards": cards_payload}
            if tabs_payload:
                put_body["tabs"] = tabs_payload

            _, err2 = await safe_request(
                client,
                "put",
                f"{base_url}/api/dashboard/{dashboard_id}/cards",
                _PROVIDER,
                "map dashboard filter to card",
                headers=headers,
                json=put_body,
            )
            if err2:
                return err2

            target_desc = (
                f"field ID {target_field_id}"
                if target_type == "dimension"
                else f"variable '{target_variable}'"
            )
            return f"Mapped dashboard filter '{parameter_id}' to card {card_id} (dashcard {dashcard_id}) targeting {target_desc}."

    # ── 4. Collections ─────────────────────────────────────────────────────

    async def list_metabase_collections(count: int = 30, tree: bool = False) -> str:
        """List collections in Metabase. Set tree=True for hierarchical view."""
        endpoint = "/api/collection/tree" if tree else "/api/collection"
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}{endpoint}",
                _PROVIDER,
                "list collections",
                headers=headers,
            )
            if err:
                return err
            collections = resp.json()

            if tree:

                def _render_tree(items: list[dict], indent: int = 0) -> list[str]:
                    lines: list[str] = []
                    for c in items:
                        prefix = "  " * indent + "• "
                        lines.append(
                            f"{prefix}{c.get('name', '?')} (ID: {c.get('id', '')})"
                        )
                        children = c.get("children", [])
                        if children:
                            lines.extend(_render_tree(children, indent + 1))
                    return lines

                tree_lines = _render_tree(collections)
                return (
                    f"Collection tree ({len(tree_lines)} items):\n"
                    + "\n".join(tree_lines)
                    if tree_lines
                    else "No collections found."
                )

            return format_entity_list(
                collections[:count],
                lambda c: f"{c.get('name', '')} | ID: {c.get('id', '')} | Items: {c.get('effective_count', '?')}",
                noun="collection",
                total=len(collections),
            )

    async def read_metabase_collection_items(
        collection_id: int = 0,
        count: int = 30,
        models: str = "",
    ) -> str:
        """Browse items in a collection. Use collection_id=0 for root.

        models: comma-separated filter (e.g. 'card,dashboard,collection').
        """
        coll_path = "root" if collection_id == 0 else str(collection_id)
        params: dict[str, Any] = {}
        if models:
            params["models"] = models.split(",")

        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/api/collection/{coll_path}/items",
                _PROVIDER,
                "list collection items",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()
            items = data.get("data", data) if isinstance(data, dict) else data
            items = items if isinstance(items, list) else []
            return format_entity_list(
                items[:count],
                lambda i: (
                    f"[{i.get('model', '?').upper()}] {i.get('name', 'Untitled')} "
                    f"| ID: {i.get('id', '')} "
                    f"| Description: {(i.get('description') or 'N/A')[:80]}"
                ),
                noun="item",
                total=len(items),
                empty_msg="Collection is empty.",
            )

    async def create_metabase_collection(
        name: str,
        description: str = "",
        parent_id: int = 0,
    ) -> str:
        """Create a new collection."""
        body: dict[str, Any] = {"name": name}
        if description:
            body["description"] = description
        if parent_id:
            body["parent_id"] = parent_id

        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}/api/collection",
                _PROVIDER,
                "create collection",
                headers=headers,
                json=body,
            )
            if err:
                return err
            coll = resp.json()
            return f"Created collection '{coll.get('name', '')}' (ID: {coll.get('id', '')})"

    async def update_metabase_collection(
        collection_id: int,
        name: str = "",
        description: str = "",
        parent_id: int = 0,
        archived: bool = False,
    ) -> str:
        """Update a collection's properties."""
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        if description:
            body["description"] = description
        if parent_id:
            body["parent_id"] = parent_id
        if archived:
            body["archived"] = True
        if not body:
            return "No fields provided to update."

        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await safe_request(
                client,
                "put",
                f"{base_url}/api/collection/{collection_id}",
                _PROVIDER,
                "update collection",
                headers=headers,
                json=body,
            )
            if err:
                return err
            coll = resp.json()
            return f"Updated collection '{coll.get('name', '')}' (ID: {coll.get('id', '')})"

    # ── 5. Data Querying ───────────────────────────────────────────────────

    async def run_metabase_query(
        database_id: int,
        query: str,
        max_rows: int = _MAX_ROWS,
        template_tags: str = "",
        parameters: str = "",
    ) -> str:
        """Run a native SQL query against a Metabase database.

        template_tags: optional JSON string for parameterized queries using
        {{variable}} syntax, e.g. '{"date":{"type":"date","name":"date","id":"abc"}}'

        parameters: optional JSON array of parameter values to bind to the
        template tags (matches the structure used by ``/api/card/{id}/query``).
        """
        parsed_tags, tags_err = _parse_json_arg(template_tags, "template_tags")
        if tags_err:
            return tags_err
        parsed_params, params_err = _parse_json_arg(parameters, "parameters")
        if params_err:
            return params_err

        coverage_error = _validate_template_tag_coverage(query, parsed_tags)
        if coverage_error:
            return coverage_error

        native_payload: dict[str, Any] = {"query": query}
        if parsed_tags is not None:
            native_payload["template-tags"] = parsed_tags

        request_body: dict[str, Any] = {
            "database": database_id,
            "type": "native",
            "native": native_payload,
        }
        if parsed_params is not None:
            request_body["parameters"] = parsed_params

        async with egress_client(timeout=_TIMEOUT_QUERY) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}/api/dataset",
                _PROVIDER,
                "run SQL query",
                headers=headers,
                json=request_body,
            )
            if err:
                return await _enrich_query_error(err, database_id)
            data = resp.json()
            failure = _check_dataset_failed(data)
            if failure:
                # Metabase can report the DB error in-body on HTTP 200/202
                # (status: failed) — enrich that path identically.
                return await _enrich_query_error(failure, database_id)
            return _format_query_results(data, max_rows=max_rows)

    async def run_metabase_mbql_query(
        database_id: int,
        mbql_query: str,
        max_rows: int = _MAX_ROWS,
        parameters: str = "",
    ) -> str:
        """Run a structured **MBQL** query (Metabase Query Language) against a
        database. This is the same query format that the Metabase GUI builder
        produces and that the official Metabase MCP server uses internally.

        Prefer MBQL over raw SQL whenever possible: it composes safely, the
        result columns carry proper Metabase metadata (semantic types, FKs),
        and you avoid the entire ``[[…]]`` / template-tag class of bugs.

        ``mbql_query`` is a JSON object representing the query stage, e.g.::

            {
              "source-table": 42,
              "filter": [">", ["field", 302, null], 100],
              "aggregation": [["sum", ["field", 302, null]]],
              "breakout": [["field", 305, {"temporal-unit": "month"}]],
              "order-by": [["desc", ["aggregation", 0]]],
              "limit": 100
            }

        Supported source forms (mirroring Metabase's MBQL):

          * ``"source-table": <table_id>`` — a database table
          * ``"source-query": {...}`` — a nested query stage
          * ``"source-table": "card__ID"`` — a saved question (string ID)

        Use ``get_metabase_table_metadata`` first to discover field IDs, with
        ``card_id`` for a saved-question source, whose columns are referenced
        by name (``["field", "COLUMN_NAME", {"base-type": ...}]``).
        """
        parsed_mbql, err_msg = _parse_json_arg(mbql_query, "mbql_query")
        if err_msg:
            return err_msg
        mbql = _normalise_mbql(parsed_mbql)
        if mbql is None:
            return (
                "mbql_query must be a JSON object (the inner query stage). "
                "Example: '{\"source-table\": 42, \"aggregation\": [[\"count\"]]}'."
            )
        if "source-table" not in mbql and "source-query" not in mbql:
            return (
                "mbql_query must include a `source-table` (int or 'card__ID') "
                "or `source-query` (nested query). See "
                "https://www.metabase.com/learn/sql/working-with-sql/sql-and-mbql "
                "for the MBQL reference."
            )

        parsed_params, params_err = _parse_json_arg(parameters, "parameters")
        if params_err:
            return params_err

        request_body: dict[str, Any] = {
            "database": database_id,
            "type": "query",
            "query": mbql,
        }
        if parsed_params is not None:
            request_body["parameters"] = parsed_params

        async with egress_client(timeout=_TIMEOUT_QUERY) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}/api/dataset",
                _PROVIDER,
                "run MBQL query",
                headers=headers,
                json=request_body,
            )
            if err:
                # MBQL queries with `source-table: "card__ID"` can reference a
                # saved question whose underlying native SQL is broken, so both
                # the bracket-error hint and the column schema hint still apply.
                return await _enrich_query_error(err, database_id, mbql)
            data = resp.json()
            failure = _check_dataset_failed(data)
            if failure:
                return await _enrich_query_error(failure, database_id, mbql)
            return _format_query_results(data, max_rows=max_rows)

    async def validate_metabase_native_query(
        database_id: int,
        query: str,
        template_tags: str = "",
        parameters: str = "",
    ) -> str:
        """Dry-run a native SQL query without persisting it as a question.

        Use this before calling ``create_metabase_question`` to confirm that
        the SQL + ``template_tags`` pair is valid. Catches:

          * Missing template-tag declarations (the
            ``syntax error at or near "["`` family — the most common reason
            Metabase questions blow up at query time).
          * Unused / misnamed tags.
          * Database syntax errors (returned by the underlying DB engine).
          * Permission issues (returned by Metabase itself).

        Returns ``"OK: ..."`` on success with row count + first-row preview, or
        a structured error message ready to be acted on.
        """
        parsed_tags, tags_err = _parse_json_arg(template_tags, "template_tags")
        if tags_err:
            return tags_err
        coverage_error = _validate_template_tag_coverage(query, parsed_tags)
        if coverage_error:
            return coverage_error
        parsed_params, params_err = _parse_json_arg(parameters, "parameters")
        if params_err:
            return params_err

        native_payload: dict[str, Any] = {"query": query}
        if parsed_tags is not None:
            native_payload["template-tags"] = parsed_tags

        request_body: dict[str, Any] = {
            "database": database_id,
            "type": "native",
            "native": native_payload,
        }
        if parsed_params is not None:
            request_body["parameters"] = parsed_params

        async with egress_client(timeout=_TIMEOUT_QUERY) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}/api/dataset",
                _PROVIDER,
                "validate SQL query",
                headers=headers,
                json=request_body,
            )
            if err:
                return await _enrich_query_error(err, database_id)
            data = resp.json()
            failure = _check_dataset_failed(data)
            if failure:
                return await _enrich_query_error(failure, database_id)
            row_count = data.get("row_count", len(data.get("data", {}).get("rows", [])))
            cols = [
                c.get("display_name", c.get("name", "?"))
                for c in data.get("data", {}).get("cols", [])
            ]
            running_time = data.get("running_time", "?")
            return (
                f"OK: query is valid. Returned {row_count} row(s) in "
                f"{running_time}ms. Columns: {cols}. Safe to save as a question."
            )

    # ── 6. Metrics & Segments (MBQL building blocks) ───────────────────────

    async def list_metabase_metrics(limit: int = 50) -> str:
        """List Metabase **Metrics** — reusable, named aggregations (e.g.
        "Total Revenue") that can be referenced from MBQL queries via
        ``{"type": "metric", "id": <id>}`` or by passing the metric ID as the
        ``source-table`` of a card.

        On Metabase 0.50+ metrics live in the ``cards`` table with
        ``type='metric'``. There is no server-side ``f=metric`` filter
        so the fallback path must download cards and
        filter client-side. We cap the fallback at *limit* metrics and use
        the query-timeout budget instead of the short read timeout so it
        stays robust on large instances.
        """
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/api/legacy-metric",
                _PROVIDER,
                "list metrics",
                headers=headers,
            )
            if err:
                # Metabase 0.50+ removed `/api/legacy-metric` — fall back to
                # the unified card endpoint. There is currently no server-side
                # "type=metric" filter (`f=` accepts all/mine/bookmarked/
                # archived/database/table/using_model only), so we have to scan
                # client-side. Use _TIMEOUT_QUERY (60s) instead of
                # _TIMEOUT_READ (30s) because instances with many thousands of
                # cards have a measurably slower listing response.
                async with egress_client(
                    timeout=_TIMEOUT_QUERY
                ) as long_client:
                    resp, err2 = await safe_request(
                        long_client,
                        "get",
                        f"{base_url}/api/card",
                        _PROVIDER,
                        "list metrics (card fallback)",
                        headers=headers,
                        params={"f": "all"},
                    )
                    if err2:
                        return err2
                    cards = resp.json()
                metrics = [c for c in cards if c.get("type") == "metric"]
                truncated = len(metrics) > limit
                metrics = metrics[:limit]
                result = format_entity_list(
                    metrics,
                    lambda c: (
                        f"{c.get('name', 'Untitled')} | ID: {c.get('id', '')} "
                        f"| Table: {c.get('table_id', '?')} "
                        f"| Description: {(c.get('description') or 'N/A')[:80]}"
                    ),
                    noun="metric",
                    empty_msg="No metrics found.",
                )
                if truncated:
                    result += (
                        f"\n\n[Note: Metabase 0.50+ has no server-side "
                        f"`type=metric` filter, so the card listing was scanned "
                        f"client-side and capped at {limit} metrics. Pass a "
                        f"higher `limit` if needed.]"
                    )
                return result
            metrics = resp.json()
            return format_entity_list(
                metrics,
                lambda m: (
                    f"{m.get('name', 'Untitled')} | ID: {m.get('id', '')} "
                    f"| Table: {m.get('table_id', '?')} "
                    f"| Description: {(m.get('description') or 'N/A')[:80]}"
                ),
                noun="metric",
                empty_msg="No metrics found.",
            )

    async def read_metabase_metric(metric_id: int) -> str:
        """Read a Metabase **Metric**'s full configuration including its MBQL
        aggregation definition. Use this to discover the metric's queryable
        dimensions before composing an MBQL breakout."""
        used_fallback = False
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/api/legacy-metric/{metric_id}",
                _PROVIDER,
                "read metric",
                headers=headers,
            )
            if err:
                # Fall back to card endpoint (Metabase 0.50+).
                resp, err2 = await safe_request(
                    client,
                    "get",
                    f"{base_url}/api/card/{metric_id}",
                    _PROVIDER,
                    "read metric (card fallback)",
                    headers=headers,
                )
                if err2:
                    return err2
                used_fallback = True
            m = resp.json()
            # When we fell back to /api/card/{id}, the endpoint will happily
            # return any card type. Verify the caller actually pointed us at a
            # metric instead of silently treating a
            # question/model as a metric.
            if used_fallback and m.get("type") != "metric":
                return (
                    f"Card {metric_id} is not a metric — got type="
                    f"{m.get('type') or 'question'!r}. Pass a metric ID "
                    f"(from list_metabase_metrics)."
                )
            definition = m.get("definition") or m.get("dataset_query", {}).get("query")
            return json.dumps(
                {
                    "id": m.get("id"),
                    "name": m.get("name"),
                    "description": m.get("description"),
                    "table_id": m.get("table_id"),
                    "definition": definition,
                },
                indent=2,
                default=str,
            )

    async def list_metabase_segments() -> str:
        """List Metabase **Segments** — reusable, named filter conditions (e.g.
        "Active Users") that can be applied to MBQL queries via
        ``["segment", ID]`` inside a ``filter`` clause."""
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/api/segment",
                _PROVIDER,
                "list segments",
                headers=headers,
            )
            if err:
                return err
            segments = resp.json()
            return format_entity_list(
                segments,
                lambda s: (
                    f"{s.get('name', 'Untitled')} | ID: {s.get('id', '')} "
                    f"| Table: {s.get('table_id', '?')} "
                    f"| Description: {(s.get('description') or 'N/A')[:80]}"
                ),
                noun="segment",
                empty_msg="No segments found.",
            )

    async def read_metabase_segment(segment_id: int) -> str:
        """Read a Metabase **Segment**'s full configuration including its MBQL
        filter definition."""
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/api/segment/{segment_id}",
                _PROVIDER,
                "read segment",
                headers=headers,
            )
            if err:
                return err
            s = resp.json()
            return json.dumps(
                {
                    "id": s.get("id"),
                    "name": s.get("name"),
                    "description": s.get("description"),
                    "table_id": s.get("table_id"),
                    "definition": s.get("definition"),
                },
                indent=2,
                default=str,
            )

    # ── 7. Alerts ──────────────────────────────────────────────────────────

    async def list_metabase_alerts() -> str:
        """List alerts the current user has created or will receive."""
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/api/alert",
                _PROVIDER,
                "list alerts",
                headers=headers,
            )
            if err:
                return err
            alerts = resp.json()
            return format_entity_list(
                alerts,
                lambda a: (
                    f"Alert ID: {a.get('id', '')} "
                    f"| Card: {a.get('card', {}).get('name', '?')} (ID: {a.get('card', {}).get('id', '')}) "
                    f"| Channels: {len(a.get('channels', []))}"
                ),
                noun="alert",
                empty_msg="No alerts found.",
            )

    async def create_metabase_alert(
        card_id: int,
        alert_condition: str = "rows",
        alert_first_only: bool = False,
        schedule_type: str = "hourly",
        schedule_hour: int = 0,
        schedule_day: str = "",
    ) -> str:
        """Create an alert on a saved question.

        alert_condition: 'rows' (when question returns results) or 'goal' (when goal is met).
        schedule_type: 'hourly', 'daily', 'weekly'.
        """
        schedule: dict[str, Any] = {"schedule_type": schedule_type}
        if schedule_type in ("daily", "weekly"):
            schedule["schedule_hour"] = schedule_hour
        if schedule_type == "weekly" and schedule_day:
            schedule["schedule_day"] = schedule_day

        body: dict[str, Any] = {
            "card": {"id": card_id},
            "alert_condition": alert_condition,
            "alert_first_only": alert_first_only,
            "channels": [
                {
                    "channel_type": "email",
                    "enabled": True,
                    "schedule_type": schedule_type,
                }
            ],
        }

        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}/api/alert",
                _PROVIDER,
                "create alert",
                headers=headers,
                json=body,
            )
            if err:
                return err
            alert = resp.json()
            return f"Created alert (ID: {alert.get('id', '')}) on question {card_id} with condition '{alert_condition}'."

    # ── 8. Search & Discovery ──────────────────────────────────────────────

    async def search_metabase(
        query: str,
        models: str = "",
        archived: bool = False,
        table_db_id: int = 0,
        created_by: int = 0,
        collection: int = 0,
    ) -> str:
        """Search across all Metabase content.

        models: comma-separated filter (e.g. 'card,dashboard,collection,table,dataset,metric').
        archived: search archived items only.
        table_db_id: restrict to items from a specific database.
        created_by: restrict to items created by a specific user ID.
        collection: restrict to items in a specific collection ID.
        """
        params: dict[str, Any] = {"q": query}
        if models:
            for m in models.split(","):
                params.setdefault("models", [])
                params["models"].append(m.strip())
        if archived:
            params["archived"] = "true"
        if table_db_id:
            params["table_db_id"] = table_db_id
        if created_by:
            params["created_by"] = created_by
        if collection:
            params["collection"] = collection

        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/api/search",
                _PROVIDER,
                "search Metabase",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()
            results = data.get("data", data) if isinstance(data, dict) else data
            items = results[:30] if isinstance(results, list) else []
            return format_entity_list(
                items,
                lambda r: (
                    f"[{r.get('model', '?').upper()}] {r.get('name', '')} "
                    f"| ID: {r.get('id', '')} "
                    f"| Collection: {r.get('collection', {}).get('name', 'root') if r.get('collection') else 'root'}"
                ),
                noun="result",
                empty_msg=f"No results for '{query}'.",
            )

    async def list_metabase_databases() -> str:
        """List databases configured in Metabase."""
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/api/database",
                _PROVIDER,
                "list databases",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            dbs = data.get("data", data) if isinstance(data, dict) else data
            items = dbs if isinstance(dbs, list) else [dbs]
            return format_entity_list(
                items,
                lambda db: f"{db.get('name', '')} | Engine: {db.get('engine', '?')} | ID: {db.get('id', '')}",
                noun="database",
                empty_msg="No databases configured.",
            )

    # ── Register all tools ─────────────────────────────────────────────────

    executor.register("get_metabase_database_metadata", get_metabase_database_metadata)
    executor.register("list_metabase_database_schemas", list_metabase_database_schemas)
    executor.register("list_metabase_schema_tables", list_metabase_schema_tables)
    executor.register("get_metabase_table_metadata", get_metabase_table_metadata)
    executor.register("list_metabase_questions", list_metabase_questions)
    executor.register("read_metabase_question", read_metabase_question)
    executor.register("read_metabase_question_raw", read_metabase_question_raw)
    executor.register("run_metabase_question", run_metabase_question)
    executor.register("export_metabase_question", export_metabase_question)
    executor.register("create_metabase_question", create_metabase_question)
    executor.register("update_metabase_question", update_metabase_question)
    executor.register("delete_metabase_question", delete_metabase_question)
    executor.register("copy_metabase_question", copy_metabase_question)
    executor.register(
        "list_metabase_question_dashboards", list_metabase_question_dashboards
    )
    executor.register("list_metabase_dashboards", list_metabase_dashboards)
    executor.register("read_metabase_dashboard", read_metabase_dashboard)
    executor.register("create_metabase_dashboard", create_metabase_dashboard)
    executor.register("update_metabase_dashboard", update_metabase_dashboard)
    executor.register("delete_metabase_dashboard", delete_metabase_dashboard)
    executor.register("add_card_to_metabase_dashboard", add_card_to_metabase_dashboard)
    executor.register("copy_metabase_dashboard", copy_metabase_dashboard)
    executor.register(
        "remove_card_from_metabase_dashboard", remove_card_from_metabase_dashboard
    )
    executor.register("add_metabase_dashboard_filter", add_metabase_dashboard_filter)
    executor.register("map_metabase_dashboard_filter", map_metabase_dashboard_filter)
    executor.register("list_metabase_collections", list_metabase_collections)
    executor.register("read_metabase_collection_items", read_metabase_collection_items)
    executor.register("create_metabase_collection", create_metabase_collection)
    executor.register("update_metabase_collection", update_metabase_collection)
    executor.register("run_metabase_query", run_metabase_query)
    executor.register("run_metabase_mbql_query", run_metabase_mbql_query)
    executor.register(
        "validate_metabase_native_query", validate_metabase_native_query
    )
    executor.register("list_metabase_metrics", list_metabase_metrics)
    executor.register("read_metabase_metric", read_metabase_metric)
    executor.register("list_metabase_segments", list_metabase_segments)
    executor.register("read_metabase_segment", read_metabase_segment)
    executor.register("list_metabase_alerts", list_metabase_alerts)
    executor.register("create_metabase_alert", create_metabase_alert)
    executor.register("search_metabase", search_metabase)
    executor.register("list_metabase_databases", list_metabase_databases)

    # ── Tool definitions ───────────────────────────────────────────────────

    return [
        # -- Schema Introspection --
        ToolDefinition(
            name="get_metabase_database_metadata",
            description="Get full metadata for a Metabase database including all tables and their fields with types. Essential for understanding the schema before writing SQL queries.",
            input_schema={
                "type": "object",
                "properties": {
                    "database_id": {
                        "type": "integer",
                        "description": "Database ID (use list_metabase_databases to find it)",
                    },
                },
                "required": ["database_id"],
            },
            source="integration:metabase",
            priority=1,
        ),
        ToolDefinition(
            name="list_metabase_database_schemas",
            description="List schema names in a Metabase database.",
            input_schema={
                "type": "object",
                "properties": {
                    "database_id": {"type": "integer", "description": "Database ID"},
                },
                "required": ["database_id"],
            },
            source="integration:metabase",
            priority=2,
        ),
        ToolDefinition(
            name="list_metabase_schema_tables",
            description="List tables in a specific schema of a Metabase database.",
            input_schema={
                "type": "object",
                "properties": {
                    "database_id": {"type": "integer", "description": "Database ID"},
                    "schema": {
                        "type": "string",
                        "description": "Schema name (e.g. 'public'). Leave empty for the default schema.",
                        "default": "",
                    },
                },
                "required": ["database_id"],
            },
            source="integration:metabase",
            priority=1,
        ),
        ToolDefinition(
            name="get_metabase_table_metadata",
            description="Get detailed metadata for a table: field IDs, names, data types, base types, foreign keys, and semantic types. "
            "Each field shows its Metabase field ID and base_type — use these to construct template-tag dimension references "
            'for field filters: ["field", FIELD_ID, {"base-type": "BASE_TYPE"}]. '
            'With card_id instead, lists a saved question\'s result columns with the field ref an MBQL query over "card__ID" '
            "uses (by column name, not display name).",
            input_schema={
                "type": "object",
                "properties": {
                    "table_id": {
                        "type": "integer",
                        "description": "Table ID (use list_metabase_schema_tables or get_metabase_database_metadata to find it)",
                    },
                    "card_id": {
                        "type": "integer",
                        "description": "Saved question ID, instead of table_id, to list its result columns",
                    },
                },
                "required": [],
            },
            source="integration:metabase",
            priority=0,
        ),
        # -- Questions / Cards --
        ToolDefinition(
            name="list_metabase_questions",
            description="List saved questions (cards) in Metabase.",
            input_schema={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Max questions to return",
                        "default": 20,
                    },
                    "filter": {
                        "type": "string",
                        "description": "Filter: 'all', 'mine', 'bookmarked', 'archived', 'database', 'table'",
                        "default": "all",
                        "enum": [
                            "all",
                            "mine",
                            "bookmarked",
                            "archived",
                            "database",
                            "table",
                        ],
                    },
                },
                "required": [],
            },
            source="integration:metabase",
            priority=1,
        ),
        ToolDefinition(
            name="read_metabase_question",
            description=(
                "Read a question's full configuration including its query, "
                "template tags, display type, visualization settings (number "
                "formatting, column settings, prefixes, compact mode), "
                "description, and collection. Shows template tag details "
                "(type, widget-type, dimension, id) for verifying field "
                "filter setup. The query body lives at the END of the "
                "response inside a fenced code block (```sql / ``` for native "
                "questions, ```json / ``` for MBQL) — when updating, ALWAYS "
                "copy the EXACT bytes between the fences into "
                "update_metabase_question's native_query / mbql_query arg. "
                "Multi-line SQL is safe to round-trip this way. "
                "ALWAYS read this before updating visualization_settings."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "card_id": {"type": "integer", "description": "Question/card ID"},
                },
                "required": ["card_id"],
            },
            source="integration:metabase",
            priority=1,
        ),
        ToolDefinition(
            name="read_metabase_question_raw",
            description="Read a question's raw dataset_query and visualization_settings JSON. "
            "Use this to inspect exactly what Metabase has stored — essential for debugging field filter, "
            "template tag, and number formatting issues. Returns the raw JSON of id, name, dataset_query, "
            "parameters, and visualization_settings.",
            input_schema={
                "type": "object",
                "properties": {
                    "card_id": {"type": "integer", "description": "Question/card ID"},
                },
                "required": ["card_id"],
            },
            source="integration:metabase",
            priority=2,
        ),
        ToolDefinition(
            name="run_metabase_question",
            description=(
                "Run a saved question (a dashboard card runs by its Card ID) and return tabular "
                "results (a preview of the first rows; export_metabase_question stores every row "
                "as a file). Optionally pass filter parameter values as JSON. The first line says "
                "when Metabase computed the result and whether it came from its cache. For Board "
                "or finance figures, run with ignore_cache=true, or report the result's age."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "card_id": {"type": "integer", "description": "Question/card ID"},
                    "parameters": {
                        "type": "string",
                        "description": 'Optional JSON array of filter parameter values, e.g. \'[{"type":"category","value":"US","target":["dimension",["field",42,null]]}]\'',
                        "default": "",
                    },
                    "ignore_cache": {
                        "type": "boolean",
                        "description": "Recompute the result instead of serving Metabase's cached one",
                        "default": False,
                    },
                },
                "required": ["card_id"],
            },
            source="integration:metabase",
            priority=0,
        ),
        ToolDefinition(
            name="export_metabase_question",
            description=(
                "Export a saved question's full results (every row, not the run preview) as a "
                "CSV, XLSX or JSON file into the conversation: a download card, the question's "
                "link and a file_id to forward to any tool that takes a file; the file's "
                "readable content comes with the result. Include the returned [[FILE:...]] "
                "marker verbatim so the user gets a download card."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "card_id": {"type": "integer", "description": "Question/card ID"},
                    "export_format": {
                        "type": "string",
                        "description": "csv (default), xlsx or json",
                        "default": "csv",
                    },
                    "parameters": {
                        "type": "string",
                        "description": "Optional JSON array of filter parameter values, as for run_metabase_question",
                        "default": "",
                    },
                    "format_rows": {
                        "type": "boolean",
                        "description": "Format values as the Metabase UI shows them (default: raw values)",
                        "default": False,
                    },
                    "pivot_results": {
                        "type": "boolean",
                        "description": "Export a pivot table question pivoted",
                        "default": False,
                    },
                },
                "required": ["card_id"],
            },
            source="integration:metabase",
            priority=1,
        ),
        ToolDefinition(
            name="create_metabase_question",
            description=(
                "Create a new saved Metabase question. Two query languages are "
                "supported, mirroring Metabase's GUI:\n"
                "  • **Native (SQL)** — set query_type='native' and provide "
                "native_query. We pre-validate every `{{var}}` and `[[…]]` block "
                "against template_tags before saving, so the agent catches the "
                "`syntax error at or near \"[\"` family before it hits prod.\n"
                "  • **MBQL (structured / GUI builder question)** — set "
                "query_type='query' and provide either `source_table_id` (for a "
                "simple table dump) OR a full `mbql_query` JSON object with "
                "filter / aggregation / breakout / order-by / limit / joins / "
                "expressions. **Prefer MBQL whenever possible** — it avoids the "
                "entire `[[…]]` template-tag bug class, composes safely, and "
                "carries proper Metabase column metadata.\n"
                "Field filter SQL syntax (native only): use WHERE {{var}} (no "
                "column/operator — Metabase generates the SQL). Optional: WHERE "
                "TRUE [[AND {{var}}]]. Multiple: WHERE TRUE [[AND {{v1}}]] "
                "[[AND {{v2}}]]. EVERY `[[…]]` block MUST contain a `{{var}}` and "
                "EVERY `{{var}}` MUST be declared in template_tags. "
                "Use get_metabase_table_metadata for field IDs and base_types."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Question name"},
                    "database_id": {
                        "type": "integer",
                        "description": "Database ID to query against",
                    },
                    "query_type": {
                        "type": "string",
                        "description": (
                            "Query language: 'native' (raw SQL) or 'query' (MBQL — "
                            "structured GUI-builder question)"
                        ),
                        "default": "native",
                        "enum": ["native", "query"],
                    },
                    "native_query": {
                        "type": "string",
                        "description": (
                            "SQL query (when query_type='native'). For field "
                            "filters: WHERE {{var}} (no column/operator) or "
                            "WHERE TRUE [[AND {{var}}]] for optional. For basic "
                            "variables: WHERE column = {{var}} or "
                            "[[WHERE column = {{var}}]] for optional."
                        ),
                        "default": "",
                    },
                    "source_table_id": {
                        "type": "integer",
                        "description": (
                            "Source table ID (when query_type='query' and you do "
                            "not provide a full mbql_query)."
                        ),
                        "default": 0,
                    },
                    "mbql_query": {
                        "type": "string",
                        "description": (
                            "JSON object describing the MBQL query stage (when "
                            "query_type='query'). Forwarded verbatim into the "
                            "question's dataset_query.query. Example: "
                            "'{\"source-table\":42,"
                            "\"filter\":[\">\",[\"field\",302,null],100],"
                            "\"aggregation\":[[\"sum\",[\"field\",302,null]]],"
                            "\"breakout\":[[\"field\",305,{\"temporal-unit\":\"month\"}]]}'. "
                            "Use list_metabase_metrics and list_metabase_segments "
                            "to discover reusable building blocks."
                        ),
                        "default": "",
                    },
                    "display": {
                        "type": "string",
                        "description": "Visualization type: 'table', 'bar', 'line', 'pie', 'area', 'row', 'scalar', 'funnel', 'map'",
                        "default": "table",
                    },
                    "description": {
                        "type": "string",
                        "description": "Question description",
                        "default": "",
                    },
                    "collection_id": {
                        "type": "integer",
                        "description": "Collection ID to save into (0 for root)",
                        "default": 0,
                    },
                    "card_type": {
                        "type": "string",
                        "description": "Card type: 'question' (default), 'model', or 'metric'",
                        "default": "question",
                        "enum": ["question", "model", "metric"],
                    },
                    "template_tags": {
                        "type": "string",
                        "description": "JSON string of template tag definitions for native queries with {{variable}} syntax. "
                        "Required for field filters (dashboard-level filtering). "
                        "Each tag needs: name, type ('dimension' for field filters, 'text'/'number'/'date' for simple variables), id (any UUID). "
                        "For field filters (type='dimension'), also provide: dimension (['field', field_id, {'base-type': 'type/...'}]), "
                        "widget-type ('date/all-options', 'date/range', 'string/=', etc.), display-name. "
                        'Example: \'{"period":{"type":"dimension","name":"period","id":"a1b2c3",'
                        '"display-name":"Period","dimension":["field",123,{"base-type":"type/DateTimeWithLocalTZ"}],'
                        '"widget-type":"date/all-options"}}\'',
                        "default": "",
                    },
                    "visualization_settings": {
                        "type": "string",
                        "description": (
                            "Optional JSON object of visualization settings stored "
                            "on the card (number formatting, axis labels, colors). "
                            "Defaults to {}."
                        ),
                        "default": "",
                    },
                },
                "required": ["name", "database_id"],
            },
            source="integration:metabase",
            priority=0,
        ),
        ToolDefinition(
            name="update_metabase_question",
            description=(
                "Update an existing Metabase question in-place. ALWAYS prefer "
                "this over deleting + recreating — it preserves the card ID, "
                "dashboard references, alerts, and history.\n"
                "Two query languages are supported (mirroring "
                "create_metabase_question):\n"
                "  • **Native SQL** — pass native_query and/or template_tags. "
                "template_tags can be used alone (existing SQL preserved) or "
                "with native_query. Existing tags are merged (provided tags "
                "override). We pre-validate the SQL + template-tags pair so "
                "you cannot save a broken question that triggers `syntax error "
                "at or near \"[\"` at query time, AND empty/whitespace-only "
                "resulting SQL is refused so a malformed update never wipes the "
                "question body. **template_tags only applies to native SQL "
                "questions — calling it on an MBQL question without "
                "native_query is rejected** (use mbql_query for structured "
                "questions, or map_metabase_dashboard_filter to bind a "
                "dashboard filter to an MBQL field).\n"
                "  • **MBQL** — pass mbql_query to FULLY REPLACE the "
                "question's structured query stage (filter / aggregation / "
                "breakout / order-by / limit / joins / expressions). Switches "
                "the question to type='query' automatically. To change only "
                "one clause, fetch the existing MBQL with "
                "read_metabase_question_raw, patch it locally, and pass the "
                "complete updated query — there is no partial-merge mode. "
                "**Prefer MBQL whenever possible — it avoids the `[[…]]` / "
                "template-tag class of bugs entirely.**\n"
                "visualization_settings are MERGED with existing settings — "
                "only the keys you provide are updated, others are preserved. "
                "For column_settings, nested column keys are also merged."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "card_id": {
                        "type": "integer",
                        "description": "Question/card ID to update",
                    },
                    "name": {
                        "type": "string",
                        "description": "New name",
                        "default": "",
                    },
                    "description": {
                        "type": "string",
                        "description": "New description",
                        "default": "",
                    },
                    "display": {
                        "type": "string",
                        "description": "New display type",
                        "default": "",
                    },
                    "collection_id": {
                        "type": "integer",
                        "description": "Move to collection ID",
                        "default": 0,
                    },
                    "native_query": {
                        "type": "string",
                        "description": "New SQL query (only for native questions). "
                        "For field filters: use WHERE {{var}} (no column/operator) or WHERE TRUE [[AND {{var}}]] for optional. "
                        "For basic variables: use WHERE column = {{var}} or [[WHERE column = {{var}}]] for optional.",
                        "default": "",
                    },
                    "mbql_query": {
                        "type": "string",
                        "description": (
                            "FULL replacement MBQL query stage (JSON object). "
                            "Unlike template_tags / visualization_settings — "
                            "which are merged with the existing values — "
                            "mbql_query REPLACES the entire query stage. If "
                            "you only need to change one clause, fetch the "
                            "current MBQL via read_metabase_question_raw, "
                            "patch it locally, and pass the complete updated "
                            "query back. Switches the question to type='query' "
                            "automatically. Mutually exclusive with "
                            "native_query. Example: '{\"source-table\":42,"
                            "\"aggregation\":[[\"count\"]],"
                            "\"breakout\":[[\"field\",101,null]]}'."
                        ),
                        "default": "",
                    },
                    "template_tags": {
                        "type": "string",
                        "description": "JSON string of template tag definitions to add/update on the question. "
                        "Merged with existing tags (new tags are added, existing tags with the same name are overwritten). "
                        "Can be used with or without native_query — when used alone, only the tags are updated (SQL is preserved). "
                        "For field filters (type='dimension'): enables dashboard-level filtering (e.g. date range pickers). "
                        "Each tag needs: name, type, id (UUID). For dimension type, also: dimension, widget-type, display-name. "
                        "Use get_metabase_table_metadata to find field IDs and base_types for the dimension array. "
                        'Example: \'{"period":{"type":"dimension","name":"period","id":"a1b2c3",'
                        '"display-name":"Period","dimension":["field",123,{"base-type":"type/DateTimeWithLocalTZ"}],'
                        '"widget-type":"date/all-options"}}\'',
                        "default": "",
                    },
                    "archived": {
                        "type": "boolean",
                        "description": "Set to true to archive the question",
                        "default": False,
                    },
                    "card_type": {
                        "type": "string",
                        "description": "Change card type: 'question', 'model', or 'metric'",
                        "default": "",
                    },
                    "visualization_settings": {
                        "type": "string",
                        "description": "JSON string of visualization settings to MERGE into existing settings. "
                        "Only the keys you provide are updated — all other existing keys are preserved. "
                        "For column_settings, nested column keys are also merged (not replaced). "
                        "Common keys for scalar/number questions: "
                        '"scalar.field" (column name to display), '
                        '"scalar.switch_positive_negative" (bool), '
                        '"number.prefix" (e.g. "€", "$"), '
                        '"number.suffix" (e.g. "%"), '
                        '"number.style" ("decimal", "percent", "scientific", "currency"), '
                        '"number.compact" (bool — shows K, M, B suffixes), '
                        '"number.decimals" (int — decimal places), '
                        '"number.separator_style" (".", ",", "'
                        '"). '
                        "For column-level formatting in tables/charts, use column_settings with column ref keys: "
                        '\'{"column_settings":{"[\\"name\\",\\"AMOUNT\\"]":{"number_style":"currency","currency":"EUR","compact":true}}}\'',
                        "default": "",
                    },
                },
                "required": ["card_id"],
            },
            source="integration:metabase",
            priority=1,
        ),
        ToolDefinition(
            name="delete_metabase_question",
            description="Permanently delete a saved question. WARNING: this breaks dashboard references and alerts. Use update_metabase_question instead if you just need to modify the question.",
            input_schema={
                "type": "object",
                "properties": {
                    "card_id": {
                        "type": "integer",
                        "description": "Question/card ID to delete",
                    },
                },
                "required": ["card_id"],
            },
            source="integration:metabase",
            priority=2,
        ),
        ToolDefinition(
            name="copy_metabase_question",
            description="Duplicate a saved question, optionally into a different collection.",
            input_schema={
                "type": "object",
                "properties": {
                    "card_id": {
                        "type": "integer",
                        "description": "Question/card ID to copy",
                    },
                    "collection_id": {
                        "type": "integer",
                        "description": "Target collection ID (0 to keep in same collection)",
                        "default": 0,
                    },
                },
                "required": ["card_id"],
            },
            source="integration:metabase",
            priority=2,
        ),
        ToolDefinition(
            name="list_metabase_question_dashboards",
            description="List all dashboards that contain a given question.",
            input_schema={
                "type": "object",
                "properties": {
                    "card_id": {"type": "integer", "description": "Question/card ID"},
                },
                "required": ["card_id"],
            },
            source="integration:metabase",
            priority=2,
        ),
        # -- Dashboards --
        ToolDefinition(
            name="list_metabase_dashboards",
            description="List dashboards in Metabase.",
            input_schema={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Max dashboards to return",
                        "default": 20,
                    },
                },
                "required": [],
            },
            source="integration:metabase",
            priority=1,
        ),
        ToolDefinition(
            name="read_metabase_dashboard",
            description="Read a dashboard's details including all its cards/questions with their positions and sizes.",
            input_schema={
                "type": "object",
                "properties": {
                    "dashboard_id": {"type": "integer", "description": "Dashboard ID"},
                },
                "required": ["dashboard_id"],
            },
            source="integration:metabase",
            priority=1,
        ),
        ToolDefinition(
            name="create_metabase_dashboard",
            description="Create a new empty dashboard in Metabase.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Dashboard name"},
                    "description": {
                        "type": "string",
                        "description": "Dashboard description",
                        "default": "",
                    },
                    "collection_id": {
                        "type": "integer",
                        "description": "Collection ID to create in (0 for root)",
                        "default": 0,
                    },
                },
                "required": ["name"],
            },
            source="integration:metabase",
            priority=0,
        ),
        ToolDefinition(
            name="update_metabase_dashboard",
            description="Update a dashboard's name, description, collection, layout width, or cache TTL.",
            input_schema={
                "type": "object",
                "properties": {
                    "dashboard_id": {
                        "type": "integer",
                        "description": "Dashboard ID to update",
                    },
                    "name": {
                        "type": "string",
                        "description": "New name",
                        "default": "",
                    },
                    "description": {
                        "type": "string",
                        "description": "New description",
                        "default": "",
                    },
                    "collection_id": {
                        "type": "integer",
                        "description": "Move to collection ID",
                        "default": 0,
                    },
                    "archived": {
                        "type": "boolean",
                        "description": "Set to true to archive",
                        "default": False,
                    },
                    "width": {
                        "type": "string",
                        "description": "Dashboard layout width: 'fixed' or 'full'",
                        "default": "",
                        "enum": ["", "fixed", "full"],
                    },
                    "cache_ttl": {
                        "type": "integer",
                        "description": "Cache TTL in seconds (0 to skip)",
                        "default": 0,
                    },
                },
                "required": ["dashboard_id"],
            },
            source="integration:metabase",
            priority=1,
        ),
        ToolDefinition(
            name="delete_metabase_dashboard",
            description="Permanently delete a dashboard.",
            input_schema={
                "type": "object",
                "properties": {
                    "dashboard_id": {
                        "type": "integer",
                        "description": "Dashboard ID to delete",
                    },
                },
                "required": ["dashboard_id"],
            },
            source="integration:metabase",
            priority=2,
        ),
        ToolDefinition(
            name="add_card_to_metabase_dashboard",
            description="Add a saved question (card) to a dashboard at a specific position and size. IMPORTANT: call this tool ONE AT A TIME per dashboard (not in parallel) because each call reads and rewrites the full card list. Use read_metabase_dashboard first to see existing card positions.",
            input_schema={
                "type": "object",
                "properties": {
                    "dashboard_id": {"type": "integer", "description": "Dashboard ID"},
                    "card_id": {
                        "type": "integer",
                        "description": "Question/card ID to add",
                    },
                    "row": {
                        "type": "integer",
                        "description": "Row position (0-based grid)",
                        "default": 0,
                    },
                    "col": {
                        "type": "integer",
                        "description": "Column position (0-based, max 17)",
                        "default": 0,
                    },
                    "size_x": {
                        "type": "integer",
                        "description": "Width in grid units (1-18)",
                        "default": 6,
                    },
                    "size_y": {
                        "type": "integer",
                        "description": "Height in grid units",
                        "default": 4,
                    },
                    "dashboard_tab_id": {
                        "type": "integer",
                        "description": "Tab ID to add the card to. Required when the dashboard has tabs (omit or set 0 to default to the first tab). Use read_metabase_dashboard to list tab IDs.",
                        "default": 0,
                    },
                },
                "required": ["dashboard_id", "card_id"],
            },
            source="integration:metabase",
            priority=0,
        ),
        ToolDefinition(
            name="copy_metabase_dashboard",
            description="Duplicate a dashboard. deep_copy=True also copies the underlying questions.",
            input_schema={
                "type": "object",
                "properties": {
                    "dashboard_id": {
                        "type": "integer",
                        "description": "Dashboard ID to copy",
                    },
                    "name": {
                        "type": "string",
                        "description": "Name for the copy (blank keeps original name with 'Copy of' prefix)",
                        "default": "",
                    },
                    "collection_id": {
                        "type": "integer",
                        "description": "Target collection ID (0 to keep in same collection)",
                        "default": 0,
                    },
                    "deep_copy": {
                        "type": "boolean",
                        "description": "Also copy the underlying questions (true) or reference originals (false)",
                        "default": True,
                    },
                },
                "required": ["dashboard_id"],
            },
            source="integration:metabase",
            priority=2,
        ),
        ToolDefinition(
            name="remove_card_from_metabase_dashboard",
            description="Remove a card from a dashboard by its dashcard ID. IMPORTANT: call this tool ONE AT A TIME per dashboard (not in parallel) because each call reads and rewrites the full card list. Use read_metabase_dashboard to find dashcard IDs.",
            input_schema={
                "type": "object",
                "properties": {
                    "dashboard_id": {
                        "type": "integer",
                        "description": "Dashboard ID",
                    },
                    "dashcard_id": {
                        "type": "integer",
                        "description": "DashCard ID to remove (from read_metabase_dashboard output, not the card/question ID)",
                    },
                },
                "required": ["dashboard_id", "dashcard_id"],
            },
            source="integration:metabase",
            priority=1,
        ),
        ToolDefinition(
            name="add_metabase_dashboard_filter",
            description="Add a filter parameter to a dashboard. This creates the filter definition itself. After creating the filter, use map_metabase_dashboard_filter to wire it to specific cards. IMPORTANT: call ONE AT A TIME per dashboard.",
            input_schema={
                "type": "object",
                "properties": {
                    "dashboard_id": {
                        "type": "integer",
                        "description": "Dashboard ID",
                    },
                    "name": {
                        "type": "string",
                        "description": "Display name for the filter (e.g. 'Date Range', 'Category')",
                    },
                    "slug": {
                        "type": "string",
                        "description": "URL slug for the parameter (e.g. 'date_range', 'category')",
                    },
                    "filter_type": {
                        "type": "string",
                        "description": "Parameter type: 'date/single', 'date/range', 'date/relative', 'date/month-year', 'string/=', 'string/contains', 'number/=', 'number/between', 'category', 'id', 'location/city', 'location/state', 'location/country'",
                    },
                    "parameter_id": {
                        "type": "string",
                        "description": "Custom parameter ID (defaults to slug if not provided)",
                        "default": "",
                    },
                },
                "required": ["dashboard_id", "name", "slug", "filter_type"],
            },
            source="integration:metabase",
            priority=0,
        ),
        ToolDefinition(
            name="map_metabase_dashboard_filter",
            description="Connect a dashboard-level filter (parameter) to a specific card on that dashboard. This wires the filter so it actually affects the card's query results. IMPORTANT: call this tool ONE AT A TIME per dashboard (not in parallel) because each call reads and rewrites the full card list. Use read_metabase_dashboard first to find parameter IDs and dashcard IDs, then get_metabase_table_metadata to find the target field ID.",
            input_schema={
                "type": "object",
                "properties": {
                    "dashboard_id": {
                        "type": "integer",
                        "description": "Dashboard ID",
                    },
                    "dashcard_id": {
                        "type": "integer",
                        "description": "DashCard ID (the card instance on the dashboard, from read_metabase_dashboard output)",
                    },
                    "parameter_id": {
                        "type": "string",
                        "description": "Dashboard parameter/filter ID (from the dashboard's 'parameters' list)",
                    },
                    "card_id": {
                        "type": "integer",
                        "description": "Question/card ID that the filter should affect",
                    },
                    "target_type": {
                        "type": "string",
                        "description": "Mapping type: 'dimension' (map to a DB field, most common) or 'variable' (map to a native query template tag)",
                        "default": "dimension",
                        "enum": ["dimension", "variable"],
                    },
                    "target_field_id": {
                        "type": "integer",
                        "description": "Field ID to filter on (required when target_type='dimension'). Use get_metabase_table_metadata to find field IDs.",
                        "default": 0,
                    },
                    "target_variable": {
                        "type": "string",
                        "description": "Template tag variable name (required when target_type='variable', for native SQL questions with {{variable}} syntax)",
                        "default": "",
                    },
                },
                "required": ["dashboard_id", "dashcard_id", "parameter_id", "card_id"],
            },
            source="integration:metabase",
            priority=0,
        ),
        # -- Collections --
        ToolDefinition(
            name="list_metabase_collections",
            description="List collections (folders) in Metabase. Set tree=True for a hierarchical view.",
            input_schema={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Max collections to return (flat mode)",
                        "default": 30,
                    },
                    "tree": {
                        "type": "boolean",
                        "description": "Return hierarchical tree view",
                        "default": False,
                    },
                },
                "required": [],
            },
            source="integration:metabase",
            priority=2,
        ),
        ToolDefinition(
            name="read_metabase_collection_items",
            description="Browse items inside a collection (questions, dashboards, sub-collections). Use collection_id=0 for the root collection.",
            input_schema={
                "type": "object",
                "properties": {
                    "collection_id": {
                        "type": "integer",
                        "description": "Collection ID (0 for root)",
                        "default": 0,
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max items to return",
                        "default": 30,
                    },
                    "models": {
                        "type": "string",
                        "description": "Comma-separated model filter (e.g. 'card,dashboard,collection')",
                        "default": "",
                    },
                },
                "required": [],
            },
            source="integration:metabase",
            priority=1,
        ),
        ToolDefinition(
            name="create_metabase_collection",
            description="Create a new collection (folder) in Metabase.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Collection name"},
                    "description": {
                        "type": "string",
                        "description": "Collection description",
                        "default": "",
                    },
                    "parent_id": {
                        "type": "integer",
                        "description": "Parent collection ID (0 for root)",
                        "default": 0,
                    },
                },
                "required": ["name"],
            },
            source="integration:metabase",
            priority=1,
        ),
        ToolDefinition(
            name="update_metabase_collection",
            description="Update a collection's name, description, or parent.",
            input_schema={
                "type": "object",
                "properties": {
                    "collection_id": {
                        "type": "integer",
                        "description": "Collection ID to update",
                    },
                    "name": {
                        "type": "string",
                        "description": "New name",
                        "default": "",
                    },
                    "description": {
                        "type": "string",
                        "description": "New description",
                        "default": "",
                    },
                    "parent_id": {
                        "type": "integer",
                        "description": "Move to parent collection ID",
                        "default": 0,
                    },
                    "archived": {
                        "type": "boolean",
                        "description": "Set to true to archive",
                        "default": False,
                    },
                },
                "required": ["collection_id"],
            },
            source="integration:metabase",
            priority=2,
        ),
        # -- Data Querying --
        ToolDefinition(
            name="run_metabase_query",
            description=(
                "Run a native SQL query against a Metabase database. Use "
                "get_metabase_table_metadata or get_metabase_database_metadata first "
                "to understand the schema. Supports parameterized queries with "
                "{{variable}} syntax via template_tags. "
                "IMPORTANT: every `{{var}}` in the SQL must have a matching entry in "
                "`template_tags` and every `[[...]]` optional clause must contain at "
                "least one `{{var}}` — otherwise Metabase forwards the literal `[[]]` "
                "to the database and you get `syntax error at or near \"[\"`. We "
                "pre-validate before sending. **Prefer `run_metabase_mbql_query` over "
                "raw SQL whenever a structured query is sufficient — it avoids this "
                "entire class of bug and composes safely.** "
                "When a query fails with 'column X does not exist', the error response "
                "automatically includes a bounded list of available columns (up to "
                f"{_SCHEMA_HINT_MAX_TABLES} tables, {_SCHEMA_HINT_MAX_COLUMNS_PER_TABLE} "
                "columns each) so you can correct the SQL immediately; call "
                "get_metabase_database_metadata if a table you need is not listed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "database_id": {
                        "type": "integer",
                        "description": "Database ID (use list_metabase_databases to find it)",
                    },
                    "query": {
                        "type": "string",
                        "description": "SQL query to execute (supports {{variable}} template tag syntax)",
                    },
                    "max_rows": {
                        "type": "integer",
                        "description": "Maximum rows to return in results",
                        "default": _MAX_ROWS,
                    },
                    "template_tags": {
                        "type": "string",
                        "description": 'Optional JSON object of template tag definitions for parameterized queries, e.g. \'{"date":{"type":"date","name":"date","id":"abc","display-name":"Date"}}\'',
                        "default": "",
                    },
                    "parameters": {
                        "type": "string",
                        "description": (
                            "Optional JSON array of parameter values to bind to the "
                            "template tags, e.g. "
                            "'[{\"type\":\"date/single\",\"value\":\"2026-01-01\","
                            "\"target\":[\"variable\",[\"template-tag\",\"date\"]]}]'"
                        ),
                        "default": "",
                    },
                },
                "required": ["database_id", "query"],
            },
            source="integration:metabase",
            priority=0,
        ),
        ToolDefinition(
            name="run_metabase_mbql_query",
            description=(
                "Run a structured **MBQL** query (Metabase Query Language) against a "
                "Metabase database — the same query format the GUI builder produces "
                "and the official Metabase MCP server uses internally. **Prefer this "
                "over run_metabase_query** whenever the question can be expressed "
                "structurally: MBQL avoids the `[[…]]` / template-tag class of bugs, "
                "result columns carry full Metabase metadata (semantic types, FKs), "
                "and composition is safe. "
                "Use get_metabase_table_metadata to find field IDs first (card_id "
                "for a card__ID source, whose columns are referenced by name). "
                "MBQL operators inside `filter` clauses: `=`, `!=`, `<`, `<=`, `>`, "
                "`>=`, `between`, `and`, `or`, `not`, `is-null`, `not-null`, "
                "`is-empty`, `not-empty`, `contains`, `does-not-contain`, "
                "`starts-with`, `ends-with`, `time-interval`, `segment`. "
                "Aggregation operators inside `aggregation`: `count`, `sum`, `avg`, "
                "`min`, `max`, `distinct`, `median`, `stddev`, `var`, `percentile`, "
                "`count-where`, `sum-where`, `share`, `cum-count`, `cum-sum`. "
                "Field refs are `[\"field\", FIELD_ID, null]` or with options like "
                "`{\"temporal-unit\": \"month\"}`."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "database_id": {
                        "type": "integer",
                        "description": "Database ID (use list_metabase_databases to find it)",
                    },
                    "mbql_query": {
                        "type": "string",
                        "description": (
                            "JSON object describing the MBQL query stage. Example: "
                            "'{\"source-table\":42,"
                            "\"filter\":[\">\",[\"field\",302,null],100],"
                            "\"aggregation\":[[\"sum\",[\"field\",302,null]]],"
                            "\"breakout\":[[\"field\",305,{\"temporal-unit\":\"month\"}]],"
                            "\"order-by\":[[\"desc\",[\"aggregation\",0]]],"
                            "\"limit\":100}'. "
                            "Sources: `\"source-table\":INT`, `\"source-table\":"
                            "\"card__ID\"`, or `\"source-query\":{...}`."
                        ),
                    },
                    "max_rows": {
                        "type": "integer",
                        "description": "Maximum rows to return in results",
                        "default": _MAX_ROWS,
                    },
                    "parameters": {
                        "type": "string",
                        "description": (
                            "Optional JSON array of parameter values (rarely needed "
                            "for MBQL — filter values are baked into the query)."
                        ),
                        "default": "",
                    },
                },
                "required": ["database_id", "mbql_query"],
            },
            source="integration:metabase",
            priority=0,
        ),
        ToolDefinition(
            name="validate_metabase_native_query",
            description=(
                "Dry-run a native SQL query against a Metabase database without "
                "persisting it as a question. Use this BEFORE create_metabase_question "
                "to confirm the SQL + template_tags pair is valid — it catches the "
                "`syntax error at or near \"[\"` family (missing tag declarations, "
                "bare `[[…]]` blocks, mistyped tag names) plus database-level syntax "
                "errors and permission issues. Returns 'OK: ...' with row count and "
                "column list on success, or a structured error message."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "database_id": {
                        "type": "integer",
                        "description": "Database ID to validate against",
                    },
                    "query": {
                        "type": "string",
                        "description": "SQL query to validate",
                    },
                    "template_tags": {
                        "type": "string",
                        "description": "Optional JSON object of template tag definitions",
                        "default": "",
                    },
                    "parameters": {
                        "type": "string",
                        "description": "Optional JSON array of parameter values",
                        "default": "",
                    },
                },
                "required": ["database_id", "query"],
            },
            source="integration:metabase",
            priority=1,
        ),
        # -- Metrics & Segments (MBQL building blocks) --
        ToolDefinition(
            name="list_metabase_metrics",
            description=(
                "List Metabase **Metrics** — reusable, named aggregations (e.g. "
                "'Total Revenue', 'Active Users') that can be referenced from MBQL "
                "queries. Use these to compose dashboards with consistent KPI "
                "definitions instead of re-implementing the same aggregation in many "
                "questions. On Metabase 0.50+ metrics live in the `cards` table with "
                "type='metric'; since there is no server-side filter for that, the "
                "fallback scans cards client-side and caps the result list at "
                "`limit` (default 50). Bump `limit` if your instance has more "
                "metrics."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Max metrics to return when falling back to the card "
                            "listing on Metabase 0.50+. Default 50."
                        ),
                        "default": 50,
                    },
                },
                "required": [],
            },
            source="integration:metabase",
            priority=1,
        ),
        ToolDefinition(
            name="read_metabase_metric",
            description=(
                "Read a Metabase **Metric**'s full configuration including its MBQL "
                "aggregation definition and table/dimension references. Use this to "
                "discover the metric's queryable dimensions before composing an "
                "MBQL breakout."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "metric_id": {
                        "type": "integer",
                        "description": "Metric ID (from list_metabase_metrics)",
                    },
                },
                "required": ["metric_id"],
            },
            source="integration:metabase",
            priority=2,
        ),
        ToolDefinition(
            name="list_metabase_segments",
            description=(
                "List Metabase **Segments** — reusable, named filter conditions "
                "(e.g. 'Active Users', 'EU Customers') that can be applied to MBQL "
                "queries via `[\"segment\", ID]` inside a `filter` clause."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
            },
            source="integration:metabase",
            priority=2,
        ),
        ToolDefinition(
            name="read_metabase_segment",
            description=(
                "Read a Metabase **Segment**'s full configuration including its MBQL "
                "filter definition."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "segment_id": {
                        "type": "integer",
                        "description": "Segment ID (from list_metabase_segments)",
                    },
                },
                "required": ["segment_id"],
            },
            source="integration:metabase",
            priority=2,
        ),
        # -- Alerts --
        ToolDefinition(
            name="list_metabase_alerts",
            description="List alerts the current API key user has created or will receive.",
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
            },
            source="integration:metabase",
            priority=2,
        ),
        ToolDefinition(
            name="create_metabase_alert",
            description="Create an alert on a saved question. Triggers when the question returns results ('rows') or meets a goal ('goal').",
            input_schema={
                "type": "object",
                "properties": {
                    "card_id": {
                        "type": "integer",
                        "description": "Question/card ID to alert on",
                    },
                    "alert_condition": {
                        "type": "string",
                        "description": "Condition: 'rows' (has results) or 'goal' (meets goal)",
                        "default": "rows",
                        "enum": ["rows", "goal"],
                    },
                    "alert_first_only": {
                        "type": "boolean",
                        "description": "Only alert the first time condition is met",
                        "default": False,
                    },
                    "schedule_type": {
                        "type": "string",
                        "description": "Check frequency: 'hourly', 'daily', 'weekly'",
                        "default": "hourly",
                        "enum": ["hourly", "daily", "weekly"],
                    },
                    "schedule_hour": {
                        "type": "integer",
                        "description": "Hour to check (0-23, for daily/weekly)",
                        "default": 0,
                    },
                    "schedule_day": {
                        "type": "string",
                        "description": "Day to check (for weekly: 'mon','tue','wed','thu','fri','sat','sun')",
                        "default": "",
                    },
                },
                "required": ["card_id"],
            },
            source="integration:metabase",
            priority=2,
        ),
        # -- Search & Discovery --
        ToolDefinition(
            name="search_metabase",
            description="Search across all Metabase content (dashboards, questions, collections, tables, models, metrics).",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search text"},
                    "models": {
                        "type": "string",
                        "description": "Comma-separated model filter (e.g. 'card,dashboard,collection,table,dataset,metric')",
                        "default": "",
                    },
                    "archived": {
                        "type": "boolean",
                        "description": "Search archived items only",
                        "default": False,
                    },
                    "table_db_id": {
                        "type": "integer",
                        "description": "Restrict to items from a specific database ID",
                        "default": 0,
                    },
                    "created_by": {
                        "type": "integer",
                        "description": "Restrict to items created by a specific user ID",
                        "default": 0,
                    },
                    "collection": {
                        "type": "integer",
                        "description": "Restrict to items in a specific collection ID",
                        "default": 0,
                    },
                },
                "required": ["query"],
            },
            source="integration:metabase",
            priority=0,
        ),
        ToolDefinition(
            name="list_metabase_databases",
            description="List databases configured in Metabase.",
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
            },
            source="integration:metabase",
            priority=2,
        ),
    ]
