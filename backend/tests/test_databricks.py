"""Tests for the Databricks integration provider.

Covers tool introspection, workspace-URL normalisation, the dual PAT / OAuth
machine-to-machine auth modes (including the in-process token cache), the
read-only SQL guard, Statement Execution result rendering and polling, the
Unity Catalog / Jobs / Mosaic AI / Files tool surfaces, and the failure paths
(missing credentials, non-JSON bodies, unexpected response shapes). Every API
call routes through the shared ``safe_request`` helper, which is monkeypatched
here so no network is touched; the OAuth token exchange is stubbed the same
way.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from app.integrations.providers import databricks as databricks_mod
from app.integrations.providers.databricks import (
    DatabricksIntegration,
    build_tools,
    check_read_only_sql,
    format_statement_result,
    resolve_workspace_url,
)
from app.tools.base import CallableToolExecutor, ToolCall

_HOST = "https://dbc-1234abcd-5e6f.cloud.databricks.com"
# workspace_url||client_id||warehouse_id||secret — PAT mode leaves client_id empty.
_PAT_TOKEN = f"{_HOST}||||wh-abc123||dapi-secret"
_OAUTH_TOKEN = f"{_HOST}||sp-client-id||wh-abc123||oauth-secret"


class _StubResponse:
    def __init__(
        self, payload: Any, *, content: bytes | None = None, status_code: int = 200
    ):
        self._payload = payload
        self.status_code = status_code
        self.content = b"{}" if content is None else content

    def json(self) -> Any:
        if self._payload is _NON_JSON:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


_NON_JSON = object()


class _Recorder:
    """Fake ``safe_request`` that routes by (method, url) and records calls."""

    def __init__(self, router):
        self._router = router
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        payload = self._router(method, url)
        if isinstance(payload, tuple):
            # (payload, error) — lets a test drive the error branch.
            body, error = payload
            if body is None:
                return None, error
            resp = body if isinstance(body, _StubResponse) else _StubResponse(body)
            return resp, error
        if isinstance(payload, _StubResponse):
            return payload, None
        if isinstance(payload, bytes):
            return _StubResponse({}, content=payload), None
        return _StubResponse(payload), None


def _build(
    monkeypatch, router, token: str = _PAT_TOKEN
) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(router)
    monkeypatch.setattr(databricks_mod, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=token, executor=executor)
    return executor, recorder


async def _call(executor: CallableToolExecutor, tool: str, /, **args) -> str:
    """Invoke a registered tool. Both parameters are positional-only so a
    tool argument literally called ``name`` (serving endpoints) cannot
    collide with the helper's own signature."""
    result = await executor.execute_tool(ToolCall(id="t1", name=tool, arguments=args))
    return result.content


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


def test_build_tools_returns_definitions_with_dummy_token():
    executor = CallableToolExecutor()
    tools = build_tools(
        token="https://dummy.example.com||__dummy__", executor=executor
    )
    names = {t.name for t in tools}
    assert "query_databricks_sql" in names
    assert "execute_databricks_sql" in names
    assert "list_databricks_catalogs" in names
    assert "run_databricks_job" in names
    assert "query_databricks_serving_endpoint" in names
    assert "read_databricks_file" in names


def test_every_tool_definition_is_registered_on_the_executor():
    executor = CallableToolExecutor()
    tools = build_tools(token=_PAT_TOKEN, executor=executor)
    registered = set(executor._handlers)
    for tool in tools:
        assert tool.name in registered, f"{tool.name} is not registered"


def test_tool_definitions_are_unique_and_sourced():
    executor = CallableToolExecutor()
    tools = build_tools(token=_PAT_TOKEN, executor=executor)
    names = [t.name for t in tools]
    assert len(names) == len(set(names))
    assert all(t.source == "integration:databricks" for t in tools)


def test_required_schema_fields_are_declared_as_properties():
    executor = CallableToolExecutor()
    for tool in build_tools(token=_PAT_TOKEN, executor=executor):
        props = tool.input_schema.get("properties", {})
        for required in tool.input_schema.get("required", []):
            assert required in props, f"{tool.name}: {required} missing from properties"


# ---------------------------------------------------------------------------
# Workspace URL normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (_HOST, _HOST),
        (f"{_HOST}/", _HOST),
        ("dbc-1234abcd-5e6f.cloud.databricks.com", _HOST),
        (f"{_HOST}/sql/warehouses/abc?o=12345#tab", _HOST),
        ("https://adb-123.7.azuredatabricks.net/", "https://adb-123.7.azuredatabricks.net"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_resolve_workspace_url_normalises_every_pasted_form(raw, expected):
    assert resolve_workspace_url(raw) == expected


def test_resolve_workspace_url_upgrades_http_to_https():
    # The bearer token must never travel in cleartext.
    assert resolve_workspace_url("http://dbc-x.cloud.databricks.com") == (
        "https://dbc-x.cloud.databricks.com"
    )


# ---------------------------------------------------------------------------
# Read-only SQL guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select * from main.default.events where ts > '2026-01-01'",
        "WITH recent AS (SELECT * FROM t) SELECT count(*) FROM recent",
        "SHOW TABLES IN main.default",
        "SHOW CREATE TABLE main.default.events",
        "SHOW GRANTS ON TABLE main.default.events",
        "DESCRIBE TABLE EXTENDED main.default.events",
        "DESC HISTORY main.default.events",
        "EXPLAIN SELECT * FROM t",
        "VALUES (1), (2)",
        "TABLE main.default.events",
        "SELECT replace(name, 'a', 'b') FROM t",
        "SELECT comment FROM t",
        "SELECT load_time, last_update FROM t",
        "SELECT `update`, `delete` FROM t",
        "SELECT 'drop table t' AS literal FROM t",
        "-- drop table t\nSELECT 1",
        "/* delete from t */ SELECT 1",
    ],
)
def test_read_only_guard_accepts_genuine_reads(sql):
    assert check_read_only_sql(sql) is None


