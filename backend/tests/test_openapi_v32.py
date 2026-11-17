"""OpenAPI 3.2: QUERY and custom methods, HEAD / OPTIONS, querystring, sequential bodies, $self, tags."""

from __future__ import annotations

import json
import socket
from typing import Any

import httpx
import pytest

from app.services import web_fetch
from app.services.declarative_http_executor import execute_declarative_tool, validate_execution_spec
from app.services.openapi_import.document import parse_spec_document
from app.services.openapi_import.operations import analyze_spec

SPEC = """openapi: 3.2.0
$self: https://specs.acme.test/catalog/openapi.yaml
info: {title: Acme Catalog, version: "2"}
servers:
  - url: ../api/v2
    name: Production
    description: The live API
tags:
  - name: products
    summary: Products
    description: The product catalog
  - name: search
    summary: Search
    parent: products
    kind: nav
paths:
  /products:
    query:
      operationId: searchProducts
      tags: [search]
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              properties:
                text: {type: string}
      responses:
        "200": {description: ok}
    options:
      operationId: productOptions
      responses:
        "204": {description: allowed methods}
    additionalOperations:
      PURGE:
        operationId: purgeProducts
        responses:
          "204": {description: purged}
      post:
        operationId: notAllowedHere
        responses:
          "201": {description: created}
  /products/{sku}:
    parameters:
      - {name: sku, in: path, required: true, schema: {type: string}}
    head:
      operationId: productExists
      responses:
        "200": {description: ok}
    trace:
      operationId: traceProduct
      responses:
        "200": {description: echo}
  /reports:
    get:
      operationId: report
      parameters:
        - name: filter
          in: querystring
          required: true
          content:
            application/x-www-form-urlencoded:
              schema:
                type: object
                properties:
                  status: {type: string}
                  tags: {type: array, items: {type: string}}
      responses:
        "200": {description: ok}
  /search:
    get:
      operationId: jsonSearch
      parameters:
        - name: q
          in: querystring
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Query'
      responses:
        "200": {description: ok}
  /imports:
    post:
      operationId: importProducts
      requestBody:
        content:
          application/jsonl:
            itemSchema:
              $ref: '#/components/schemas/Product'
      responses:
        "202": {description: accepted}
  /events:
    post:
      operationId: sendEvents
      requestBody:
        required: true
        content:
          application/json-seq:
            itemSchema: {type: object, properties: {kind: {type: string}}}
      responses:
        "202": {description: accepted}
  /labels:
    post:
      operationId: createLabel
      requestBody:
        required: true
        content:
          application/json:
            $ref: '#/components/mediaTypes/Label'
      responses:
        "201": {description: created}
  /session:
    get:
      operationId: session
      parameters:
        - {name: sid, in: cookie, style: cookie, schema: {type: string}}
      responses:
        "200": {description: ok}
components:
  schemas:
    Product:
      type: object
      required: [sku]
      properties:
        sku: {type: string}
        price: {type: number}
    Query:
      type: object
      properties:
        numbers: {type: array, items: {type: integer}}
  mediaTypes:
    Label:
      schema:
        type: object
        required: [name]
        properties:
          name: {type: string}
"""


@pytest.fixture(scope="module")
def analysis():
    return analyze_spec(parse_spec_document(SPEC))


def _ops(analysis) -> dict[str, Any]:
    return {op.key: op for op in analysis.operations}


def test_every_method_becomes_a_tool_but_trace(analysis):
    ops = _ops(analysis)
    assert ops["QUERY /products"].supported and ops["QUERY /products"].tool_type == "read"
    assert ops["QUERY /products"].tool["execution"]["method"] == "QUERY"
    assert ops["OPTIONS /products"].supported and ops["OPTIONS /products"].tool_type == "read"
    assert ops["HEAD /products/{sku}"].supported and ops["HEAD /products/{sku}"].tool_type == "read"
    purge = ops["PURGE /products"]
    assert purge.supported and purge.tool_type == "write"
    assert purge.tool["execution"]["method"] == "PURGE"
    assert purge.tool_name == "acme_catalog_purge_products"
    trace = ops["TRACE /products/{sku}"]
    assert not trace.supported and "echoes the request back" in trace.skip_reason
    # additionalOperations may not repeat a fixed method.
    assert "POST /products" not in ops
    assert any("repeats the POST method" in w for w in analysis.warnings)


