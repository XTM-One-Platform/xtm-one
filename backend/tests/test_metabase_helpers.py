"""Unit tests for the Metabase integration helpers.

These pure functions are the safety net for the `[[…]]` / template-tag class
of bugs that the integration kept hitting in prod. If a regex
or scanner change breaks them, this test module catches it.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.integrations.providers.metabase import (
    _SCHEMA_HINT_MAX_COLUMNS_PER_TABLE,
    _SCHEMA_HINT_MAX_TABLES,
    _card_source_id,
    _check_dataset_failed,
    _explain_sql_bracket_error,
    _extract_optional_clauses,
    _format_age,
    _is_column_not_found_error,
    _missing_column,
    _normalise_mbql,
    _parse_json_arg,
    _result_freshness,
    _validate_template_tag_coverage,
    build_tools,
)


def _mock_response(payload: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def _get_registered_fn(executor_mock: MagicMock, name: str):
    for call in executor_mock.register.call_args_list:
        if call[0][0] == name:
            return call[0][1]
    raise KeyError(f"Tool {name!r} not registered")

# ---------------------------------------------------------------------------
# _parse_json_arg
# ---------------------------------------------------------------------------


def test_parse_json_arg_empty_returns_none_none():
    """Empty string should mean 'not provided' (not 'null')."""
    val, err = _parse_json_arg("", "template_tags")
    assert val is None
    assert err is None


def test_parse_json_arg_valid_object():
    val, err = _parse_json_arg('{"a": 1, "b": [2, 3]}', "mbql_query")
    assert val == {"a": 1, "b": [2, 3]}
    assert err is None


def test_parse_json_arg_invalid_returns_error():
    val, err = _parse_json_arg("not-json", "template_tags")
    assert val is None
    assert err is not None
    assert "Invalid JSON in template_tags" in err
    # Position information should be included for debugging.
    assert "line 1" in err and "col" in err


def test_parse_json_arg_explicit_null_string():
    """`'null'` IS valid JSON and parses to Python None — distinct from empty."""
    val, err = _parse_json_arg("null", "field")
    assert val is None
    assert err is None  # parse succeeded; ambiguity documented in helper docstring


def test_parse_json_arg_truncates_long_input():
    long_payload = "{" + "x" * 500
    val, err = _parse_json_arg(long_payload, "viz")
    assert val is None and err
    # Truncated to 200 chars + ellipsis.
    assert "..." in err


# ---------------------------------------------------------------------------
# _extract_optional_clauses (stack-based, nesting-aware)
# ---------------------------------------------------------------------------


def test_extract_optional_clauses_flat():
    bodies = _extract_optional_clauses("WHERE [[AND a = {{a}}]] AND [[b = {{b}}]]")
    assert bodies == ["AND a = {{a}}", "b = {{b}}"]


def test_extract_optional_clauses_nested():
    """Nested `[[...]]` is handled — the outer body contains the inner block."""
    sql = "WHERE TRUE [[AND col1 = {{v1}} [[AND col2 = {{v2}}]]]]"
    bodies = _extract_optional_clauses(sql)
    # Only the OUTER body is returned (nested ones are included in the parent).
    assert len(bodies) == 1
    assert "{{v1}}" in bodies[0] and "{{v2}}" in bodies[0]


def test_extract_optional_clauses_unmatched_open():
    """Unmatched `[[` should not crash — just yields no bodies for the dangling part."""
    bodies = _extract_optional_clauses("SELECT [[col FROM t")
    assert bodies == []


def test_extract_optional_clauses_unmatched_close():
    """Unmatched `]]` should be ignored."""
    bodies = _extract_optional_clauses("SELECT col FROM t]] WHERE x = 1")
    assert bodies == []


def test_extract_optional_clauses_empty_sql():
    assert _extract_optional_clauses("") == []
    assert _extract_optional_clauses("SELECT 1") == []


# ---------------------------------------------------------------------------
# _validate_template_tag_coverage
# ---------------------------------------------------------------------------


def test_validate_passes_when_tags_match():
    """Valid SQL + matching template tags returns None."""
    err = _validate_template_tag_coverage(
        "SELECT * FROM t WHERE TRUE [[AND status = {{status}}]]",
        {"status": {"type": "text", "name": "status", "id": "abc"}},
    )
    assert err is None


def test_validate_flags_undeclared_template_tag():
    err = _validate_template_tag_coverage(
        "SELECT * FROM t WHERE id = {{user_id}}", {}
    )
    assert err is not None
    assert "user_id" in err
    assert "NOT declared" in err


def test_validate_flags_bare_optional_clause():
    """`[[…]]` without any `{{var}}` is the smoking gun for the bracket error."""
    err = _validate_template_tag_coverage(
        "SELECT * FROM t WHERE TRUE [[AND status = 'active']]", None
    )
    assert err is not None
    assert "leak" in err
    assert "`[[" in err


def test_validate_does_not_block_on_unused_template_tag(caplog):
    """Declared but unreferenced tags are HARMLESS (Metabase ignores them).
    Blocking on them was preventing the canonical "declare a dimension tag
    THEN wire its `[[AND {{var}}]]` clause" workflow — the validator now
    logs the orphan tags as info and returns None."""
    import logging

    with caplog.at_level(logging.INFO, logger="app.integrations.providers.metabase"):
        err = _validate_template_tag_coverage(
            "SELECT * FROM t",
            {"orphan": {"type": "text", "name": "orphan", "id": "x"}},
        )
    assert err is None
    # The orphan tag is still logged so it's discoverable in debugging.
    assert any("orphan" in record.message for record in caplog.records)


def test_validate_empty_sql_returns_none():
    """Empty SQL is a no-op for the validator (the caller handles that case)."""
    assert _validate_template_tag_coverage("", None) is None
    assert _validate_template_tag_coverage("", {"x": {}}) is None


def test_validate_handles_nested_brackets_correctly():
    """Tags inside nested `[[…[[…]]…]]` blocks must still be discovered."""
    sql = "WHERE TRUE [[AND col1 = {{v1}} [[AND col2 = {{v2}}]]]]"
    # Both tags declared → valid (no missing-tag error, no bare-bracket error).
    err = _validate_template_tag_coverage(
        sql,
        {
            "v1": {"type": "text", "name": "v1", "id": "1"},
            "v2": {"type": "text", "name": "v2", "id": "2"},
        },
    )
    assert err is None
    # Drop one tag → it's reported as undeclared (proves the scanner reaches inside).
    err = _validate_template_tag_coverage(
        sql,
        {"v1": {"type": "text", "name": "v1", "id": "1"}},
    )
    assert err is not None and "v2" in err


def test_validate_returns_multiple_problems_at_once():
    """Missing tag + bare bracket should both surface in a single diagnostic.
    (Unused tags are non-blocking and not part of the returned message —
    they're logged separately for visibility.)"""
    err = _validate_template_tag_coverage(
        "SELECT * FROM t WHERE id = {{missing}} [[AND foo = 'bar']]",
        None,
    )
    assert err is not None
    assert "missing" in err
    assert "bare" in err.lower() or "leak" in err


# ---------------------------------------------------------------------------
# _explain_sql_bracket_error
# ---------------------------------------------------------------------------


def test_explain_bracket_error_appends_hint_on_match():
    msg = 'Error: Metabase HTTP 400. ERROR: syntax error at or near "["'
    annotated = _explain_sql_bracket_error(msg)
    assert annotated.startswith(msg)
    assert "optional-clause" in annotated
    assert "MBQL" in annotated  # nudges the LLM toward MBQL


def test_explain_bracket_error_case_insensitive():
    annotated = _explain_sql_bracket_error('ERROR: SYNTAX ERROR AT OR NEAR "["')
    assert "optional-clause" in annotated


def test_explain_bracket_error_single_quote_variant():
    """Some DB engines use single quotes around the offending token."""
    annotated = _explain_sql_bracket_error("ERROR: syntax error at or near '['")
    assert "optional-clause" in annotated


def test_explain_bracket_error_passthrough_unrelated():
    """Non-bracket errors must NOT be decorated."""
    msg = "Error: permission denied"
    assert _explain_sql_bracket_error(msg) == msg


# ---------------------------------------------------------------------------
# _check_dataset_failed
# ---------------------------------------------------------------------------


def test_check_dataset_failed_status_completed():
    assert _check_dataset_failed({"status": "completed", "data": {"rows": []}}) is None


def test_check_dataset_failed_no_status_field():
    """If `status` is absent, treat the response as successful (no decoration)."""
    assert _check_dataset_failed({"data": {"rows": []}}) is None


def test_check_dataset_failed_status_failed_with_error():
    msg = _check_dataset_failed({"status": "failed", "error": "table not found"})
    assert msg is not None
    assert "failed" in msg.lower()
    assert "table not found" in msg


def test_check_dataset_failed_status_failed_no_error():
    msg = _check_dataset_failed({"status": "failed"})
    assert msg is not None
    # Falls back to a generic message instead of crashing.
    assert "failed" in msg.lower()


def test_check_dataset_failed_non_dict_input():
    assert _check_dataset_failed(None) is None
    assert _check_dataset_failed("oops") is None
    assert _check_dataset_failed([1, 2, 3]) is None


# ---------------------------------------------------------------------------
# _normalise_mbql
# ---------------------------------------------------------------------------


def test_normalise_mbql_inner_form_is_copied():
    src = {"source-table": 1, "aggregation": [["count"]]}
    out = _normalise_mbql(src)
    assert out == src
    assert out is not src  # shallow copy
    out["source-table"] = 999
    assert src["source-table"] == 1  # original untouched


def test_normalise_mbql_envelope_is_unwrapped_and_copied():
    """When given a full `{"type": "query", "query": {...}}` envelope, return
    the inner query — fresh copy."""
    inner = {"source-table": 42}
    envelope = {"database": 1, "type": "query", "query": inner}
    out = _normalise_mbql(envelope)
    assert out == inner
    assert out is not inner
    out["new_key"] = "x"
    assert "new_key" not in inner


def test_normalise_mbql_rejects_non_dict():
    assert _normalise_mbql(None) is None
    assert _normalise_mbql("not a dict") is None
    assert _normalise_mbql([1, 2, 3]) is None


def test_normalise_mbql_envelope_without_inner_dict_is_treated_as_plain():
    """`type` + `query` keys but `query` is not a dict → treat as a plain
    inner MBQL (it might legitimately use those keys)."""
    src = {"type": "metric", "query": "irrelevant"}
    out = _normalise_mbql(src)
    assert out == src and out is not src


# ---------------------------------------------------------------------------
# Tool registration smoke test
# ---------------------------------------------------------------------------


def test_build_tools_registers_new_metabase_tools():
    """Make sure the 6 tools of ``expected_new`` are wired up."""
    executor = MagicMock()
    executor.register = MagicMock()
    tools = build_tools("https://metabase.example.com||fake-key", executor)
    tool_names = {t.name for t in tools}
    expected_new = {
        "run_metabase_mbql_query",
        "validate_metabase_native_query",
        "list_metabase_metrics",
        "read_metabase_metric",
        "list_metabase_segments",
        "read_metabase_segment",
    }
    assert expected_new.issubset(tool_names)


def test_list_metabase_metrics_has_limit_param():
    """The fallback path is bounded by `limit` to avoid downloading every card
    on a large instance — exposed as a tool parameter so callers can override."""
    executor = MagicMock()
    executor.register = MagicMock()
    tools = build_tools("https://metabase.example.com||fake-key", executor)
    tool = next(t for t in tools if t.name == "list_metabase_metrics")
    assert "limit" in tool.input_schema["properties"]
    assert tool.input_schema["properties"]["limit"]["type"] == "integer"


def test_update_metabase_question_documents_full_replacement():
    """The MBQL update path is a full replacement (not merged) — the tool
    description must say so explicitly so the LLM does not pass partial MBQL."""
    executor = MagicMock()
    executor.register = MagicMock()
    tools = build_tools("https://metabase.example.com||fake-key", executor)
    tool = next(t for t in tools if t.name == "update_metabase_question")
    desc = tool.description.lower()
    assert "full" in desc and "replace" in desc
    # And on the mbql_query property itself:
    mbql_desc = tool.input_schema["properties"]["mbql_query"]["description"].lower()
    assert "replace" in mbql_desc


# ---------------------------------------------------------------------------
# Safety nets on update_metabase_question — prevents the production bug where
# agents accidentally emptied a question's SQL body.
# ---------------------------------------------------------------------------


async def test_update_refuses_template_tags_on_mbql_question():
    """Bug A: applying template_tags to an MBQL question must NOT silently
    convert it to a native question with empty SQL — the legacy code did
    exactly that and wiped questions in prod."""
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("https://metabase.example.com||fake-key", executor)
    update_fn = _get_registered_fn(executor, "update_metabase_question")

    existing_mbql_card = {
        "id": 42,
        "dataset_query": {
            "database": 1,
            "type": "query",
            "query": {
                "source-table": 100,
                "aggregation": [["count"]],
                "breakout": [["field", 200, None]],
            },
        },
    }

    with patch(
        "app.integrations.providers.metabase.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (_mock_response(existing_mbql_card), None)
        result = await update_fn(
            card_id=42,
            template_tags='{"date":{"type":"dimension","name":"date","id":"abc"}}',
        )

    # The fix path returns a structured refusal — no HTTP write happens.
    assert "Refusing" in result
    assert "MBQL" in result or "structured" in result
    assert "template_tags only apply to native" in result
    # Only the READ happened, no PUT. (The first call is the read.)
    methods = [call.args[1] for call in mock_req.call_args_list]
    assert methods == ["get"]


async def test_update_refuses_empty_sql_native_question():
    """Bug C: refuse to save an empty/whitespace-only SQL body on a native
    question — that would wipe the question content."""
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("https://metabase.example.com||fake-key", executor)
    update_fn = _get_registered_fn(executor, "update_metabase_question")

    existing_native_card = {
        "id": 7,
        "dataset_query": {
            "database": 1,
            "type": "native",
            "native": {"query": "SELECT * FROM very_important_table", "template-tags": {}},
        },
    }

    with patch(
        "app.integrations.providers.metabase.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (_mock_response(existing_native_card), None)
        # Whitespace-only payload that the legacy code happily saved.
        result = await update_fn(card_id=7, native_query="   \n   ")

    assert "Refusing" in result
    assert "empty SQL" in result
    methods = [call.args[1] for call in mock_req.call_args_list]
    assert methods == ["get"]


async def test_update_accepts_template_tags_only_on_native_question():
    """Regression test: the safety net must NOT block the legitimate flow
    of merging template_tags into an existing native question's tag set."""
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("https://metabase.example.com||fake-key", executor)
    update_fn = _get_registered_fn(executor, "update_metabase_question")

    existing_native_card = {
        "id": 9,
        "dataset_query": {
            "database": 1,
            "type": "native",
            "native": {
                "query": "SELECT * FROM t WHERE TRUE [[AND status = {{status}}]]",
                "template-tags": {
                    "status": {"type": "text", "name": "status", "id": "s1"},
                },
            },
        },
    }
    updated_card = {**existing_native_card, "name": "Updated"}

    with patch(
        "app.integrations.providers.metabase.safe_request", new_callable=AsyncMock
    ) as mock_req:
        # First call: read. Second call: put. Both succeed.
        mock_req.side_effect = [
            (_mock_response(existing_native_card), None),
            (_mock_response(updated_card), None),
        ]
        result = await update_fn(
            card_id=9,
            template_tags=(
                '{"period":{"type":"dimension","name":"period","id":"p1",'
                '"display-name":"Period",'
                '"dimension":["field",123,{"base-type":"type/DateTime"}],'
                '"widget-type":"date/all-options"}}'
            ),
        )

    assert "Updated" in result or "updated" in result.lower()
    methods = [call.args[1] for call in mock_req.call_args_list]
    assert methods == ["get", "put"]

    # The PUT payload must keep the original SQL and merge the tags.
    put_call = mock_req.call_args_list[1]
    put_body = put_call.kwargs["json"]
    assert put_body["dataset_query"]["type"] == "native"
    assert (
        put_body["dataset_query"]["native"]["query"]
        == "SELECT * FROM t WHERE TRUE [[AND status = {{status}}]]"
    )
    tags = put_body["dataset_query"]["native"]["template-tags"]
    assert "status" in tags and "period" in tags


# ---------------------------------------------------------------------------
# read_metabase_question fenced-block formatting
# ---------------------------------------------------------------------------


async def test_read_native_question_emits_fenced_sql_block():
    """Multi-line SQL must live inside a ```sql / ``` fenced block at the END
    of the response so it cannot bleed into the metadata fields and confuse
    LLM round-tripping."""
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("https://metabase.example.com||fake-key", executor)
    read_fn = _get_registered_fn(executor, "read_metabase_question")

    multiline_sql = "SELECT *\nFROM orders\nWHERE created_at > '2026-01-01'\n  AND status = 'paid'"
    card = {
        "id": 11,
        "name": "Orders YTD",
        "dataset_query": {
            "database": 1,
            "type": "native",
            "native": {"query": multiline_sql, "template-tags": {}},
        },
        "display": "table",
        "visualization_settings": {},
        "collection": None,
    }

    with patch(
        "app.integrations.providers.metabase.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (_mock_response(card), None)
        result = await read_fn(card_id=11)

    # Fenced block opens before SQL and closes after — no metadata key (like
    # "Database ID:", "Created:", etc.) should appear AFTER ```sql.
    assert "```sql" in result
    closing = result.rindex("```")
    opening = result.index("```sql")
    body = result[opening + len("```sql") + 1 : closing]
    # The exact SQL bytes round-trip cleanly.
    assert body.strip() == multiline_sql.strip()
    # Metadata appears BEFORE the fenced block, not inside it.
    after_fence = result[closing:]
    assert "Database ID" not in after_fence
    assert "Created" not in after_fence


async def test_read_mbql_question_emits_fenced_json_block():
    """MBQL questions get a ```json fenced block of the inner query stage so
    the LLM can copy it verbatim into update_metabase_question(mbql_query=...)."""
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("https://metabase.example.com||fake-key", executor)
    read_fn = _get_registered_fn(executor, "read_metabase_question")

    inner_mbql = {
        "source-table": 42,
        "aggregation": [["count"]],
        "breakout": [["field", 305, {"temporal-unit": "month"}]],
    }
    card = {
        "id": 99,
        "name": "Active Users by Month",
        "dataset_query": {"database": 1, "type": "query", "query": inner_mbql},
        "display": "line",
        "visualization_settings": {},
        "collection": None,
    }

    with patch(
        "app.integrations.providers.metabase.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (_mock_response(card), None)
        result = await read_fn(card_id=99)

    assert "```json" in result
    # The JSON block must parse back to the exact MBQL stage.
    import json
    start = result.index("```json") + len("```json")
    end = result.rindex("```")
    body = result[start:end].strip()
    parsed = json.loads(body)
    assert parsed == inner_mbql


# ---------------------------------------------------------------------------
# _is_column_not_found_error
# ---------------------------------------------------------------------------


def test_is_column_not_found_error_detects_postgres_pattern():
    """Standard Postgres 'column "X" does not exist' messages are detected."""
    err = 'Error: Metabase returned HTTP 400 while trying to run SQL query. ERROR: column "platforms_count" does not exist\n  Position: 236'
    assert _is_column_not_found_error(err) is True


def test_is_column_not_found_error_detects_second_example():
    err = 'Error: Metabase returned HTTP 400 while trying to run SQL query. ERROR: column "total_logins_count" does not exist\n  Position: 146'
    assert _is_column_not_found_error(err) is True


def test_is_column_not_found_error_returns_false_for_bracket_error():
    """The bracket-syntax error must not trigger the column-hint path."""
    err = 'Error: syntax error at or near "["'
    assert _is_column_not_found_error(err) is False


def test_is_column_not_found_error_returns_false_for_table_error():
    """A 'relation X does not exist' (table not found) must not match."""
    err = 'ERROR: relation "public_marts.products_xtm_one_usage_by_tag_monthly" does not exist'
    assert _is_column_not_found_error(err) is False


def test_is_column_not_found_error_detects_alias_qualified_form():
    """Postgres emits alias/table-qualified column errors WITHOUT quotes."""
    assert _is_column_not_found_error("ERROR: column t.platforms_count does not exist") is True
    assert _is_column_not_found_error("ERROR: column events.user_idd does not exist\n  Position: 42") is True


def test_is_column_not_found_error_detects_of_relation_form():
    """INSERT/UPDATE against a bad column produce the 'of relation' wording."""
    err = 'ERROR: column "user_idd" of relation "events" does not exist'
    assert _is_column_not_found_error(err) is True


def test_is_column_not_found_error_detects_qualified_name_with_a_space():
    """Postgres prints a qualified column unquoted, spaces included."""
    err = (
        "Error: Metabase rejected the request to run MBQL query — bad request. "
        "ERROR: column __mb_source.Narr Target does not exist\n  Position: 203"
    )
    assert _is_column_not_found_error(err) is True


def test_missing_column_reads_qualifier_and_column():
    assert _missing_column('ERROR: column "platforms_count" does not exist') == (
        "",
        "platforms_count",
    )
    assert _missing_column("ERROR: column t.platforms_count does not exist") == (
        "t",
        "platforms_count",
    )
    assert _missing_column("ERROR: column public.events.user_idd does not exist") == (
        "public.events",
        "user_idd",
    )
    assert _missing_column(
        "ERROR: column __mb_source.Narr Target does not exist\n  Position: 203"
    ) == ("__mb_source", "Narr Target")
    assert _missing_column(
        'ERROR: column "user_idd" of relation "events" does not exist'
    ) == ("", "user_idd")
    assert _missing_column('ERROR: relation "events" does not exist') is None


def test_card_source_id_reads_only_a_saved_question_source():
    assert _card_source_id({"source-table": "card__123"}) == 123
    assert _card_source_id({"source-table": 42}) is None
    assert _card_source_id({"source-query": {"source-table": "card__1"}}) is None
    assert _card_source_id({"source-table": "card__abc"}) is None
    assert _card_source_id(None) is None


# ---------------------------------------------------------------------------
# run_metabase_query — column-not-found schema hint
# ---------------------------------------------------------------------------

_DB_METADATA_TWO_TABLES = {
    "id": 3,
    "name": "Analytics",
    "engine": "postgres",
    "tables": [
        {
            "name": "events",
            "schema": "public",
            "fields": [
                {"name": "id"},
                {"name": "user_id"},
                {"name": "event_type"},
                {"name": "created_at"},
            ],
        },
        {
            "name": "users",
            "schema": "public",
            "fields": [
                {"name": "id"},
                {"name": "email"},
                {"name": "created_at"},
            ],
        },
    ],
}


async def test_run_metabase_query_column_error_appends_schema_hint():
    """When run_metabase_query gets a 'column X does not exist' HTTP 400, the
    response must include available columns from the real schema so the agent
    can self-correct without a separate introspection call."""
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("https://metabase.example.com||fake-key", executor)
    run_fn = _get_registered_fn(executor, "run_metabase_query")

    # First call: the SQL query → HTTP 400 column error
    # Second call: DB metadata fetch triggered by the column-hint path
    with patch(
        "app.integrations.providers.metabase.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.side_effect = [
            (None, 'Error: Metabase returned HTTP 400 while trying to run SQL query. ERROR: column "platforms_count" does not exist\n  Position: 236'),
            (_mock_response(_DB_METADATA_TWO_TABLES), None),
        ]
        result = await run_fn(database_id=3, query="SELECT platforms_count FROM events")

    # The original error is preserved.
    assert "platforms_count" in result
    assert "does not exist" in result
    # The schema hint lists real column names.
    assert "public.events" in result
    assert "user_id" in result
    assert "event_type" in result
    assert "public.users" in result
    assert "email" in result
    # The schema fetch was triggered as the second call.
    assert mock_req.call_count == 2


async def test_run_metabase_query_non_column_error_no_schema_fetch():
    """Non-column errors (e.g. bracket syntax) must NOT trigger the extra
    schema-metadata fetch — the path is column-error-only."""
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("https://metabase.example.com||fake-key", executor)
    run_fn = _get_registered_fn(executor, "run_metabase_query")

    with patch(
        "app.integrations.providers.metabase.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (
            None,
            'Error: Metabase returned HTTP 400 while trying to run SQL query. syntax error at or near "["',
        )
        result = await run_fn(database_id=3, query="SELECT [[col FROM t")

    # Only the one failing query call was made — no metadata fetch.
    assert mock_req.call_count == 1
    # The bracket hint is still present.
    assert "Hint" in result or "template-tag" in result


async def test_run_metabase_query_schema_fetch_failure_degrades_gracefully():
    """If the metadata call itself fails, run_metabase_query must still return
    the original column error plus a fallback hint — it must never raise."""
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("https://metabase.example.com||fake-key", executor)
    run_fn = _get_registered_fn(executor, "run_metabase_query")

    with patch(
        "app.integrations.providers.metabase.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.side_effect = [
            (None, 'Error: Metabase returned HTTP 400 while trying to run SQL query. ERROR: column "bad_col" does not exist'),
            # Metadata fetch returns an error too.
            (None, "Error: Metabase returned HTTP 403 while trying to fetch schema for column hint. Forbidden"),
        ]
        result = await run_fn(database_id=3, query="SELECT bad_col FROM t")

    assert "bad_col" in result
    assert "does not exist" in result
    # Fallback guidance is present.
    assert "get_metabase_database_metadata" in result or "get_metabase_table_metadata" in result


# ---------------------------------------------------------------------------
# validate_metabase_native_query — column-not-found schema hint
# ---------------------------------------------------------------------------


async def test_validate_metabase_native_query_column_error_appends_schema_hint():
    """validate_metabase_native_query must also enrich column-not-found errors
    with available schema columns."""
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("https://metabase.example.com||fake-key", executor)
    validate_fn = _get_registered_fn(executor, "validate_metabase_native_query")

    with patch(
        "app.integrations.providers.metabase.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.side_effect = [
            (None, 'Error: Metabase returned HTTP 400 while trying to validate SQL query. ERROR: column "total_logins_count" does not exist\n  Position: 146'),
            (_mock_response(_DB_METADATA_TWO_TABLES), None),
        ]
        result = await validate_fn(database_id=3, query="SELECT total_logins_count FROM users")

    assert "total_logins_count" in result
    assert "does not exist" in result
    assert "public.users" in result
    assert "email" in result
    assert mock_req.call_count == 2


async def test_validate_metabase_native_query_in_body_failure_appends_schema_hint():
    """The in-body `status: failed` path (HTTP 200/202) of the validate tool
    must be enriched identically to the HTTP-error path."""
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("https://metabase.example.com||fake-key", executor)
    validate_fn = _get_registered_fn(executor, "validate_metabase_native_query")

    failed_body = {
        "status": "failed",
        "error": 'ERROR: column "total_logins_count" does not exist\n  Position: 146',
    }
    with patch(
        "app.integrations.providers.metabase.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.side_effect = [
            (_mock_response(failed_body, status_code=202), None),
            (_mock_response(_DB_METADATA_TWO_TABLES), None),
        ]
        result = await validate_fn(database_id=3, query="SELECT total_logins_count FROM users")

    assert "total_logins_count" in result
    assert "does not exist" in result
    assert "public.users" in result
    assert "email" in result
    assert mock_req.call_count == 2


async def test_schema_hint_caps_tables_and_columns():
    """The hint is bounded: at most _SCHEMA_HINT_MAX_TABLES tables (with a
    truncation marker pointing at get_metabase_database_metadata) and at most
    _SCHEMA_HINT_MAX_COLUMNS_PER_TABLE columns per table (with a '+N more'
    marker). Tables beyond the cap are never formatted (short-circuit)."""
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("https://metabase.example.com||fake-key", executor)
    run_fn = _get_registered_fn(executor, "run_metabase_query")

    wide_fields = [{"name": f"col_{i}"} for i in range(_SCHEMA_HINT_MAX_COLUMNS_PER_TABLE + 5)]
    big_metadata = {
        "id": 3,
        "tables": [
            {"name": f"table_{i:03d}", "schema": "public", "fields": wide_fields}
            for i in range(_SCHEMA_HINT_MAX_TABLES + 2)
        ],
    }
    with patch(
        "app.integrations.providers.metabase.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.side_effect = [
            (None, 'ERROR: column "ghost" does not exist'),
            (_mock_response(big_metadata), None),
        ]
        result = await run_fn(database_id=3, query="SELECT ghost FROM table_000")

    # Table cap: the first table is listed, everything past the cap is not.
    assert "public.table_000:" in result
    assert f"public.table_{_SCHEMA_HINT_MAX_TABLES - 1:03d}:" in result
    assert f"table_{_SCHEMA_HINT_MAX_TABLES:03d}" not in result
    assert "truncated" in result
    # Column cap: the 50th column is shown, the 51st is folded into '+N more'.
    assert f"col_{_SCHEMA_HINT_MAX_COLUMNS_PER_TABLE - 1}" in result
    assert f"col_{_SCHEMA_HINT_MAX_COLUMNS_PER_TABLE}," not in result
    assert "(+5 more)" in result


async def test_run_metabase_query_in_body_failure_appends_schema_hint():
    """Metabase reports many DB errors in-body on HTTP 200/202 (status: failed).
    That path must be enriched with the schema hint too — not only HTTP errors."""
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("https://metabase.example.com||fake-key", executor)
    run_fn = _get_registered_fn(executor, "run_metabase_query")

    failed_body = {
        "status": "failed",
        "error": 'ERROR: column "platforms_count" does not exist\n  Position: 236',
    }
    with patch(
        "app.integrations.providers.metabase.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.side_effect = [
            (_mock_response(failed_body, status_code=202), None),
            (_mock_response(_DB_METADATA_TWO_TABLES), None),
        ]
        result = await run_fn(database_id=3, query="SELECT platforms_count FROM events")

    assert "platforms_count" in result
    assert "does not exist" in result
    assert "public.events" in result
    assert "public.users" in result
    assert mock_req.call_count == 2


async def test_schema_hint_cached_across_repeated_column_errors():
    """The metadata fetch must run once per database per tool set — a retry
    loop hitting a second phantom column reuses the cached hint."""
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("https://metabase.example.com||fake-key", executor)
    run_fn = _get_registered_fn(executor, "run_metabase_query")

    with patch(
        "app.integrations.providers.metabase.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.side_effect = [
            (None, 'ERROR: column "bad_one" does not exist'),
            (_mock_response(_DB_METADATA_TWO_TABLES), None),
            (None, 'ERROR: column "bad_two" does not exist'),
            # NO fourth response: a second metadata fetch would raise StopIteration.
        ]
        first = await run_fn(database_id=3, query="SELECT bad_one FROM events")
        second = await run_fn(database_id=3, query="SELECT bad_two FROM events")

    assert "public.events" in first
    assert "public.events" in second
    # 2 query calls + exactly 1 metadata fetch.
    assert mock_req.call_count == 3


async def test_run_metabase_mbql_query_column_error_appends_schema_hint():
    """MBQL queries (e.g. card__ sources over broken native SQL) get the same
    column-error enrichment."""
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("https://metabase.example.com||fake-key", executor)
    mbql_fn = _get_registered_fn(executor, "run_metabase_mbql_query")

    with patch(
        "app.integrations.providers.metabase.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.side_effect = [
            (None, 'ERROR: column "ghost_col" does not exist'),
            (_mock_response(_DB_METADATA_TWO_TABLES), None),
        ]
        result = await mbql_fn(database_id=3, mbql_query='{"source-table": 7}')

    assert "ghost_col" in result
    assert "public.events" in result
    assert mock_req.call_count == 2


async def test_run_metabase_mbql_query_in_body_failure_appends_schema_hint():
    """The in-body `status: failed` path (HTTP 200/202) of the MBQL tool must
    be enriched identically to the HTTP-error path — mirroring the coverage of
    run_metabase_query and validate_metabase_native_query."""
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("https://metabase.example.com||fake-key", executor)
    mbql_fn = _get_registered_fn(executor, "run_metabase_mbql_query")

    failed_body = {
        "status": "failed",
        "error": 'ERROR: column "ghost_col" does not exist\n  Position: 42',
    }
    with patch(
        "app.integrations.providers.metabase.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.side_effect = [
            (_mock_response(failed_body, status_code=202), None),
            (_mock_response(_DB_METADATA_TWO_TABLES), None),
        ]
        result = await mbql_fn(database_id=3, mbql_query='{"source-table": 7}')

    assert "ghost_col" in result
    assert "does not exist" in result
    assert "public.events" in result
    assert "public.users" in result
    assert mock_req.call_count == 2


# ---------------------------------------------------------------------------
# Saved-question sources
# ---------------------------------------------------------------------------

_PIPELINE_CARD = {
    "id": 123,
    "name": "Pipeline by quarter",
    "dataset_query": {"database": 3, "type": "native", "native": {"query": "SELECT ..."}},
    "result_metadata": [
        {"name": "quarter", "display_name": "Quarter", "base_type": "type/Text"},
        {"name": "narr_target", "display_name": "Narr Target", "base_type": "type/Float"},
        {
            "name": "marketing_sourced",
            "display_name": "Marketing Sourced",
            "base_type": "type/Float",
        },
    ],
}

_SOURCE_COLUMN_ERROR = (
    "Error: Metabase rejected the request to run MBQL query — bad request. "
    "ERROR: column __mb_source.Narr Target does not exist\n  Position: 203"
)

_CARD_MBQL = (
    '{"source-table": "card__123", '
    '"aggregation": [["sum", ["field", "Narr Target", {"base-type": "type/Float"}]]]}'
)


async def test_mbql_source_column_error_lists_the_saved_question_columns():
    """A column of a card__N source written as its display name is answered
    with the card's columns and the ref to use - not the database schema."""
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("https://metabase.example.com||fake-key", executor)
    mbql_fn = _get_registered_fn(executor, "run_metabase_mbql_query")

    with patch(
        "app.integrations.providers.metabase.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.side_effect = [
            (None, _SOURCE_COLUMN_ERROR),
            (_mock_response(_PIPELINE_CARD), None),
        ]
        result = await mbql_fn(database_id=3, mbql_query=_CARD_MBQL)

    assert "__mb_source.Narr Target does not exist" in result
    assert 'is the display name of column "narr_target"' in result
    assert '["field", "narr_target", {"base-type": "type/Float"}]' in result
    assert '["field", "marketing_sourced", {"base-type": "type/Float"}]' in result
    assert "Pipeline by quarter" in result
    # The card was read, the database schema was not.
    assert mock_req.call_count == 2
    assert mock_req.call_args_list[1][0][2].endswith("/api/card/123")
    assert "public.events" not in result


async def test_mbql_source_column_error_in_body_failure_is_grounded_too():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("https://metabase.example.com||fake-key", executor)
    mbql_fn = _get_registered_fn(executor, "run_metabase_mbql_query")

    failed_body = {
        "status": "failed",
        "error": "ERROR: column __mb_source.Marketing Sourced does not exist\n  Position: 327",
    }
    with patch(
        "app.integrations.providers.metabase.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.side_effect = [
            (_mock_response(failed_body, status_code=202), None),
            (_mock_response(_PIPELINE_CARD), None),
        ]
        result = await mbql_fn(database_id=3, mbql_query=_CARD_MBQL)

    assert 'is the display name of column "marketing_sourced"' in result
    assert mock_req.call_count == 2


async def test_mbql_source_column_hint_reads_the_card_once_per_tool_set():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("https://metabase.example.com||fake-key", executor)
    mbql_fn = _get_registered_fn(executor, "run_metabase_mbql_query")

    with patch(
        "app.integrations.providers.metabase.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.side_effect = [
            (None, _SOURCE_COLUMN_ERROR),
            (_mock_response(_PIPELINE_CARD), None),
            (None, "ERROR: column __mb_source.Marketing Sourced does not exist"),
            # NO fourth response: a second card read would raise StopIteration.
        ]
        await mbql_fn(database_id=3, mbql_query=_CARD_MBQL)
        second = await mbql_fn(database_id=3, mbql_query=_CARD_MBQL)

    assert 'is the display name of column "marketing_sourced"' in second
    assert mock_req.call_count == 3


async def test_mbql_source_column_error_degrades_when_the_card_cannot_be_read():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("https://metabase.example.com||fake-key", executor)
    mbql_fn = _get_registered_fn(executor, "run_metabase_mbql_query")

    with patch(
        "app.integrations.providers.metabase.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.side_effect = [
            (None, _SOURCE_COLUMN_ERROR),
            (None, "Error: Metabase returned HTTP 403 while trying to read question columns."),
        ]
        result = await mbql_fn(database_id=3, mbql_query=_CARD_MBQL)

    assert "not a column of saved question 123" in result
    assert "get_metabase_table_metadata(card_id=123)" in result
    assert "not its display name" in result


async def test_mbql_nested_query_source_column_error_states_the_rule_without_a_fetch():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("https://metabase.example.com||fake-key", executor)
    mbql_fn = _get_registered_fn(executor, "run_metabase_mbql_query")

    with patch(
        "app.integrations.providers.metabase.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (None, _SOURCE_COLUMN_ERROR)
        result = await mbql_fn(
            database_id=3,
            mbql_query='{"source-query": {"source-table": 7}, "aggregation": [["count"]]}',
        )

    assert "not a column of this query's source stage" in result
    assert "not its display name" in result
    assert mock_req.call_count == 1


async def test_native_query_source_qualifier_keeps_the_database_schema_hint():
    """``source`` is a legitimate table alias in hand-written SQL: a native
    query never reads it as the MBQL source stage."""
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("https://metabase.example.com||fake-key", executor)
    run_fn = _get_registered_fn(executor, "run_metabase_query")

    with patch(
        "app.integrations.providers.metabase.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.side_effect = [
            (None, "ERROR: column source.ghost does not exist"),
            (_mock_response(_DB_METADATA_TWO_TABLES), None),
        ]
        result = await run_fn(database_id=3, query="SELECT source.ghost FROM events source")

    assert "public.events" in result
    assert "source stage" not in result


async def test_table_metadata_with_card_id_lists_the_saved_question_columns():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("https://metabase.example.com||fake-key", executor)
    meta_fn = _get_registered_fn(executor, "get_metabase_table_metadata")

    with patch(
        "app.integrations.providers.metabase.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (_mock_response(_PIPELINE_CARD), None)
        result = await meta_fn(card_id=123)

    assert mock_req.call_args[0][2].endswith("/api/card/123")
    assert '"source-table": "card__123"' in result
    assert "Database ID: 3" in result
    assert 'Narr Target → ["field", "narr_target", {"base-type": "type/Float"}]' in result
    assert 'Quarter → ["field", "quarter", {"base-type": "type/Text"}]' in result


async def test_table_metadata_without_an_id_asks_for_one():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("https://metabase.example.com||fake-key", executor)
    meta_fn = _get_registered_fn(executor, "get_metabase_table_metadata")

    with patch(
        "app.integrations.providers.metabase.safe_request", new_callable=AsyncMock
    ) as mock_req:
        result = await meta_fn()

    assert "table_id" in result and "card_id" in result
    assert mock_req.call_count == 0


# ---------------------------------------------------------------------------
# run_metabase_question — result cache (ignore_cache + freshness line)
# ---------------------------------------------------------------------------


def _card_result(value: float, *, cached: object, started_at: str) -> dict:
    """A ``POST /api/card/:id/query`` body as Metabase 0.63 answers it."""
    return {
        "status": "completed",
        "row_count": 1,
        "started_at": started_at,
        "cached": cached,
        "data": {
            "cols": [{"name": "avg_deal_size", "display_name": "Average deal size"}],
            "rows": [[value]],
        },
    }


# Card 1234 "Average deal size": the cache holds a result computed before the
# data changed; recomputing reads the mart's current value.
_STALE_AVERAGE = 104218.37
_CURRENT_AVERAGE = 108734.92


async def _metabase_with_a_stale_cache(client, method, url, provider, action, **kwargs):
    if (kwargs.get("json") or {}).get("ignore_cache"):
        return (
            _mock_response(
                _card_result(
                    _CURRENT_AVERAGE,
                    cached=None,
                    started_at="2026-10-06T09:31:12.345678Z",
                )
            ),
            None,
        )
    return (
        _mock_response(
            _card_result(
                _STALE_AVERAGE,
                cached="2026-09-12T10:00:00.123456789+02:00",
                started_at="2026-10-06T09:30:58.001234Z",
            )
        ),
        None,
    )


async def test_run_question_without_ignore_cache_says_it_is_cached_and_its_age():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("https://metabase.example.com||fake-key", executor)
    run_fn = _get_registered_fn(executor, "run_metabase_question")

    with patch(
        "app.integrations.providers.metabase.safe_request",
        new=AsyncMock(side_effect=_metabase_with_a_stale_cache),
    ) as mock_req:
        result = await run_fn(card_id=1234)

    assert result.splitlines()[0] == (
        "Freshness: computed 2026-09-12 08:00 UTC, cached (24 days old). "
        "Run again with ignore_cache=true for the current value."
    )
    assert str(_STALE_AVERAGE) in result
    assert mock_req.call_args.args[2].endswith("/api/card/1234/query")
    assert mock_req.call_args.kwargs["json"] is None


async def test_run_question_with_ignore_cache_returns_the_current_value():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("https://metabase.example.com||fake-key", executor)
    run_fn = _get_registered_fn(executor, "run_metabase_question")

    with patch(
        "app.integrations.providers.metabase.safe_request",
        new=AsyncMock(side_effect=_metabase_with_a_stale_cache),
    ) as mock_req:
        result = await run_fn(card_id=1234, ignore_cache=True)

    assert mock_req.call_args.kwargs["json"] == {"ignore_cache": True}
    assert result.splitlines()[0] == (
        "Freshness: computed 2026-10-06 09:31 UTC, not cached (cache bypassed)."
    )
    assert str(_CURRENT_AVERAGE) in result
    assert str(_STALE_AVERAGE) not in result


async def test_run_question_sends_ignore_cache_alongside_parameters():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("https://metabase.example.com||fake-key", executor)
    run_fn = _get_registered_fn(executor, "run_metabase_question")

    with patch(
        "app.integrations.providers.metabase.safe_request",
        new=AsyncMock(side_effect=_metabase_with_a_stale_cache),
    ) as mock_req:
        await run_fn(
            card_id=1234,
            parameters='[{"type":"category","value":"Q3","target":["variable",["template-tag","quarter"]]}]',
            ignore_cache=True,
        )

    body = mock_req.call_args.kwargs["json"]
    assert body["ignore_cache"] is True
    assert body["parameters"][0]["value"] == "Q3"


async def test_run_question_failure_carries_no_freshness_line():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("https://metabase.example.com||fake-key", executor)
    run_fn = _get_registered_fn(executor, "run_metabase_question")

    failed = {"status": "failed", "error": "relation does not exist", "cached": None}
    with patch(
        "app.integrations.providers.metabase.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (_mock_response(failed, status_code=202), None)
        result = await run_fn(card_id=1234, ignore_cache=True)

    assert "relation does not exist" in result
    assert "Freshness" not in result


def test_result_freshness_reads_older_metabase_shapes():
    """Before ``cached`` carried the time, it was a boolean, some versions
    with the time in ``updated_at``."""
    assert _result_freshness(
        {"cached": True, "updated_at": "2026-09-12T08:00:00Z", "started_at": "2026-09-12T11:05:00Z"}
    ).startswith("Freshness: computed 2026-09-12 08:00 UTC, cached (3 hours old).")
    assert _result_freshness({"cached": True}).startswith(
        "Freshness: cached, computed at a time this Metabase version does not report."
    )
    assert _result_freshness({"cached": False, "started_at": "2026-09-12T08:00:00Z"}) == (
        "Freshness: computed 2026-09-12 08:00 UTC, not cached."
    )


def test_format_age_picks_a_readable_unit():
    assert _format_age(timedelta(seconds=30)) == "under a minute"
    assert _format_age(timedelta(seconds=-5)) == "under a minute"
    assert _format_age(timedelta(minutes=1)) == "1 minute"
    assert _format_age(timedelta(minutes=59)) == "59 minutes"
    assert _format_age(timedelta(hours=1)) == "1 hour"
    assert _format_age(timedelta(hours=47)) == "47 hours"
    assert _format_age(timedelta(days=24, hours=1)) == "24 days"


def test_run_metabase_question_offers_ignore_cache_for_board_figures():
    executor = MagicMock()
    executor.register = MagicMock()
    tools = build_tools("https://metabase.example.com||fake-key", executor)
    tool = next(t for t in tools if t.name == "run_metabase_question")

    assert tool.input_schema["properties"]["ignore_cache"]["type"] == "boolean"
    assert tool.input_schema["properties"]["ignore_cache"]["default"] is False
    assert "ignore_cache" not in tool.input_schema["required"]
    assert (
        "For Board or finance figures, run with ignore_cache=true, or report the result's age."
        in tool.description
    )