@pytest.mark.parametrize(
    ("sql", "needle"),
    [
        ("DELETE FROM t", "starts with 'DELETE'"),
        ("INSERT INTO t VALUES (1)", "starts with 'INSERT'"),
        ("CREATE TABLE t (a INT)", "starts with 'CREATE'"),
        ("MERGE INTO t USING s ON t.a = s.a", "starts with 'MERGE'"),
        ("COMMENT ON TABLE t IS 'x'", "starts with 'COMMENT'"),
        ("LOAD DATA INPATH '/x' INTO TABLE t", "starts with 'LOAD'"),
        ("SELECT 1; DROP TABLE t", "DROP"),
        ("SELECT * FROM t; DELETE FROM t", "DELETE"),
        ("WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x", "INSERT"),
        ("SELECT 1 -- ok\n; GRANT SELECT ON t TO `u`", "GRANT"),
        ("EXPLAIN DELETE FROM t", "DELETE"),
    ],
)
def test_read_only_guard_refuses_mutations(sql, needle):
    error = check_read_only_sql(sql)
    assert error is not None
    assert needle in error
    assert "execute_databricks_sql" in error


@pytest.mark.parametrize("sql", ["", "   ", "-- only a comment", "/* nothing */"])
def test_read_only_guard_rejects_empty_and_comment_only_input(sql):
    assert check_read_only_sql(sql) is not None


def test_read_only_guard_is_not_fooled_by_a_quote_inside_a_comment():
    # The block comment is stripped first, so its unbalanced quote cannot
    # swallow the rest of the statement and hide the DROP.
    error = check_read_only_sql("SELECT 1 /* it's fine */ ; DROP TABLE t")
    assert error is not None and "DROP" in error


# ---------------------------------------------------------------------------
# Statement result rendering
# ---------------------------------------------------------------------------


def _succeeded_payload(rows, *, total=None, truncated=False):
    return {
        "statement_id": "stmt-1",
        "status": {"state": "SUCCEEDED"},
        "manifest": {
            "schema": {
                "columns": [
                    {"name": "id", "type_text": "BIGINT"},
                    {"name": "name", "type_text": "STRING"},
                ]
            },
            "total_row_count": len(rows) if total is None else total,
            "truncated": truncated,
        },
        "result": {"data_array": rows},
    }


def test_format_statement_result_renders_a_bounded_table():
    out = format_statement_result(
        _succeeded_payload([["1", "alice"], ["2", "bob"]]), 100
    )
    assert "succeeded - 2 row(s), 2 column(s)" in out
    assert "id (BIGINT) | name (STRING)" in out
    assert "1 | alice" in out
    assert "2 | bob" in out


def test_format_statement_result_honours_the_row_cap():
    rows = [[str(i), f"n{i}"] for i in range(10)]
    out = format_statement_result(_succeeded_payload(rows), 3)
    assert "Showing 3 of 10 fetched rows" in out
    assert "n9" not in out


def test_format_statement_result_renders_null_cells():
    out = format_statement_result(_succeeded_payload([["1", None]]), 100)
    assert "1 | NULL" in out


def test_format_statement_result_flags_server_side_truncation():
    out = format_statement_result(
        _succeeded_payload([["1", "a"]], total=99999, truncated=True), 100
    )
    assert "Databricks truncated the result set" in out


def test_format_statement_result_reports_a_statement_that_failed():
    out = format_statement_result(
        {
            "statement_id": "stmt-2",
            "status": {
                "state": "FAILED",
                "error": {
                    "error_code": "TABLE_OR_VIEW_NOT_FOUND",
                    "message": "cannot resolve main.default.nope",
                },
            },
        },
        100,
    )
    assert "finished with state FAILED" in out
    assert "TABLE_OR_VIEW_NOT_FOUND" in out
    assert "cannot resolve main.default.nope" in out


def test_format_statement_result_handles_a_dml_statement_with_no_result_set():
    out = format_statement_result(
        {"statement_id": "stmt-3", "status": {"state": "SUCCEEDED"}}, 100
    )
    assert "succeeded (no rows returned)" in out


# ---------------------------------------------------------------------------
# Authentication: PAT vs OAuth machine-to-machine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pat_mode_sends_the_secret_directly_as_the_bearer(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"warehouses": []})
    await _call(executor, "list_databricks_warehouses")
    assert recorder.calls[0]["headers"]["Authorization"] == "Bearer dapi-secret"


@pytest.mark.asyncio
async def test_oauth_mode_exchanges_credentials_and_caches_the_token(monkeypatch):
    exchanges: list[tuple[str, str, str]] = []

    async def _fake_exchange(host, client_id, client_secret):
        exchanges.append((host, client_id, client_secret))
        return "minted-token", 3600

    monkeypatch.setattr(databricks_mod, "request_oauth_token", _fake_exchange)
    # A fresh cache key per run: the secret is unique to this test.
    executor, recorder = _build(
        monkeypatch,
        lambda m, u: {"warehouses": []},
        token=f"{_HOST}||sp-client-id||wh-abc123||oauth-secret-cache-test",
    )
    await _call(executor, "list_databricks_warehouses")
    await _call(executor, "list_databricks_warehouses")
    assert recorder.calls[0]["headers"]["Authorization"] == "Bearer minted-token"
    assert recorder.calls[1]["headers"]["Authorization"] == "Bearer minted-token"
    # Second call reused the cached token instead of re-minting.
    assert len(exchanges) == 1
    assert exchanges[0] == (_HOST, "sp-client-id", "oauth-secret-cache-test")


@pytest.mark.asyncio
async def test_oauth_failure_surfaces_service_principal_guidance(monkeypatch):
    async def _failing_exchange(_host, _cid, _secret):
        return None, 0

    monkeypatch.setattr(databricks_mod, "request_oauth_token", _failing_exchange)
    executor, _ = _build(
        monkeypatch,
        lambda m, u: {"warehouses": []},
        token=f"{_HOST}||sp-client-id||wh||bad-secret-unique",
    )
    out = await _call(executor, "list_databricks_warehouses")
    assert "OAuth machine-to-machine" in out
    assert "service principal" in out