def test_a_connect_operation_is_listed_as_never_a_tool():
    document = parse_spec_document(
        "openapi: 3.2.0\n"
        "info: {title: Tunnel, version: '1'}\n"
        "servers: [{url: 'https://api.acme.test'}]\n"
        "paths:\n"
        "  /tunnel:\n"
        "    additionalOperations:\n"
        "      CONNECT: {operationId: openTunnel, responses: {'200': {description: ok}}}\n"
    )
    (entry,) = analyze_spec(document).operations
    assert entry.key == "CONNECT /tunnel" and not entry.supported
    assert entry.skip_reason == (
        "HTTP CONNECT operations are never tools: it opens a tunnel through the server "
        "instead of calling the API."
    )


def test_a_query_body_is_sent(analysis):
    tool = _ops(analysis)["QUERY /products"].tool
    assert tool["execution"]["body_encoding"] == "json"
    assert tool["input_schema"]["properties"] == {"text": {"type": "string"}}


def test_self_is_the_base_of_relative_servers(analysis):
    assert analysis.servers == [
        {"url": "https://specs.acme.test/api/v2", "description": "The live API", "name": "Production"}
    ]


def test_tags_keep_their_summary_and_parent(analysis):
    assert analysis.tags[:2] == [
        {"name": "products", "description": "The product catalog", "summary": "Products", "parent": None},
        {"name": "search", "description": None, "summary": "Search", "parent": "products"},
    ]


def test_querystring_parameters(analysis):
    report = _ops(analysis)["GET /reports"].tool
    assert report["execution"]["querystring"] == {
        "from": "filter",
        "media_type": "application/x-www-form-urlencoded",
        "required": True,
    }
    assert report["input_schema"]["required"] == ["filter"]
    assert "whole query string" in report["input_schema"]["properties"]["filter"]["description"]
    search = _ops(analysis)["GET /search"].tool
    assert search["execution"]["querystring"]["media_type"] == "application/json"
    assert search["input_schema"]["properties"]["q"]["properties"]["numbers"]["items"] == {"type": "integer"}


def test_sequential_bodies_take_a_list_of_items(analysis):
    imports = _ops(analysis)["POST /imports"].tool
    assert imports["execution"]["body_encoding"] == "jsonl"
    assert imports["execution"]["body_from"] == "items"
    assert imports["input_schema"]["properties"]["items"]["items"]["required"] == ["sku"]
    events = _ops(analysis)["POST /events"].tool
    assert events["execution"]["body_encoding"] == "json-seq"
    assert events["input_schema"]["required"] == ["items"]


def test_media_type_objects_can_be_references(analysis):
    label = _ops(analysis)["POST /labels"].tool
    assert label["input_schema"]["required"] == ["name"]


def test_cookie_style(analysis):
    session = _ops(analysis)["GET /session"].tool
    assert session["execution"]["cookie_mapping"] == {"sid": {"from": "sid", "style": "cookie"}}


def test_every_generated_spec_is_valid(analysis):
    for op in analysis.operations:
        if op.tool:
            assert validate_execution_spec(op.tool["execution"], tool_index=0) == [], op.key


def test_methods_before_32_stay_as_they_were():
    older = SPEC.replace("openapi: 3.2.0", "openapi: 3.1.0")
    ops = _ops(analyze_spec(parse_spec_document(older)))
    assert "QUERY /products" not in ops and "PURGE /products" not in ops
    assert ops["HEAD /products/{sku}"].supported


# ── Executor ─────────────────────────────────────────────────────────────────


_REAL_ASYNC_CLIENT = httpx.AsyncClient


@pytest.fixture
def mock_http(monkeypatch):
    def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(web_fetch.socket, "getaddrinfo", fake_getaddrinfo)
    seen: list[httpx.Request] = []
    answers: dict[str, httpx.Response] = {}
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answers.get(request.method) or httpx.Response(200, json={"ok": True})

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        captured.update(kwargs)
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen, answers, captured


async def _run(spec: dict[str, Any], arguments: dict[str, Any], **kwargs: Any) -> str:
    return await execute_declarative_tool(
        spec,
        arguments,
        token="__no_auth__",
        extra_config={"base_url": "https://api.acme.test"},
        provider_name="Acme",
        tool_name="t",
        enforce_destination_guard=True,
        **kwargs,
    )


