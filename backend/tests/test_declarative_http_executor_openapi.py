"""Declarative executor features used by OpenAPI integrations (and bundles alike).

Requests go through a real ``httpx.AsyncClient`` on a ``MockTransport`` so the
assertions read what would actually be on the wire.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
from typing import Any

import httpx
import pytest

from app.services import web_fetch
from app.services.declarative_http_executor import (
    _build_query_params,
    execute_declarative_tool,
    validate_execution_spec,
)

_REAL_ASYNC_CLIENT = httpx.AsyncClient


class _Recorder:
    def __init__(self, responses: list[httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = list(responses or [])

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._responses:
            return self._responses.pop(0)
        return httpx.Response(200, json={"ok": True})


@pytest.fixture
def wire(monkeypatch):
    recorder = _Recorder()

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(recorder.handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return recorder


async def _run(spec: dict[str, Any], args: dict[str, Any], token: str = "tk", **kwargs: Any) -> str:  # noqa: S107 - test literal, not a credential
    return await execute_declarative_tool(
        spec,
        args,
        token=token,
        extra_config={"base_url": "https://api.example.test/v1"},
        provider_name="Demo",
        tool_name="demo",
        **kwargs,
    )


def _query(request: httpx.Request) -> list[tuple[str, str]]:
    return list(request.url.params.multi_items())


# ── Query serialisation ──────────────────────────────────────────────────────


async def test_query_styles(wire):
    spec = {
        "method": "GET",
        "path": "/pets",
        "query_mapping": {
            "status": {"from": "status"},
            "tags": {"from": "tags", "explode": False},
            "ids": {"from": "ids", "style": "pipeDelimited"},
            "filter": {"from": "filter", "style": "deepObject", "explode": True},
            "active": {"from": "active"},
            "q": {"from": "q", "style": "json"},
        },
        "auth": {"type": "none"},
    }
    await _run(
        spec,
        {
            "status": ["available", "sold"],
            "tags": ["a", "b"],
            "ids": [1, 2],
            "filter": {"kind": "dog", "min": 2},
            "active": True,
            "q": {"name": "rex"},
        },
    )
    assert _query(wire.requests[0]) == [
        ("status", "available"),
        ("status", "sold"),
        ("tags", "a,b"),
        ("ids", "1|2"),
        ("filter[kind]", "dog"),
        ("filter[min]", "2"),
        ("active", "true"),
        ("q", '{"name":"rex"}'),
    ]


def test_build_query_params_keeps_the_dict_shape():
    assert _build_query_params({"a": {"from": "a"}, "b": {"from": "b"}}, {"a": 1, "b": False}) == {
        "a": "1",
        "b": "false",
    }
    assert _build_query_params({"t": {"from": "t"}}, {"t": ["x", "y"]}) == {"t": ["x", "y"]}


async def test_exploded_form_object_spreads_its_properties(wire):
    spec = {"method": "GET", "path": "/s", "query_mapping": {"opts": {"from": "opts", "style": "form"}}}
    await _run(spec, {"opts": {"page": 2, "size": 10}})
    assert _query(wire.requests[0]) == [("page", "2"), ("size", "10")]


@pytest.mark.parametrize("mapping", ["filter", {"from": "filter"}, {"from": "filter", "explode": True}])
async def test_an_object_never_names_the_parameters_of_a_mapping_without_a_style(wire, mapping):
    """A bundle mapping sends under its declared name, whatever the model passes."""
    spec = {"method": "GET", "path": "/s", "query_mapping": {"filter": mapping}}
    await _run(spec, {"filter": {"admin": "true", "limit": "9999"}})
    assert _query(wire.requests[0]) == [("filter", '{"admin":"true","limit":"9999"}')]


async def test_a_missing_required_query_parameter_is_left_to_the_input_schema(wire):
    """``required`` in a query mapping was never enforced by the runtime: a
    bundle declaring it keeps working when the argument is omitted."""
    spec = {"method": "GET", "path": "/s", "query_mapping": {"q": {"from": "q", "required": True}}}
    result = await _run(spec, {})
    assert not result.startswith("Error:")
    assert _query(wire.requests[0]) == []


# ── Path, headers, cookies ───────────────────────────────────────────────────


async def test_path_mapping_renames_and_encodes(wire):
    spec = {
        "method": "GET",
        "path": "/items/{item-id}/tags/{tags}",
        "path_params": ["item-id", "tags"],
        "path_mapping": {"item-id": {"from": "item_id", "required": True}, "tags": {"from": "tags"}},
    }
    await _run(spec, {"item_id": "a/b c", "tags": ["x", "y"]})
    assert wire.requests[0].url.raw_path.decode() == "/v1/items/a%2Fb%20c/tags/x,y"


@pytest.mark.parametrize(
    "value", ["..", ".", "", [".."], ["."], [], [None], {}, {"a": None}]
)
async def test_path_values_that_reshape_the_path_are_refused(wire, value):
    """Judged on the segment sent: a list serialises to its items."""
    spec = {"method": "GET", "path": "/files/{name}", "path_params": ["name"]}
    result = await _run(spec, {"name": value})
    assert result.startswith("Error: Path parameter 'name'")
    assert wire.requests == []


_DOT_SEGMENT_REFUSAL = (
    "Error: Path parameter 'name' must be a non-empty value that is not, and does "
    "not decode to, a '.' or '..' segment."
)


@pytest.mark.parametrize(
    "value",
    [
        # literal
        "a/../b",
        "../admin",
        "a/.",
        "a\\..\\b",
        "..;x",
        # single-encoded: a proxy that decodes once sees the dot segment
        "%2e%2e",
        "%2E%2e",
        ".%2e",
        "%2e",
        # double-encoded: a proxy and an application that each decode once
        "%252e%252e",
        "%25252e%25252e",
        # separator-smuggled
        "a%2F..%2Fb",
        "a%2f%2e%2e%2fb",
        "a%5C..%5Cb",
        "a%252F..%252Fb",
        "..%3Bx",
        # a serialised list item
        ["%2e%2e"],
        # nested deeper than the decoding bound
        "%" + "25" * 9 + "2e",
    ],
)
async def test_path_values_that_decode_to_a_dot_segment_are_refused(wire, value):
    """Each decoding a proxy or an application may apply is judged: a value
    that becomes ``..`` there would land the credentialed call on another
    endpoint."""
    spec = {"method": "GET", "path": "/files/{name}", "path_params": ["name"]}
    result = await _run(spec, {"name": value})
    assert result == _DOT_SEGMENT_REFUSAL
    assert wire.requests == []


async def test_a_json_path_value_that_decodes_to_a_dot_segment_is_refused(wire):
    spec = {
        "method": "GET",
        "path": "/files/{name}",
        "path_params": ["name"],
        "path_mapping": {"name": {"from": "name", "style": "json"}},
    }
    result = await _run(spec, {"name": {"k": "x/../y"}})
    assert result == _DOT_SEGMENT_REFUSAL
    assert wire.requests == []


@pytest.mark.parametrize(
    ("value", "raw_path"),
    [
        ("v1..2", "/v1/files/v1..2"),
        ("..x", "/v1/files/..x"),
        ("a/b", "/v1/files/a%2Fb"),
        ("a;b", "/v1/files/a%3Bb"),
        ("100%", "/v1/files/100%25"),
        ("%zz", "/v1/files/%25zz"),
        ("%2e%2ex", "/v1/files/%252e%252ex"),
    ],
)
async def test_path_values_with_dots_that_are_not_a_dot_segment_are_sent(wire, value, raw_path):
    spec = {"method": "GET", "path": "/files/{name}", "path_params": ["name"]}
    result = await _run(spec, {"name": value})
    assert result == '{"ok": true}'
    assert len(wire.requests) == 1
    assert wire.requests[0].url.raw_path.decode() == raw_path


async def test_header_and_cookie_mappings(wire):
    spec = {
        "method": "GET",
        "path": "/x",
        "header_mapping": {"X-Tenant": {"from": "tenant"}, "X-Ids": {"from": "ids"}},
        "cookie_mapping": {"locale": {"from": "locale"}},
        "auth": {"type": "cookie", "cookie": "SESSION"},
    }
    await _run(spec, {"tenant": "acme", "ids": [1, 2], "locale": "fr FR"}, token="s3cr3t")
    request = wire.requests[0]
    assert request.headers["X-Tenant"] == "acme"
    assert request.headers["X-Ids"] == "1,2"
    assert request.headers["Cookie"] == "locale=fr%20FR; SESSION=s3cr3t"


async def test_header_values_cannot_inject_lines(wire):
    spec = {"method": "GET", "path": "/x", "header_mapping": {"X-Note": {"from": "note"}}}
    result = await _run(spec, {"note": "ok\r\nX-Evil: 1"})
    assert result.startswith("Error: Header 'X-Note'")
    assert wire.requests == []


async def test_accept_and_server_url(wire):
    spec = {
        "method": "GET",
        "path": "/export",
        "accept": "application/json",
        "server_url": "https://exports.example.test",
    }
    await _run(spec, {})
    request = wire.requests[0]
    assert str(request.url) == "https://exports.example.test/export"
    assert request.headers["Accept"] == "application/json"


# ── Credentials ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "auth",
    [
        {"type": "bearer"},
        {"type": "query", "param": "api_key"},
        {"type": "cookie", "cookie": "SESSION"},
        {"type": "header", "header": "X-API-Key", "prefix": ""},
    ],
)
async def test_a_server_on_another_origin_gets_no_credential(wire, auth):
    """The spec chose that server, not the integration: its secret stays home."""
    minted: list[str] = []

    async def provider() -> str:
        minted.append("token")
        return "minted"

    spec = {"method": "GET", "path": "/x", "server_url": "https://collector.example.org", "auth": auth}
    await _run(spec, {}, token="s3cr3t", token_provider=provider)
    request = wire.requests[0]
    assert "s3cr3t" not in str(request.url) and "minted" not in str(request.url)
    assert not {"authorization", "cookie", "x-api-key"} & set(request.headers.keys())
    assert minted == []


@pytest.mark.parametrize(
    "server_url",
    ["https://api.example.test/v2", "https://API.example.test:443/v2", "https://api.example.test/v1"],
)
async def test_a_server_on_the_same_origin_gets_the_credential(wire, server_url):
    spec = {"method": "GET", "path": "/x", "server_url": server_url}
    await _run(spec, {}, token="s3cr3t")
    assert wire.requests[0].headers["Authorization"] == "Bearer s3cr3t"


@pytest.mark.parametrize(
    "server_url",
    ["http://api.example.test/v1", "https://api.example.test:8443/v1", "https://example.test/v1"],
)
async def test_scheme_port_and_host_all_count(wire, server_url):
    await _run({"method": "GET", "path": "/x", "server_url": server_url}, {}, token="s3cr3t")
    assert "authorization" not in wire.requests[0].headers


async def test_a_refusal_from_another_server_says_why(wire):
    wire._responses.append(httpx.Response(401, json={"error": "unauthorized"}))
    spec = {"method": "GET", "path": "/x", "server_url": "https://collector.example.org"}
    result = await _run(spec, {}, token="s3cr3t")
    assert result.startswith("Error: Demo refused the call (HTTP 401).")
    assert "runs on https://collector.example.org" in result
    assert "re-authorize" not in result




async def test_query_auth_wins_over_a_same_named_argument(wire):
    spec = {
        "method": "GET",
        "path": "/x",
        "query_mapping": {"api_key": {"from": "api_key"}, "q": {"from": "q"}},
        "auth": {"type": "query", "param": "api_key"},
    }
    await _run(spec, {"api_key": "from-the-model", "q": "z"}, token="real-key")
    assert _query(wire.requests[0]) == [("q", "z"), ("api_key", "real-key")]


async def test_auth_header_wins_over_a_mapped_header(wire):
    spec = {
        "method": "GET",
        "path": "/x",
        "header_mapping": {"x-api-key": {"from": "k"}},
        "auth": {"type": "header", "header": "X-API-Key", "prefix": ""},
    }
    await _run(spec, {"k": "from-the-model"}, token="real-key")
    assert wire.requests[0].headers.get_list("x-api-key") == ["real-key"]


@pytest.mark.parametrize(("token", "userpass"), [("alice:pw:d", "alice:pw:d"), ("sk_live", "sk_live:")])
async def test_basic_auth(wire, token, userpass):
    await _run({"method": "GET", "path": "/x", "auth": {"type": "basic"}}, {}, token=token)
    expected = "Basic " + base64.b64encode(userpass.encode()).decode()
    assert wire.requests[0].headers["Authorization"] == expected


async def test_no_auth_marker_sends_no_credential(wire):
    await _run({"method": "GET", "path": "/x", "auth": {"type": "basic"}}, {}, token="__no_auth__")
    assert "Authorization" not in wire.requests[0].headers


async def test_token_provider_supplies_the_credential(wire):
    async def provider() -> str:
        return "fresh-token"

    await _run({"method": "GET", "path": "/x"}, {}, token="__client_credentials__", token_provider=provider)
    assert wire.requests[0].headers["Authorization"] == "Bearer fresh-token"


async def test_token_provider_failure_is_reported_without_a_request(wire):
    async def provider() -> str:
        raise RuntimeError("the token endpoint answered HTTP 401 (invalid_client)")

    result = await _run({"method": "GET", "path": "/x"}, {}, token_provider=provider)
    assert "could not obtain an access token" in result
    assert "invalid_client" in result
    assert wire.requests == []


async def test_bearer_401_is_retried_once_with_a_refreshed_token(monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization", ""))
        if request.headers.get("Authorization") == "Bearer stale":
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(200, json={"ok": "yes"})

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)

    async def refresh() -> str:
        return "renewed"

    result = await _run({"method": "GET", "path": "/x"}, {}, token="stale", refresh_fn=refresh)
    assert json.loads(result) == {"ok": "yes"}
    assert seen == ["Bearer stale", "Bearer renewed"]


# ── Bodies ───────────────────────────────────────────────────────────────────


async def test_whole_body_argument(wire):
    spec = {"method": "POST", "path": "/bulk", "body_from": "body", "body_required": True}
    await _run(spec, {"body": ["a", "b"]})
    request = wire.requests[0]
    assert json.loads(request.content) == ["a", "b"]
    assert request.headers["Content-Type"] == "application/json"


async def test_missing_required_whole_body(wire):
    spec = {"method": "POST", "path": "/bulk", "body_from": "body", "body_required": True}
    result = await _run(spec, {})
    assert result == "Error: Required request body 'body' is missing."
    assert wire.requests == []


async def test_form_body(wire):
    spec = {
        "method": "POST",
        "path": "/pets/{id}",
        "path_params": ["id"],
        "body_encoding": "form",
        "content_type": "application/x-www-form-urlencoded",
        "body_mapping": {"name": {"from": "name"}, "tags": {"from": "tags"}},
    }
    await _run(spec, {"id": 7, "name": "Rex", "tags": ["a", "b"]})
    request = wire.requests[0]
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert request.content.decode() == "name=Rex&tags=a&tags=b"


async def test_multipart_text_fields(wire):
    spec = {
        "method": "POST",
        "path": "/upload",
        "body_encoding": "multipart",
        "body_mapping": {"note": {"from": "note"}},
    }
    await _run(spec, {"note": "hello"})
    request = wire.requests[0]
    assert request.headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert b'name="note"' in request.content and b"hello" in request.content


# ── File arguments ───────────────────────────────────────────────────────────


def _files(*refs: Any) -> Any:
    from app.tools.base import FileContext

    return FileContext.from_file_refs(list(refs))


def _ref(data: bytes, filename: str, content_type: str, file_id: str) -> Any:
    from app.llm.base import FileRef

    return FileRef.from_bytes(data, filename, content_type, file_id=file_id)


_CSV_UPLOAD = {
    "method": "POST",
    "path": "/search/csv",
    "body_encoding": "multipart",
    "body_mapping": {
        "data": {"from": "data", "file": True, "required": True, "content_type": "text/csv"},
        "columns": {"from": "columns"},
    },
}


async def test_multipart_file_from_a_conversation_file(wire):
    files = _files(_ref(b"adresse\n8 bd du port\n", "adresses.csv", "text/csv", "file-1"))
    result = await _run(_CSV_UPLOAD, {"data": {"file_id": "file-1"}, "columns": ["adresse"]}, file_context=files)
    assert "Error" not in result
    body = wire.requests[0].content
    assert b'name="data"; filename="adresses.csv"' in body
    assert b"Content-Type: text/csv" in body and b"8 bd du port" in body
    assert body.count(b'name="columns"') == 1


async def test_multipart_file_from_text_content(wire):
    await _run(_CSV_UPLOAD, {"data": {"content": "q\nParis\n", "filename": "villes.csv"}})
    body = wire.requests[0].content
    assert b'filename="villes.csv"' in body and b"Paris" in body
    assert b"Content-Type: text/csv" in body


async def test_a_bare_string_is_a_file_id_when_one_matches(wire):
    files = _files(_ref(b"\x89PNG", "logo.png", "image/png", "img-7"))
    spec = {"method": "PUT", "path": "/logo", "body_encoding": "binary", "body_from": "file"}
    await _run(spec, {"file": "img-7"}, file_context=files)
    assert wire.requests[0].content == b"\x89PNG"
    assert wire.requests[0].headers["Content-Type"] == "image/png"
    await _run(spec, {"file": "plain text"}, file_context=files)
    assert wire.requests[1].content == b"plain text"


async def test_binary_body_uses_the_declared_media_type(wire):
    spec = {
        "method": "PUT",
        "path": "/projects/{id}/input-file",
        "path_params": ["id"],
        "body_encoding": "binary",
        "body_from": "file",
        "content_type": "application/octet-stream",
    }
    files = _files(_ref(b"a,b\n1,2\n", "in.csv", "text/csv", "f"))
    await _run(spec, {"id": "p1", "file": {"file_id": "f"}}, file_context=files)
    request = wire.requests[0]
    assert request.content == b"a,b\n1,2\n"
    assert request.headers["Content-Type"] == "application/octet-stream"


async def test_several_files_in_one_multipart_field(wire):
    spec = {
        "method": "POST",
        "path": "/attachments",
        "body_encoding": "multipart",
        "body_mapping": {"files": {"from": "files", "file": True}},
    }
    await _run(spec, {"files": [{"content": "one", "filename": "1.txt"}, {"content": "two", "filename": "2.txt"}]})
    body = wire.requests[0].content
    assert body.count(b'name="files"') == 2
    assert b'filename="1.txt"' in body and b'filename="2.txt"' in body


@pytest.mark.parametrize(
    ("args", "file_context", "fragment"),
    [
        ({"data": {"file_id": "nope"}}, None, "No files are available here"),
        ({"data": {"file_id": "nope"}}, "empty", "is not available in this conversation"),
        ({"data": {}}, None, "needs file_id"),
        ({"data": 42}, None, "must be a file"),
        ({}, None, "Required field 'data' is missing"),
    ],
)
async def test_file_argument_errors(wire, args, file_context, fragment):
    result = await _run(_CSV_UPLOAD, args, file_context=_files() if file_context == "empty" else None)
    assert result.startswith("Error:") and fragment in result
    assert wire.requests == []


async def test_uploads_are_capped(wire, monkeypatch):
    from app.services import declarative_http_executor as executor_mod

    monkeypatch.setattr(executor_mod, "MAX_UPLOAD_BYTES", 8)
    result = await _run(_CSV_UPLOAD, {"data": {"content": "far too long for the cap"}})
    assert result.startswith("Error:") and "MB" in result
    assert wire.requests == []


async def test_filenames_cannot_break_the_part_headers(wire):
    await _run(_CSV_UPLOAD, {"data": {"content": "x", "filename": '../../evil"\r\nX-Injected: 1.csv'}})
    body = wire.requests[0].content
    assert b'filename="evilX-Injected: 1.csv"' in body
    assert b"\r\nX-Injected" not in body and b"../" not in body


def test_file_specs_are_validated():
    assert validate_execution_spec(_CSV_UPLOAD, tool_index=0) == []
    form = {**_CSV_UPLOAD, "body_encoding": "form"}
    assert any("need body_encoding 'multipart'" in e for e in validate_execution_spec(form, tool_index=0))
    binary = {"method": "POST", "path": "/x", "body_encoding": "binary"}
    assert any("set body_from" in e for e in validate_execution_spec(binary, tool_index=0))


async def test_the_tool_runtime_injects_the_conversation_files(wire):
    from app.services.declarative_http_executor import make_declarative_handler
    from app.tools.base import CallableToolExecutor, ToolCall

    executor = CallableToolExecutor()
    executor.register(
        "geo_search_csv",
        make_declarative_handler(
            spec=_CSV_UPLOAD,
            tool_name="geo_search_csv",
            token="tk",
            extra_config={"base_url": "https://api.example.test/v1"},
            provider_label="Demo",
        ),
    )
    executor.file_context = _files(_ref(b"q\nLyon\n", "villes.csv", "text/csv", "file-9"))
    result = await executor.execute_tool(
        ToolCall(id="1", name="geo_search_csv", arguments={"data": {"file_id": "file-9"}})
    )
    assert not result.is_error, result.content
    assert b"Lyon" in wire.requests[0].content


async def test_text_body(wire):
    spec = {"method": "POST", "path": "/csv", "body_from": "body", "body_encoding": "text", "content_type": "text/csv"}
    await _run(spec, {"body": "a,b\n1,2"})
    request = wire.requests[0]
    assert request.content == b"a,b\n1,2"
    assert request.headers["Content-Type"] == "text/csv"


async def test_non_json_response_is_returned_as_text(monkeypatch):
    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(200, text="<pet><name>Rex</name></pet>")
        )
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    assert await _run({"method": "GET", "path": "/x"}, {}) == "<pet><name>Rex</name></pet>"


# ── Destination guard ────────────────────────────────────────────────────────


def _install_dns(monkeypatch, mapping: dict[str, str]) -> None:
    def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        ip = mapping.get(host, host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(web_fetch.socket, "getaddrinfo", fake_getaddrinfo)


@pytest.mark.parametrize(
    ("ip", "allowed"),
    [
        ("93.184.216.34", True),
        ("10.1.2.3", True),
        ("192.168.1.20", True),
        ("100.64.3.4", True),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("100.100.100.200", False),
        ("0.0.0.0", False),  # noqa: S104 - an address under test, nothing binds to it
    ],
)
async def test_destination_guard(wire, monkeypatch, ip, allowed):
    _install_dns(monkeypatch, {"api.example.test": ip})
    result = await _run({"method": "GET", "path": "/x"}, {}, enforce_destination_guard=True)
    if allowed:
        assert json.loads(result) == {"ok": True}
        assert len(wire.requests) == 1
    else:
        assert "request refused" in result
        assert wire.requests == []


# ── Validation ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("spec", "fragment"),
    [
        ({"method": "POST", "path": "/x", "body_from": "b", "body_mapping": {}}, "mutually exclusive"),
        ({"method": "GET", "path": "/x", "body_from": "b"}, "not supported for GET"),
        ({"method": "GET", "path": "/x", "header_mapping": {"Authorization": {"from": "a"}}}, "reserved header"),
        ({"method": "GET", "path": "/x", "header_mapping": {"Bad Header": {"from": "a"}}}, "token characters"),
        ({"method": "GET", "path": "/x", "cookie_mapping": {"a;b": {"from": "a"}}}, "token characters"),
        ({"method": "GET", "path": "/x", "query_mapping": {"a": {"from": "a", "style": "matrix"}}}, "style must be one of"),
        ({"method": "GET", "path": "/x", "server_url": "ftp://files.test"}, "server_url"),
        ({"method": "GET", "path": "/x", "auth": {"type": "query"}}, "auth.param"),
        ({"method": "POST", "path": "/x", "body_from": "b", "body_encoding": "form", "content_type": "application/json"}, "x-www-form-urlencoded"),
        ({"method": "POST", "path": "/x", "body_from": "b", "body_encoding": "xml"}, "body_encoding"),
        ({"method": "GET", "path": "/x/{id}", "path_params": ["id"], "path_mapping": {"other": {"from": "o"}}}, "path_mapping keys"),
        ({"method": "GET", "path": "/x", "server_url": "https://[::1/v1"}, "server_url"),
        ({"method": "GET", "path": "/x", "auth": {"type": "header", "header": "Content-Length"}}, "reserved header"),
        ({"method": "GET", "path": "/x", "auth": {"type": "header", "header": "Host"}}, "reserved header"),
        ({"method": "GET", "path": "/x", "auth": {"type": "header", "header": "Proxy-Authorization"}}, "reserved header"),
    ],
)
def test_validation_rejects(spec, fragment):
    errors = validate_execution_spec(spec, tool_index=0)
    assert any(fragment in e for e in errors), errors


def test_validation_accepts_the_new_fields():
    spec = {
        "method": "POST",
        "path": "/items/{id}",
        "path_params": ["id"],
        "path_mapping": {"id": {"from": "item_id", "required": True}},
        "query_mapping": {"f": {"from": "f", "style": "deepObject", "explode": True}},
        "header_mapping": {"X-Tenant": {"from": "tenant", "required": True}},
        "cookie_mapping": {"locale": {"from": "locale"}},
        "body_from": "body",
        "body_required": True,
        "body_encoding": "json",
        "content_type": "application/merge-patch+json",
        "accept": "application/json",
        "server_url": "https://uploads.example.test/v2",
        "auth": {"type": "basic"},
    }
    assert validate_execution_spec(spec, tool_index=0) == []

# -- Review fixes: query string, credential header, base URL, time budget --


async def test_a_querystring_survives_the_query_credential(wire):
    """httpx drops a URL's query when params are given: both must still go out."""
    spec = {
        "method": "GET",
        "path": "/search",
        "querystring": {"from": "filter", "media_type": "application/json"},
        "auth": {"type": "query", "param": "api_key"},
    }
    await _run(spec, {"filter": {"status": "open"}})
    request = wire.requests[0]
    assert request.url.query.startswith(b"%7B%22status%22%3A%22open%22%7D")
    assert request.url.params["api_key"] == "tk"