@pytest.mark.asyncio
async def test_missing_workspace_url_is_an_actionable_configuration_error(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"warehouses": []}, token="||||||dapi-secret"
    )
    out = await _call(executor, "list_databricks_warehouses")
    assert "workspace URL is not configured" in out
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_missing_credential_is_an_actionable_configuration_error(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"warehouses": []}, token=f"{_HOST}||||wh||"
    )
    out = await _call(executor, "list_databricks_warehouses")
    assert "credential is not configured" in out
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_a_partial_token_still_builds_the_full_tool_surface():
    # integration_tools returns a partial compound rather than None so the
    # misconfiguration surfaces at call time instead of the tools vanishing.
    executor = CallableToolExecutor()
    tools = build_tools(token="||||||", executor=executor)
    assert len(tools) > 20


# ---------------------------------------------------------------------------
# SQL execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_sql_posts_the_statement_and_renders_rows(monkeypatch):
    def _router(method, url):
        assert method == "post" and url.endswith("/api/2.0/sql/statements")
        return _succeeded_payload([["1", "alice"]])

    executor, recorder = _build(monkeypatch, _router)
    out = await _call(
        executor, "query_databricks_sql", statement="SELECT id, name FROM t"
    )
    assert "1 | alice" in out
    body = recorder.calls[0]["json"]
    assert body["warehouse_id"] == "wh-abc123"  # the configured default
    assert body["format"] == "JSON_ARRAY"
    assert body["disposition"] == "INLINE"
    assert body["on_wait_timeout"] == "CONTINUE"


@pytest.mark.asyncio
async def test_query_sql_refuses_a_mutation_before_touching_the_network(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: _succeeded_payload([]))
    out = await _call(executor, "query_databricks_sql", statement="DELETE FROM t")
    assert "only runs read-only statements" in out
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_execute_sql_allows_a_mutation(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        lambda m, u: {"statement_id": "s9", "status": {"state": "SUCCEEDED"}},
    )
    out = await _call(executor, "execute_databricks_sql", statement="DELETE FROM t")
    assert "succeeded (no rows returned)" in out
    assert recorder.calls[0]["json"]["statement"] == "DELETE FROM t"