async def test_head_answers_with_the_status_and_headers(mock_http):
    seen, answers, _ = mock_http
    answers["HEAD"] = httpx.Response(
        200, headers={"ETag": '"v3"', "Content-Length": "42", "Set-Cookie": "session=secret"}
    )
    out = json.loads(await _run({"method": "HEAD", "path": "/products/x"}, {}))
    assert out["status"] == 200
    headers = {k.lower(): v for k, v in out["headers"].items()}
    assert headers["etag"] == '"v3"' and "set-cookie" not in headers
    assert seen[0].method == "HEAD"


async def test_options_returns_the_body_when_there_is_one(mock_http):
    _seen, answers, _ = mock_http
    answers["OPTIONS"] = httpx.Response(204, headers={"Allow": "GET, QUERY, PURGE"})
    assert json.loads(await _run({"method": "OPTIONS", "path": "/products"}, {}))["headers"]["allow"] == "GET, QUERY, PURGE"
    answers["OPTIONS"] = httpx.Response(200, json={"methods": ["GET"]})
    assert json.loads(await _run({"method": "OPTIONS", "path": "/products"}, {})) == {"methods": ["GET"]}


async def test_custom_and_query_methods_are_sent_as_declared(mock_http):
    seen, _answers, _ = mock_http
    await _run({"method": "PURGE", "path": "/products"}, {})
    await _run({"method": "query", "path": "/products", "body_from": "body"}, {"body": {"text": "red"}})
    assert [r.method for r in seen] == ["PURGE", "QUERY"]
    assert json.loads(seen[1].content) == {"text": "red"}


@pytest.mark.parametrize("method", ["TRACE", "CONNECT", "connect"])
async def test_trace_and_connect_are_never_sent(mock_http, method):
    seen, _answers, _ = mock_http
    assert f"cannot send {method.upper()}" in await _run({"method": method, "path": "/x"}, {})
    assert seen == []


async def test_a_form_querystring_is_the_whole_query(mock_http):
    seen, _answers, _ = mock_http
    spec = {
        "method": "GET",
        "path": "/reports",
        "querystring": {"from": "filter", "media_type": "application/x-www-form-urlencoded", "required": True},
    }
    await _run(spec, {"filter": {"status": "open", "tags": ["a", "b"]}})
    assert seen[0].url.query == b"status=open&tags=a&tags=b"
    assert "Required query string" in await _run(spec, {})


async def test_a_json_querystring_is_percent_encoded(mock_http):
    seen, _answers, _ = mock_http
    spec = {"method": "GET", "path": "/search", "querystring": {"from": "q", "media_type": "application/json"}}
    await _run(spec, {"q": {"numbers": [1, 2], "flag": None}})
    assert seen[0].url.query == b"%7B%22numbers%22%3A%5B1%2C2%5D%2C%22flag%22%3Anull%7D"


async def test_sequential_bodies(mock_http):
    seen, _answers, _ = mock_http
    jsonl = {"method": "POST", "path": "/imports", "body_from": "items", "body_encoding": "jsonl", "content_type": "application/jsonl"}
    await _run(jsonl, {"items": [{"sku": "a"}, {"sku": "b", "price": 2.5}]})
    assert seen[0].content == b'{"sku":"a"}\n{"sku":"b","price":2.5}\n'
    assert seen[0].headers["Content-Type"] == "application/jsonl"
    seq = {"method": "POST", "path": "/events", "body_from": "items", "body_encoding": "json-seq", "content_type": "application/json-seq"}
    await _run(seq, {"items": [{"kind": "x"}]})
    assert seen[1].content == b'\x1e{"kind":"x"}\n'
    assert "list of items" in await _run(jsonl, {"items": {"sku": "a"}})


async def test_a_client_certificate_rides_on_the_pinned_connection(mock_http, monkeypatch):
    import ssl

    _seen, _answers, captured = mock_http
    monkeypatch.setattr(web_fetch, "_environment_proxies", dict)
    context = ssl.create_default_context()
    await _run({"method": "GET", "path": "/x"}, {}, ssl_context=context)
    assert captured["transport"]._pool._ssl_context is context
    # Behind a proxy the certificate is still presented, through the proxy
    # and on the direct (NO_PROXY) connections alike.
    monkeypatch.setattr(
        web_fetch,
        "_environment_proxies",
        lambda: {"all://": "http://proxy.corp.test:3128", "all://*.internal.test": None},
    )
    await _run({"method": "GET", "path": "/x"}, {}, ssl_context=context)
    assert captured["transport"]._pool._ssl_context is context
    assert captured["mounts"]["all://"]._pool._ssl_context is context
