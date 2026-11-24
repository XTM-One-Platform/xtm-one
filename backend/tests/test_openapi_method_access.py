"""Imported OpenAPI operations: access by HTTP method, relative OAuth URLs on replace, the operation table."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.services.integration_read_only import withhold_write_tools
from app.services.openapi_import.builder_tools import tools_line
from app.services.openapi_import.document import OpenAPIImportError, parse_spec_document
from app.services.openapi_import.operations import analyze_spec, auth_url_base, method_access
from app.services.openapi_import.service import (
    imported_operations,
    operation_table,
    resolve_auth_option,
)
from app.tools.base import CallableToolExecutor, ToolDefinition


@pytest.mark.parametrize(
    ("method", "access"),
    [
        ("GET", "read"),
        ("get", "read"),
        ("HEAD", "read"),
        ("OPTIONS", "read"),
        ("QUERY", "read"),
        ("POST", "write"),
        ("PUT", "write"),
        ("PATCH", "write"),
        ("DELETE", "write"),
        ("COPY", "write"),
        ("", "write"),
        (None, "write"),
    ],
)
def test_method_access_reads_the_http_method(method, access):
    assert method_access(method) == access


_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Ops", "version": "1"},
    "servers": [{"url": "https://ops.example.test/api"}],
    "paths": {
        "/upgrade-schedule/": {
            "get": {"operationId": "createUpgradeSchedule", "responses": {"200": {"description": "ok"}}}
        },
        "/charts/": {"post": {"operationId": "listCharts", "responses": {"200": {"description": "ok"}}}},
    },
}


def test_a_get_named_create_is_a_read_and_a_post_named_list_a_write():
    analysis = analyze_spec(parse_spec_document(json.dumps(_SPEC)))
    access = {op.method: op.tool_type for op in analysis.operations}
    assert access == {"GET": "read", "POST": "write"}


def _tools():
    return [
        {"name": "ops_create_upgrade_schedule", "execution": {"method": "GET", "path": "/upgrade-schedule/"}},
        {"name": "ops_list_charts", "execution": {"method": "POST", "path": "/charts/"}},
    ]


def test_read_only_binding_keeps_a_get_declared_read_only_whatever_its_name():
    executor = CallableToolExecutor()
    defs = [
        ToolDefinition(
            name=t["name"],
            description="",
            input_schema={"type": "object", "properties": {}},
            read_only=method_access(t["execution"]["method"]) == "read",
            writes=method_access(t["execution"]["method"]) == "write",
        )
        for t in _tools()
    ]
    kept = withhold_write_tools(defs, executor, integration_label="Ops")
    assert [d.name for d in kept] == ["ops_create_upgrade_schedule"]


def test_imported_operations_and_table_carry_the_method_access():
    ops = imported_operations(_tools())
    assert [op["access"] for op in ops] == ["read", "write"]
    table = operation_table(_tools())
    assert "  - ops_create_upgrade_schedule [read] `GET /upgrade-schedule/`" in table
    assert "  - ops_list_charts [write] `POST /charts/`" in table


def test_tools_line_answers_the_operation_table():
    line = tools_line(SimpleNamespace(active_tools=_tools(), warnings=[]))
    assert "Tools (2: 1 read, 1 write):" in line
    assert "[read] `GET /upgrade-schedule/`" in line
    assert "none selected" in tools_line(SimpleNamespace(active_tools=[], warnings=[]))


_RELATIVE_OAUTH = {
    "openapi": "3.0.3",
    "info": {"title": "Relative", "version": "1"},
    "paths": {"/items": {"get": {"responses": {"200": {"description": "ok"}}}}},
    "components": {
        "securitySchemes": {
            "oauth": {
                "type": "oauth2",
                "flows": {"clientCredentials": {"tokenUrl": "/o/token/", "scopes": {}}},
            }
        }
    },
    "security": [{"oauth": []}],
}


def _token_urls(analysis):
    return [o.token_url for o in analysis.auth_options if o.supported and o.token_url]


def test_relative_oauth_urls_of_an_uploaded_file_resolve_against_the_integration_server():
    document = parse_spec_document(json.dumps(_RELATIVE_OAUTH))
    assert not _token_urls(analyze_spec(document))
    fallback = "https://airflow.example.test/api/v1"
    assert auth_url_base(document, fallback) == fallback
    analysis = analyze_spec(document, server_url_fallback=fallback)
    assert _token_urls(analysis) == ["https://airflow.example.test/o/token/"]


def test_relative_oauth_urls_prefer_the_document_servers_over_the_fallback():
    spec = {**_RELATIVE_OAUTH, "servers": [{"url": "https://own.example.test/"}]}
    analysis = analyze_spec(parse_spec_document(json.dumps(spec)), server_url_fallback="https://other.example.test/")
    assert _token_urls(analysis) == ["https://own.example.test/o/token/"]


def test_unknown_auth_option_names_the_valid_ones():
    analysis = analyze_spec(parse_spec_document(json.dumps(_SPEC)))
    with pytest.raises(OpenAPIImportError) as exc:
        resolve_auth_option(analysis, "nope")
    assert "Valid options:" in str(exc.value)