@pytest.mark.asyncio
async def test_execute_sql_still_requires_a_statement(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    assert "statement is required" in await _call(executor, "execute_databricks_sql")
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_sql_uses_an_explicit_warehouse_over_the_configured_default(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: _succeeded_payload([]))
    await _call(
        executor,
        "query_databricks_sql",
        statement="SELECT 1",
        warehouse_id="wh-explicit",
    )
    assert recorder.calls[0]["json"]["warehouse_id"] == "wh-explicit"


@pytest.mark.asyncio
async def test_sql_without_any_warehouse_is_an_actionable_error(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        lambda m, u: _succeeded_payload([]),
        token=f"{_HOST}||||||dapi-secret",  # no default warehouse configured
    )
    out = await _call(executor, "query_databricks_sql", statement="SELECT 1")
    assert "no SQL warehouse specified" in out
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_sql_parameters_are_converted_to_statement_api_objects(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: _succeeded_payload([]))
    await _call(
        executor,
        "query_databricks_sql",
        statement="SELECT * FROM t WHERE d > :since AND n > :n",
        parameters='{"since": "2026-01-01", "n": 10}',
    )
    params = recorder.calls[0]["json"]["parameters"]
    assert {"name": "since", "value": "2026-01-01"} in params
    assert {"name": "n", "value": "10"} in params


@pytest.mark.asyncio
async def test_sql_rejects_malformed_parameters_json(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: _succeeded_payload([]))
    out = await _call(
        executor, "query_databricks_sql", statement="SELECT 1", parameters="{oops"
    )
    assert "parameters must be valid JSON" in out
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_sql_rejects_a_non_object_parameters_payload(monkeypatch):
    executor, _ = _build(monkeypatch, lambda m, u: _succeeded_payload([]))
    out = await _call(
        executor, "query_databricks_sql", statement="SELECT 1", parameters="[1, 2]"
    )
    assert "must be a JSON object" in out


@pytest.mark.asyncio
async def test_a_pending_statement_is_polled_until_it_finishes(monkeypatch):
    monkeypatch.setattr(databricks_mod, "_SQL_POLL_INTERVAL", 0)
    states = iter(
        [
            {"statement_id": "s1", "status": {"state": "PENDING"}},
            {"statement_id": "s1", "status": {"state": "RUNNING"}},
            _succeeded_payload([["1", "done"]]),
        ]
    )
    executor, recorder = _build(monkeypatch, lambda m, u: next(states))
    out = await _call(executor, "query_databricks_sql", statement="SELECT 1")
    assert "1 | done" in out
    # One POST plus two polling GETs.
    assert recorder.calls[0]["method"] == "post"
    assert all(c["method"] == "get" for c in recorder.calls[1:])
    assert recorder.calls[-1]["url"].endswith("/api/2.0/sql/statements/s1")


@pytest.mark.asyncio
async def test_a_statement_that_outlives_the_poll_budget_hands_back_its_id(monkeypatch):
    monkeypatch.setattr(databricks_mod, "_SQL_POLL_INTERVAL", 0)
    monkeypatch.setattr(databricks_mod, "_SQL_MAX_POLL_SECONDS", 0)
    executor, _ = _build(
        monkeypatch, lambda m, u: {"statement_id": "slow-1", "status": {"state": "RUNNING"}}
    )
    out = await _call(executor, "query_databricks_sql", statement="SELECT 1")
    assert "get_databricks_statement" in out
    assert "slow-1" in out


@pytest.mark.asyncio
async def test_get_statement_reports_a_still_running_statement(monkeypatch):
    executor, _ = _build(
        monkeypatch, lambda m, u: {"statement_id": "s5", "status": {"state": "RUNNING"}}
    )
    out = await _call(executor, "get_databricks_statement", statement_id="s5")
    assert "still RUNNING" in out


@pytest.mark.asyncio
async def test_cancel_statement_posts_to_the_cancel_route(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    out = await _call(executor, "cancel_databricks_statement", statement_id="s7")
    assert "Cancellation requested" in out
    assert recorder.calls[0]["url"].endswith("/api/2.0/sql/statements/s7/cancel")


@pytest.mark.asyncio
async def test_mutations_opt_out_of_the_transient_status_retry(monkeypatch):
    def _router(method, url):
        return {"catalogs": []} if method == "get" else _succeeded_payload([])

    executor, recorder = _build(monkeypatch, _router)
    # Starting a statement is a mutation: replaying it could execute the SQL
    # twice behind a failing gateway, so it must not opt into the retry.
    await _call(executor, "query_databricks_sql", statement="SELECT 1")
    posts = [c for c in recorder.calls if c["method"] == "post"]
    assert posts and all(c["retry_statuses"] is None for c in posts)
    # A plain read does opt in.
    await _call(executor, "list_databricks_catalogs")
    gets = [c for c in recorder.calls if c["method"] == "get"]
    assert gets and all(
        c["retry_statuses"] == databricks_mod.TRANSIENT_RETRY_STATUSES for c in gets
    )


# ---------------------------------------------------------------------------
# Unity Catalog
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_catalogs_formats_and_paginates(monkeypatch):
    executor, _ = _build(
        monkeypatch,
        lambda m, u: {
            "catalogs": [
                {"name": "main", "catalog_type": "MANAGED_CATALOG", "owner": "ops"}
            ],
            "next_page_token": "tok-2",
        },
    )
    out = await _call(executor, "list_databricks_catalogs")
    assert "main [MANAGED_CATALOG]" in out
    assert 'page_token="tok-2"' in out


@pytest.mark.asyncio
async def test_list_schemas_requires_a_catalog(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    assert "catalog_name is required" in await _call(executor, "list_databricks_schemas")
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_list_tables_requires_both_levels_and_omits_columns(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"tables": []})
    assert "both required" in await _call(
        executor, "list_databricks_tables", catalog_name="main"
    )
    await _call(
        executor, "list_databricks_tables", catalog_name="main", schema_name="default"
    )
    assert recorder.calls[0]["params"]["omit_columns"] == "true"


@pytest.mark.asyncio
async def test_describe_table_requires_the_three_level_name(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    out = await _call(executor, "describe_databricks_table", full_name="main.default")
    assert "three-level Unity Catalog name" in out
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_describe_table_renders_the_column_schema(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        lambda m, u: {
            "full_name": "main.default.events",
            "table_type": "MANAGED",
            "data_source_format": "DELTA",
            "owner": "ops",
            "columns": [
                {"name": "id", "type_text": "BIGINT", "nullable": False},
                {"name": "note", "type_text": "STRING", "comment": "free text"},
            ],
        },
    )
    out = await _call(
        executor, "describe_databricks_table", full_name="main.default.events"
    )
    assert "id: BIGINT NOT NULL" in out
    assert "note: STRING - free text" in out
    assert "Columns (2)" in out
    # The three-level name keeps its dots through percent-encoding.
    assert recorder.calls[0]["url"].endswith(
        "/api/2.1/unity-catalog/tables/main.default.events"
    )


# ---------------------------------------------------------------------------
# Jobs & workflows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_jobs_renders_schedule_and_task_count(monkeypatch):
    executor, _ = _build(
        monkeypatch,
        lambda m, u: {
            "jobs": [
                {
                    "job_id": 42,
                    "creator_user_name": "ops@x.io",
                    "settings": {
                        "name": "Nightly ETL",
                        "tasks": [{"task_key": "a"}, {"task_key": "b"}],
                        "schedule": {
                            "quartz_cron_expression": "0 0 2 * * ?",
                            "pause_status": "UNPAUSED",
                        },
                    },
                }
            ]
        },
    )
    out = await _call(executor, "list_databricks_jobs")
    assert "Nightly ETL" in out
    assert "Job ID: 42" in out
    assert "Tasks: 2" in out
    assert "cron 0 0 2 * * ? (active)" in out


@pytest.mark.asyncio
async def test_run_job_requires_a_numeric_job_id(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    out = await _call(executor, "run_databricks_job", job_id="not-a-number")
    assert "must be a numeric Databricks job ID" in out
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_run_job_forwards_parameters_and_returns_the_run_id(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"run_id": 991})
    out = await _call(
        executor,
        "run_databricks_job",
        job_id="42",
        job_parameters='{"env": "prod"}',
        notebook_params='{"day": 3}',
    )
    assert "run ID 991" in out
    body = recorder.calls[0]["json"]
    assert body["job_id"] == 42
    assert body["job_parameters"] == {"env": "prod"}
    # Notebook widget values are always strings on the wire.
    assert body["notebook_params"] == {"day": "3"}


@pytest.mark.asyncio
async def test_run_job_rejects_malformed_parameter_json(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    out = await _call(executor, "run_databricks_job", job_id="42", job_parameters="[1]")
    assert "must be a JSON object" in out
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_list_job_runs_rejects_contradictory_filters(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"runs": []})
    out = await _call(
        executor, "list_databricks_job_runs", active_only=True, completed_only=True
    )
    assert "mutually exclusive" in out
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_get_job_run_chases_the_failing_task_output(monkeypatch):
    def _router(method, url):
        if url.endswith("/api/2.2/jobs/runs/get"):
            return {
                "run_id": 991,
                "job_id": 42,
                "run_name": "Nightly ETL",
                "state": {
                    "life_cycle_state": "TERMINATED",
                    "result_state": "FAILED",
                },
                "tasks": [
                    {
                        "task_key": "extract",
                        "run_id": 1001,
                        "state": {
                            "life_cycle_state": "TERMINATED",
                            "result_state": "SUCCESS",
                        },
                    },
                    {
                        "task_key": "load",
                        "run_id": 1002,
                        "state": {
                            "life_cycle_state": "TERMINATED",
                            "result_state": "FAILED",
                        },
                    },
                ],
            }
        return {"error": "AnalysisException: table not found", "error_trace": "at ..."}

    executor, recorder = _build(monkeypatch, _router)
    out = await _call(executor, "get_databricks_job_run", run_id="991")
    assert "TERMINATED/FAILED" in out
    assert "AnalysisException: table not found" in out
    # The output call targets the FAILING task's run id, not the parent run.
    output_call = recorder.calls[-1]
    assert output_call["url"].endswith("/api/2.2/jobs/runs/get-output")
    assert output_call["params"] == {"run_id": "1002"}


@pytest.mark.asyncio
async def test_get_job_run_skips_the_output_fetch_when_asked(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        lambda m, u: {
            "run_id": 991,
            "state": {"life_cycle_state": "TERMINATED", "result_state": "FAILED"},
            "tasks": [
                {
                    "task_key": "load",
                    "run_id": 1002,
                    "state": {
                        "life_cycle_state": "TERMINATED",
                        "result_state": "FAILED",
                    },
                }
            ],
        },
    )
    await _call(executor, "get_databricks_job_run", run_id="991", include_output=False)
    assert len(recorder.calls) == 1


@pytest.mark.asyncio
async def test_get_cluster_surfaces_the_termination_reason(monkeypatch):
    executor, _ = _build(
        monkeypatch,
        lambda m, u: {
            "cluster_id": "c-1",
            "cluster_name": "analytics",
            "state": "TERMINATED",
            "termination_reason": {
                "code": "INACTIVITY",
                "type": "SUCCESS",
                "parameters": {"reason": "idle for 30 minutes"},
            },
        },
    )
    out = await _call(executor, "get_databricks_cluster", cluster_id="c-1")
    assert "INACTIVITY" in out
    assert "idle for 30 minutes" in out


# ---------------------------------------------------------------------------
# Mosaic AI
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_serving_endpoint_unwraps_a_chat_completion(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        lambda m, u: {
            "choices": [{"message": {"role": "assistant", "content": "Hello there"}}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 3},
        },
    )
    out = await _call(
        executor, "query_databricks_serving_endpoint", name="llama-ep", prompt="Hi"
    )
    assert out.startswith("Hello there")
    assert "tokens: 8 in / 3 out" in out
    assert recorder.calls[0]["url"].endswith("/serving-endpoints/llama-ep/invocations")
    assert recorder.calls[0]["json"]["messages"] == [{"role": "user", "content": "Hi"}]


@pytest.mark.asyncio
async def test_query_serving_endpoint_returns_raw_json_for_a_custom_signature(
    monkeypatch,
):
    executor, recorder = _build(monkeypatch, lambda m, u: {"predictions": [0.1, 0.9]})
    out = await _call(
        executor,
        "query_databricks_serving_endpoint",
        name="scorer",
        payload='{"inputs": [[1, 2]]}',
    )
    assert "predictions" in out
    assert recorder.calls[0]["json"] == {"inputs": [[1, 2]]}


@pytest.mark.asyncio
async def test_query_serving_endpoint_requires_a_prompt_or_payload(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    out = await _call(executor, "query_databricks_serving_endpoint", name="ep")
    assert "either prompt" in out
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_query_vector_index_pairs_columns_with_values(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        lambda m, u: {
            "manifest": {"columns": [{"name": "id"}, {"name": "text"}]},
            "result": {"data_array": [["7", "a finding"]]},
        },
    )
    out = await _call(
        executor,
        "query_databricks_vector_index",
        index_name="main.rag.docs_index",
        query_text="ransomware",
        columns="id, text",
        filters='{"year >=": 2024}',
    )
    assert "id=7 | text=a finding" in out
    body = recorder.calls[0]["json"]
    assert body["columns"] == ["id", "text"]
    assert body["filters_json"] == '{"year >=": 2024}'
    assert recorder.calls[0]["url"].endswith(
        "/api/2.0/vector-search/indexes/main.rag.docs_index/query"
    )


@pytest.mark.asyncio
async def test_query_vector_index_reports_an_empty_result_set(monkeypatch):
    executor, _ = _build(monkeypatch, lambda m, u: {"result": {"data_array": []}})
    out = await _call(
        executor,
        "query_databricks_vector_index",
        index_name="main.rag.docs",
        query_text="nothing",
    )
    assert "No matching documents found." in out


# ---------------------------------------------------------------------------
# Volumes + files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_files_uses_the_files_api_for_a_volume_path(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        lambda m, u: {
            "contents": [
                {"path": "/Volumes/main/raw/vol/a.csv", "is_directory": False, "file_size": 12}
            ]
        },
    )
    out = await _call(executor, "list_databricks_files", path="/Volumes/main/raw/vol")
    assert "[file] /Volumes/main/raw/vol/a.csv (12 bytes)" in out
    assert recorder.calls[0]["url"].endswith(
        "/api/2.0/fs/directories/Volumes/main/raw/vol"
    )


@pytest.mark.asyncio
async def test_list_files_switches_to_dbfs_for_a_dbfs_path(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        lambda m, u: {"files": [{"path": "/logs/x.log", "is_dir": False, "file_size": 4}]},
    )
    out = await _call(executor, "list_databricks_files", path="dbfs:/logs")
    assert "[file] /logs/x.log" in out
    assert recorder.calls[0]["url"].endswith("/api/2.0/dbfs/list")
    assert recorder.calls[0]["params"]["path"] == "/logs"


@pytest.mark.asyncio
async def test_read_file_returns_a_bounded_text_preview(monkeypatch):
    executor, _ = _build(monkeypatch, lambda m, u: b"id,name\n1,alice\n")
    out = await _call(
        executor, "read_databricks_file", path="/Volumes/main/raw/vol/a.csv"
    )
    assert "id,name" in out
    assert "1,alice" in out


@pytest.mark.asyncio
async def test_read_file_truncates_at_max_bytes(monkeypatch):
    executor, _ = _build(monkeypatch, lambda m, u: b"x" * 1000)
    out = await _call(
        executor, "read_databricks_file", path="/Volumes/a/b/c/big.log", max_bytes=10
    )
    assert "truncated" in out
    assert out.split("\n", 1)[1] == "x" * 10


@pytest.mark.asyncio
async def test_read_file_reports_binary_content_instead_of_dumping_it(monkeypatch):
    executor, _ = _build(monkeypatch, lambda m, u: b"PK\x03\x04\x00\x00binary")
    out = await _call(executor, "read_databricks_file", path="/Volumes/a/b/c/x.zip")
    assert "looks like a binary file" in out


@pytest.mark.asyncio
async def test_read_file_decodes_the_base64_dbfs_envelope(monkeypatch):
    encoded = base64.b64encode(b"hello dbfs").decode()
    executor, recorder = _build(monkeypatch, lambda m, u: {"data": encoded})
    out = await _call(executor, "read_databricks_file", path="dbfs:/logs/a.txt")
    assert "hello dbfs" in out
    assert recorder.calls[0]["url"].endswith("/api/2.0/dbfs/read")


@pytest.mark.asyncio
async def test_read_file_rejects_a_malformed_dbfs_body(monkeypatch):
    executor, _ = _build(monkeypatch, lambda m, u: {"data": "!!!not-base64!!!"})
    out = await _call(executor, "read_databricks_file", path="dbfs:/logs/a.txt")
    assert "malformed body" in out


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_http_error_is_returned_verbatim_to_the_llm(monkeypatch):
    executor, _ = _build(monkeypatch, lambda m, u: (None, "Error: 403 Forbidden"))
    out = await _call(executor, "list_databricks_catalogs")
    assert out == "Error: 403 Forbidden"


@pytest.mark.asyncio
async def test_a_non_json_body_is_reported_as_a_misconfigured_url(monkeypatch):
    executor, _ = _build(monkeypatch, lambda m, u: _NON_JSON)
    out = await _call(executor, "list_databricks_catalogs")
    assert "non-JSON response" in out
    assert "login page or proxy" in out


@pytest.mark.asyncio
async def test_a_json_array_where_an_object_is_expected_is_reported(monkeypatch):
    executor, _ = _build(monkeypatch, lambda m, u: ["unexpected"])
    out = await _call(executor, "list_databricks_catalogs")
    assert "unexpected response shape" in out


# ---------------------------------------------------------------------------
# Integration class
# ---------------------------------------------------------------------------


def test_integration_metadata_is_declared():
    integration = DatabricksIntegration()
    assert integration.provider_name == "databricks"
    assert integration.display_name == "Databricks"
    assert integration.auth_types == ["api_key"]
    # OAuth here is client-credentials, not an authorization-code flow.
    assert integration.get_oauth_defaults() is None


@pytest.mark.asyncio
async def test_validate_connection_delegates_with_the_parsed_compound(monkeypatch):
    seen: list[tuple[str, str, str]] = []

    async def _fake_validate(host, client_id, secret):
        seen.append((host, client_id, secret))
        return True, "ok"

    monkeypatch.setattr(databricks_mod, "validate_connection", _fake_validate)
    assert await DatabricksIntegration().validate_connection(_OAUTH_TOKEN) is True
    assert seen == [(_HOST, "sp-client-id", "oauth-secret")]


@pytest.mark.asyncio
async def test_validate_connection_round_trips_a_secret_containing_the_delimiter(
    monkeypatch,
):
    seen: list[tuple[str, str, str]] = []

    async def _fake_validate(host, client_id, secret):
        seen.append((host, client_id, secret))
        return True, "ok"

    monkeypatch.setattr(databricks_mod, "validate_connection", _fake_validate)
    await DatabricksIntegration().validate_connection(
        f"{_HOST}||cid||wh||secret||with||pipes"
    )
    # greedy_last keeps the whole tail as the secret.
    assert seen[0][2] == "secret||with||pipes"


# ---------------------------------------------------------------------------
# Documented API page-size caps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tables_listing_respects_the_unity_catalog_50_page_cap(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"tables": []})
    await _call(
        executor,
        "list_databricks_tables",
        catalog_name="main",
        schema_name="default",
        limit=500,
    )
    assert recorder.calls[0]["params"]["max_results"] == 50


@pytest.mark.asyncio
async def test_every_pagination_hint_names_an_argument_the_tool_accepts(monkeypatch):
    """A hint like ``pass page_token="…"`` is a lie if the tool has no such arg."""
    paged = {
        "catalogs": [{"name": "main"}],
        "schemas": [{"name": "default"}],
        "tables": [{"name": "events"}],
        "volumes": [{"name": "raw"}],
        "clusters": [{"cluster_id": "c1"}],
        "contents": [{"path": "/Volumes/a/b/c/f", "is_directory": False}],
        "jobs": [{"job_id": 1, "settings": {"name": "j"}}],
        "runs": [{"run_id": 1, "state": {}}],
        "has_more": True,
        "next_page_token": "tok-next",
    }
    executor, _ = _build(monkeypatch, lambda m, u: paged)
    tools = {t.name: t for t in build_tools(token=_PAT_TOKEN, executor=executor)}
    args = {
        "list_databricks_schemas": {"catalog_name": "main"},
        "list_databricks_tables": {"catalog_name": "main", "schema_name": "default"},
        "list_databricks_volumes": {"catalog_name": "main", "schema_name": "default"},
        "list_databricks_files": {"path": "/Volumes/a/b/c"},
    }
    for name in [n for n in tools if n.startswith("list_databricks_")]:
        out = await _call(executor, name, **args.get(name, {}))
        if 'pass page_token=' not in out:
            continue
        props = tools[name].input_schema.get("properties", {})
        assert "page_token" in props, f"{name} hints page_token but does not accept it"


# ---------------------------------------------------------------------------
# Volume reads are bounded at the network layer, not just client-side
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_volume_read_requests_only_the_capped_byte_range(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: b"hello")
    await _call(
        executor, "read_databricks_file", path="/Volumes/a/b/c/f.csv", max_bytes=50
    )
    # bytes=0-50 is inclusive → 51 bytes, one over the cap, so the "is there
    # more?" test below stays exact without pulling the whole file.
    assert recorder.calls[0]["headers"]["Range"] == "bytes=0-50"


@pytest.mark.asyncio
async def test_a_range_bounded_read_still_detects_truncation(monkeypatch):
    # The server honours the range and returns exactly cap+1 bytes.
    executor, _ = _build(monkeypatch, lambda m, u: b"y" * 11)
    out = await _call(
        executor, "read_databricks_file", path="/Volumes/a/b/c/f.log", max_bytes=10
    )
    assert "truncated" in out
    assert out.split("\n", 1)[1] == "y" * 10


@pytest.mark.asyncio
async def test_a_read_exactly_at_the_cap_is_not_reported_as_truncated(monkeypatch):
    executor, _ = _build(monkeypatch, lambda m, u: b"y" * 10)
    out = await _call(
        executor, "read_databricks_file", path="/Volumes/a/b/c/f.log", max_bytes=10
    )
    assert "truncated" not in out
    assert out.split("\n", 1)[1] == "y" * 10


@pytest.mark.asyncio
async def test_an_empty_file_answering_416_falls_back_to_a_plain_read(monkeypatch):
    calls: list[dict[str, Any]] = []

    def _router(_method, _url):
        if not calls:
            calls.append({"ranged": True})
            return (
                _StubResponse({}, content=b"", status_code=416),
                "Error: 416 Range Not Satisfiable",
            )
        return b""

    executor, recorder = _build(monkeypatch, _router)
    out = await _call(executor, "read_databricks_file", path="/Volumes/a/b/c/empty.txt")
    # The 416 is swallowed, not surfaced as a tool error.
    assert "416" not in out
    assert "0 bytes shown" in out
    assert len(recorder.calls) == 2
    assert "Range" in recorder.calls[0]["headers"]
    assert "Range" not in recorder.calls[1]["headers"]


@pytest.mark.asyncio
async def test_a_genuine_error_on_a_ranged_read_is_still_surfaced(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        lambda m, u: (
            _StubResponse({}, content=b"", status_code=404),
            "Error: 404 Not Found",
        ),
    )
    out = await _call(executor, "read_databricks_file", path="/Volumes/a/b/c/gone.csv")
    assert out == "Error: 404 Not Found"
    # A 404 must NOT trigger the 416 empty-file fallback.
    assert len(recorder.calls) == 1


# ---------------------------------------------------------------------------
# Result sets are bounded server-side (INLINE is capped at 25 MiB and
# *cancels* the statement past it rather than truncating)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sql_bounds_the_result_set_on_the_server(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: _succeeded_payload([]))
    await _call(executor, "query_databricks_sql", statement="SELECT * FROM t")
    # Without row_limit an unbounded SELECT exceeds the 25 MiB INLINE ceiling
    # and Databricks cancels the statement, so the tool would hard-fail.
    assert recorder.calls[0]["json"]["row_limit"] == 100


@pytest.mark.asyncio
async def test_sql_row_limit_matches_the_rendered_limit(monkeypatch):
    rows = [[str(i), f"n{i}"] for i in range(40)]
    executor, recorder = _build(monkeypatch, lambda m, u: _succeeded_payload(rows))
    out = await _call(
        executor, "query_databricks_sql", statement="SELECT * FROM t", limit=25
    )
    # One number drives both the wire limit and the render limit, so they
    # cannot drift apart.
    assert recorder.calls[0]["json"]["row_limit"] == 25
    assert "Showing 25 of 40 fetched rows" in out


@pytest.mark.asyncio
async def test_sql_row_limit_is_clamped_to_the_hard_ceiling(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: _succeeded_payload([]))
    await _call(executor, "query_databricks_sql", statement="SELECT 1", limit=99999)
    assert recorder.calls[0]["json"]["row_limit"] == 500


@pytest.mark.asyncio
async def test_execute_sql_is_bounded_too(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: _succeeded_payload([]))
    await _call(executor, "execute_databricks_sql", statement="SELECT * FROM t")
    assert recorder.calls[0]["json"]["row_limit"] == 100


# ---------------------------------------------------------------------------
# A byte cap landing mid-character is a boundary artefact, not a binary file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_cap_splitting_a_multibyte_char_still_renders_as_text(monkeypatch):
    # 120 bytes of 2-byte characters, cut at an odd offset → mid-character.
    executor, _ = _build(monkeypatch, lambda m, u: ("é" * 60).encode())
    out = await _call(
        executor, "read_databricks_file", path="/Volumes/a/b/c/accents.csv", max_bytes=101
    )
    assert "not valid UTF-8" not in out
    assert "truncated" in out
    assert out.split("\n", 1)[1] == "é" * 50


@pytest.mark.parametrize("cap", list(range(98, 106)))
def test_render_file_is_text_at_every_offset_around_a_split(cap):
    from app.integrations.providers.databricks import _render_file

    out = _render_file("/f.csv", ("é" * 60).encode(), cap)
    assert "not valid UTF-8" not in out


def test_render_file_still_reports_a_genuine_mid_file_decode_failure():
    from app.integrations.providers.databricks import _render_file

    # The bad bytes are nowhere near the cut, so this is a real non-text file.
    out = _render_file("/f.bin", b"a" * 50 + b"\xff\xfe" + b"b" * 60, 200)
    assert "not valid UTF-8" in out


def test_render_file_does_not_repair_an_untruncated_invalid_file():
    from app.integrations.providers.databricks import _render_file

    # Nothing was cut, so a broken sequence is the file's own problem.
    out = _render_file("/f.csv", "é".encode()[:1], 500)
    assert "not valid UTF-8" in out


def test_render_file_still_reports_binary_content():
    from app.integrations.providers.databricks import _render_file

    out = _render_file("/f.zip", b"PK\x03\x04\x00\x00junk", 200)
    assert "looks like a binary file" in out


# ---------------------------------------------------------------------------
# DBFS reads report truncation like volume reads do
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dbfs_read_asks_for_one_byte_past_the_cap(monkeypatch):
    encoded = base64.b64encode(b"z" * 11).decode()
    executor, recorder = _build(monkeypatch, lambda m, u: {"data": encoded})
    out = await _call(
        executor, "read_databricks_file", path="dbfs:/logs/big.log", max_bytes=10
    )
    # Asking for exactly `cap` makes a cut file indistinguishable from a whole
    # one, so the agent would reason over a silent truncation.
    assert recorder.calls[0]["params"]["length"] == 11
    assert "truncated" in out
    assert out.split("\n", 1)[1] == "z" * 10


@pytest.mark.asyncio
async def test_dbfs_read_of_a_complete_file_is_not_flagged_truncated(monkeypatch):
    encoded = base64.b64encode(b"z" * 8).decode()
    executor, _ = _build(monkeypatch, lambda m, u: {"data": encoded})
    out = await _call(
        executor, "read_databricks_file", path="dbfs:/logs/small.log", max_bytes=10
    )
    assert "truncated" not in out


@pytest.mark.asyncio
async def test_dbfs_read_length_stays_inside_the_1mib_single_read_limit(monkeypatch):
    encoded = base64.b64encode(b"z").decode()
    executor, recorder = _build(monkeypatch, lambda m, u: {"data": encoded})
    await _call(
        executor, "read_databricks_file", path="dbfs:/logs/x.log", max_bytes=10**9
    )
    assert recorder.calls[0]["params"]["length"] <= 1_048_576


# ---------------------------------------------------------------------------
# Documented API semantics: exact job-name filter, runs/list ceiling,
# legitimately-empty pages
# ---------------------------------------------------------------------------


def test_job_name_filter_is_documented_as_exact_not_substring():
    executor = CallableToolExecutor()
    tools = {t.name: t for t in build_tools(token=_PAT_TOKEN, executor=executor)}
    jobs = tools["list_databricks_jobs"]
    prop = jobs.input_schema["properties"]["name_filter"]["description"].lower()
    # The Jobs API matches the whole name; promising a substring filter makes an
    # agent read an empty list as "the job does not exist".
    assert "exact" in prop
    assert "not a substring" in prop
    assert "substring" not in jobs.description.lower().replace("not a substring", "")


@pytest.mark.asyncio
async def test_job_runs_limit_respects_the_documented_sub_25_ceiling(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"runs": []})
    await _call(executor, "list_databricks_job_runs", limit=500)
    assert recorder.calls[0]["params"]["limit"] == 24


@pytest.mark.asyncio
async def test_job_runs_default_limit_is_inside_the_ceiling(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"runs": []})
    await _call(executor, "list_databricks_job_runs")
    # The tool's own default must not be a value the API would reject.
    assert recorder.calls[0]["params"]["limit"] < 25