async def test_a_querystring_alone_is_sent(wire):
    spec = {"method": "GET", "path": "/search", "querystring": {"from": "filter", "media_type": "application/json"}}
    await _run(spec, {"filter": {"status": "open"}})
    assert wire.requests[0].url.query == b"%7B%22status%22%3A%22open%22%7D"


@pytest.mark.parametrize("header", ["Content-Length", "Host", "Transfer-Encoding", "Content-Type"])
async def test_the_credential_never_rides_in_a_transport_header(wire, header):
    spec = {"method": "GET", "path": "/x", "auth": {"type": "header", "header": header, "prefix": ""}}
    result = await _run(spec, {})
    assert result.startswith("Error:") and header in result
    assert wire.requests == []


async def test_a_cookie_credential_is_kept_beside_mapped_cookies(wire):
    """A bundle that authenticates with the Cookie header keeps working."""
    spec = {
        "method": "GET",
        "path": "/x",
        "cookie_mapping": {"locale": {"from": "locale"}},
        "auth": {"type": "header", "header": "Cookie", "prefix": ""},
    }
    await _run(spec, {"locale": "fr"}, token="session=abc")
    assert wire.requests[0].headers["Cookie"] == "session=abc; locale=fr"


async def test_a_mapped_cookie_never_shadows_the_cookie_credential(wire):
    """A server reading the last duplicate would take the caller's session."""
    spec = {
        "method": "GET",
        "path": "/x",
        "cookie_mapping": {"session": {"from": "session"}, "locale": {"from": "locale"}},
        "auth": {"type": "header", "header": "Cookie", "prefix": ""},
    }
    await _run(spec, {"session": "attacker", "locale": "fr"}, token="session=abc; theme=dark")
    assert wire.requests[0].headers["Cookie"] == "session=abc; theme=dark; locale=fr"

    await _run(spec, {"session": "attacker"}, token="session=abc")
    assert wire.requests[1].headers["Cookie"] == "session=abc"