def test_job_runs_schema_default_matches_the_clamped_default():
    executor = CallableToolExecutor()
    tools = {t.name: t for t in build_tools(token=_PAT_TOKEN, executor=executor)}
    assert (
        tools["list_databricks_job_runs"].input_schema["properties"]["limit"]["default"]
        < 25
    )


@pytest.mark.asyncio
async def test_an_empty_page_with_a_next_token_does_not_read_as_end_of_results(
    monkeypatch,
):
    executor, _ = _build(
        monkeypatch, lambda m, u: {"catalogs": [], "next_page_token": "tok-2"}
    )
    out = await _call(executor, "list_databricks_catalogs")
    # Unity Catalog documents that a page may be empty while more pages follow,
    # and that only a missing token means the end. "No catalogs found." next to
    # "more results available" is a contradiction the agent resolves by stopping.
    assert "No catalogs found." not in out
    assert "more pages follow" in out
    assert 'page_token="tok-2"' in out


@pytest.mark.asyncio
async def test_a_genuinely_final_empty_page_still_says_nothing_was_found(monkeypatch):
    executor, _ = _build(monkeypatch, lambda m, u: {"catalogs": []})
    out = await _call(executor, "list_databricks_catalogs")
    assert "No catalogs found." in out
    assert "more pages follow" not in out