async def test_a_bundle_server_url_without_a_base_url_carries_the_credential(wire):
    spec = {"method": "GET", "path": "/x", "server_url": "https://uploads.example.test/v2"}
    await execute_declarative_tool(
        spec, {}, token="tk", extra_config={}, provider_name="Demo", tool_name="demo"
    )
    assert wire.requests[0].headers["Authorization"] == "Bearer tk"


async def test_a_user_supplied_server_url_without_a_base_url_is_refused(wire, monkeypatch):
    monkeypatch.setattr(web_fetch, "_environment_proxies", dict)
    spec = {"method": "GET", "path": "/x", "server_url": "https://uploads.example.test/v2"}
    result = await execute_declarative_tool(
        spec,
        {},
        token="tk",
        extra_config={},
        provider_name="Demo",
        tool_name="demo",
        enforce_destination_guard=True,
    )
    assert result.startswith("Error:") and "base_url" in result
    assert wire.requests == []


async def test_timeout_bounds_the_retries(monkeypatch):
    """A 503 asking for 30 s is answered, not waited on, by a 5 s tool."""
    recorder = _Recorder([httpx.Response(503, headers={"Retry-After": "30"})] * 4)

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(recorder.handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    result = await asyncio.wait_for(_run({"method": "GET", "path": "/x", "timeout_ms": 5000}, {}), 5)
    assert result.startswith("Error:") and "503" in result
    assert len(recorder.requests) == 1


async def test_a_response_path_index_in_other_digits_is_not_an_index(wire):
    recorder_spec = {"method": "GET", "path": "/x", "response_path": "items.\u00b2"}
    result = await _run(recorder_spec, {})
    assert not result.startswith("Error:")