@pytest.mark.asyncio
async def test_empty_page_wording_applies_to_every_paginated_listing(monkeypatch):
    payload = {
        "catalogs": [], "schemas": [], "tables": [], "volumes": [], "clusters": [],
        "jobs": [], "runs": [], "contents": [],
        "has_more": True, "next_page_token": "tok-2",
    }
    executor, _ = _build(monkeypatch, lambda m, u: payload)
    cases = {
        "list_databricks_catalogs": {},
        "list_databricks_schemas": {"catalog_name": "main"},
        "list_databricks_tables": {"catalog_name": "main", "schema_name": "default"},
        "list_databricks_volumes": {"catalog_name": "main", "schema_name": "default"},
        "list_databricks_clusters": {},
        "list_databricks_jobs": {},
        "list_databricks_job_runs": {},
        "list_databricks_files": {"path": "/Volumes/a/b/c"},
    }
    for tool, args in cases.items():
        out = await _call(executor, tool, **args)
        assert "more pages follow" in out, f"{tool} contradicts its pagination hint"


# ---------------------------------------------------------------------------
# The reported byte count is the count actually rendered
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cap", list(range(96, 108)))
def test_render_file_reports_the_byte_count_it_actually_rendered(cap):
    """The header must not claim bytes the repair path dropped.

    On the multi-byte repair path the preview is shorter than the cap by up to
    3 bytes, so reporting ``len(sample)`` would overstate it. ``exc.start`` is
    the exact retained length, which keeps the count honest in O(1) instead of
    re-encoding the decoded text to measure it.
    """
    from app.integrations.providers.databricks import _render_file

    out = _render_file("/f.csv", ("é" * 60).encode(), cap)
    header, body = out.split("\n", 1)
    reported = int(header.split("(")[1].split(" ")[0])
    assert reported == len(body.encode()), f"cap={cap}: header lies about the size"


def test_render_file_byte_count_is_exact_on_the_plain_path_too():
    from app.integrations.providers.databricks import _render_file

    out = _render_file("/f.txt", b"hello world", 5)
    header, body = out.split("\n", 1)
    reported = int(header.split("(")[1].split(" ")[0])
    assert reported == len(body.encode()) == 5
