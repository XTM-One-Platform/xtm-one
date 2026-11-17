"""The default path of the declarative executor reads its answer bounded.

A tool that does not opt into ``untrusted_response`` keeps its retries, its
401/403 refresh, the destination guard and the client certificate, and gets
the bounded read: ``Accept-Encoding: identity``, a compressed answer refused,
raw bytes counted up to ``max_response_bytes`` (by default the cap).

Every case runs a real ``httpx.AsyncClient`` over ``httpx.MockTransport``
with streamed bodies, so the response hook and the body read are the ones
production runs.
"""

from __future__ import annotations

import gzip
import json
import ssl
from collections.abc import AsyncIterator, Callable

import httpx
import pytest

import app.integrations.http as http_module
import app.services.declarative_http_executor as executor_module
from app.services.declarative_http_executor import execute_declarative_tool

_ACTION = "execute tool 'list_items'"
_SPEC = {"method": "GET", "path": "/items", "auth": {"type": "none"}}
_CAP = 20 * 1024 * 1024


class _CountingStream(httpx.AsyncByteStream):
    """*chunks* served one at a time, counting how many were pulled."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.pulled = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            self.pulled += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _streamed(status: int, body: bytes, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))


class _Server:
    """Answers each request with the next response, recording what was sent."""

    def __init__(self, *responses: httpx.Response) -> None:
        self._responses = list(responses)
        self.sent: list[httpx.Request] = []
        self.client_kwargs: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        return self._responses.pop(0)

    def factory(self) -> Callable[..., httpx.AsyncClient]:
        real_client = httpx.AsyncClient

        def build(*args, **kwargs):
            self.client_kwargs.append(dict(kwargs))
            # The guarded transport is recorded, then replaced by the mock.
            kwargs.pop("transport", None)
            kwargs.pop("mounts", None)
            return real_client(*args, transport=httpx.MockTransport(self.handler), **kwargs)

        return build


@pytest.fixture
def serve(monkeypatch):
    def install(*responses: httpx.Response) -> _Server:
        server = _Server(*responses)
        monkeypatch.setattr(executor_module, "egress_client", server.factory())
        # ``AutoRefreshClient`` builds its client through its own import.
        monkeypatch.setattr(http_module, "egress_client", server.factory())
        return server

    return install


async def _call(spec: dict | None = None, kwargs: dict | None = None, **options) -> str:
    return await execute_declarative_tool(
        spec or _SPEC,
        kwargs or {},
        token=options.pop("token", "__no_auth__"),
        extra_config={"base_url": options.pop("base_url", "https://api.example.com")},
        provider_name="Example",
        tool_name="list_items",
        **options,
    )


def _too_large(kb: int) -> str:
    return f"Error: the Example answer to {_ACTION} is over the {kb} KB limit; it is not read."


_COMPRESSED = f"Error: Example answered {_ACTION} compressed; the answer is not read."


# ── What the default path keeps ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_json_answer_keeps_its_exact_shape_and_is_asked_for_uncompressed(serve):
    body = {"data": {"items": [{"name": "é", "n": 1}]}}
    server = serve(_streamed(200, json.dumps(body).encode(), {"content-type": "application/json"}))

    result = await _call({**_SPEC, "response_path": "data.items"})

    assert result == json.dumps(body["data"]["items"], ensure_ascii=False)
    assert server.sent[0].headers.get_list("accept-encoding") == ["identity"]
    assert "untrusted" not in result.lower()


@pytest.mark.asyncio
async def test_a_text_answer_is_returned_as_text(serve):
    serve(_streamed(200, b"plain answer", {"content-type": "text/plain"}))
    assert await _call() == "plain answer"


@pytest.mark.parametrize("name", ["Accept-Encoding", "accept-encoding", "ACCEPT-ENCODING"])
@pytest.mark.asyncio
async def test_a_mapped_accept_encoding_is_replaced_by_identity(serve, name):
    server = serve(_streamed(200, b"{}"))
    spec = {**_SPEC, "header_mapping": {name: {"from": "encoding"}}}

    assert await _call(spec, {"encoding": "gzip, br"}) == "{}"

    assert server.sent[0].headers.get_list("accept-encoding") == ["identity"]


@pytest.mark.asyncio
async def test_an_answer_of_exactly_its_budget_is_read(serve):
    serve(_streamed(200, b'"' + b"a" * 62 + b'"'))
    assert await _call({**_SPEC, "max_response_bytes": 64}) == json.dumps("a" * 62)


@pytest.mark.asyncio
async def test_an_idempotent_call_is_still_retried_and_each_attempt_bounded(serve, monkeypatch):
    monkeypatch.setattr(http_module, "_RATE_LIMIT_BASE_DELAY", 0.0)
    server = serve(_streamed(503, b"busy"), _streamed(200, b'{"ok": true}'))

    assert json.loads(await _call()) == {"ok": True}

    assert [request.headers.get_list("accept-encoding") for request in server.sent] == [
        ["identity"],
        ["identity"],
    ]


@pytest.mark.asyncio
async def test_a_retried_answer_over_its_budget_is_refused(serve, monkeypatch):
    monkeypatch.setattr(http_module, "_RATE_LIMIT_BASE_DELAY", 0.0)
    server = serve(_streamed(503, b"busy"), _streamed(200, b"x" * 1025))

    assert await _call({**_SPEC, "max_response_bytes": 1024}) == _too_large(1)
    assert len(server.sent) == 2


@pytest.mark.asyncio
async def test_a_401_still_refreshes_the_token_once(serve):
    server = serve(_streamed(401, b"expired"), _streamed(200, b'{"ok": true}'))
    refreshed: list[int] = []

    async def refresh() -> str:
        refreshed.append(1)
        return "fresh-token"

    result = await _call({**_SPEC, "auth": {"type": "bearer"}}, token="stale-token", refresh_fn=refresh)

    assert json.loads(result) == {"ok": True}
    assert refreshed == [1]
    assert [request.headers["authorization"] for request in server.sent] == [
        "Bearer stale-token",
        "Bearer fresh-token",
    ]
    assert server.sent[1].headers.get_list("accept-encoding") == ["identity"]


@pytest.mark.asyncio
async def test_the_answer_after_a_refresh_is_bounded_too(serve):
    serve(_streamed(401, b"expired"), _streamed(200, b"x" * 2048))

    async def refresh() -> str:
        return "fresh-token"

    result = await _call(
        {**_SPEC, "auth": {"type": "bearer"}, "max_response_bytes": 1024},
        token="stale-token",
        refresh_fn=refresh,
    )

    assert result == _too_large(1)


@pytest.mark.asyncio
async def test_an_http_error_keeps_its_detail(serve):
    serve(_streamed(404, json.dumps({"message": "Pet 7 not found"}).encode(), {"content-type": "application/json"}))

    assert await _call() == f"Error: Example resource not found while trying to {_ACTION}. Detail: Pet 7 not found"


@pytest.mark.asyncio
async def test_a_head_answer_declaring_a_large_body_still_gives_its_summary(serve):
    serve(_streamed(200, b"", {"content-length": str(_CAP * 10), "x-total": "3"}))

    result = await _call({**_SPEC, "method": "HEAD"})

    assert json.loads(result) == {
        "status": 200,
        "headers": {"content-length": str(_CAP * 10), "x-total": "3"},
    }


@pytest.mark.asyncio
async def test_an_empty_compressed_options_answer_still_gives_its_summary(serve):
    serve(_streamed(204, b"", {"content-encoding": "gzip", "allow": "GET"}))

    result = await _call({**_SPEC, "method": "OPTIONS"})

    assert json.loads(result) == {"status": 204, "headers": {"content-encoding": "gzip", "allow": "GET"}}


@pytest.mark.asyncio
async def test_a_guarded_destination_keeps_its_pinned_transport_and_is_bounded(serve):
    server = serve(_streamed(200, b"x" * 2048))

    result = await _call(
        {**_SPEC, "max_response_bytes": 1024},
        base_url="https://93.184.216.34",
        enforce_destination_guard=True,
    )

    assert result == _too_large(1)
    (options,) = server.client_kwargs
    assert isinstance(options["transport"], httpx.AsyncBaseTransport)
    assert len(options["event_hooks"]["response"]) == 1


@pytest.mark.asyncio
async def test_a_client_certificate_is_still_presented(serve):
    server = serve(_streamed(200, b"{}"))
    context = ssl.create_default_context()

    assert await _call(ssl_context=context) == "{}"

    assert server.client_kwargs[0]["verify"] is context


# ── What the default path refuses ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_streamed_answer_stops_at_the_first_chunk_past_its_budget(serve):
    stream = _CountingStream([b"x" * 512] * 10)
    serve(httpx.Response(200, stream=stream))

    assert await _call({**_SPEC, "max_response_bytes": 1024}) == _too_large(1)

    assert stream.pulled == 3
    assert stream.closed is True


@pytest.mark.asyncio
async def test_a_declared_length_over_the_budget_is_refused_unread(serve):
    stream = _CountingStream([b"{}"])
    serve(httpx.Response(200, headers={"content-length": "4096"}, stream=stream))

    assert await _call({**_SPEC, "max_response_bytes": 1024}) == _too_large(1)

    assert stream.pulled == 0
    assert stream.closed is True


@pytest.mark.asyncio
async def test_without_a_budget_the_cap_applies(serve):
    chunk = b"x" * (1024 * 1024)
    stream = _CountingStream([chunk] * 21)
    serve(httpx.Response(200, stream=stream))

    assert await _call() == _too_large(_CAP // 1024)

    assert stream.pulled == 21


@pytest.mark.asyncio
async def test_an_error_answer_over_the_budget_is_refused(serve):
    serve(_streamed(500, b"x" * 2048))
    assert await _call({**_SPEC, "max_response_bytes": 1024}) == _too_large(1)


@pytest.mark.parametrize(
    ("encoding", "body"),
    [
        ("gzip", gzip.compress(b"0" * 100_000)),
        ("br", b"\x1b\xff\xff"),
        ("deflate, gzip", b"\x78\x9c"),
        ("GZIP", gzip.compress(b"{}")),
    ],
    ids=["gzip-bomb", "br", "stacked", "upper-case"],
)
@pytest.mark.asyncio
async def test_a_compressed_answer_is_refused_unread(serve, caplog, encoding, body):
    stream = _CountingStream([body, b"more"])
    serve(httpx.Response(200, headers={"content-encoding": encoding}, stream=stream))

    with caplog.at_level("WARNING", logger=executor_module.__name__):
        assert await _call() == _COMPRESSED

    assert stream.pulled == 1
    assert stream.closed is True
    assert [record.getMessage() for record in caplog.records if record.name == executor_module.__name__] == [
        f"Example: {_ACTION} answered compressed ({encoding.strip().lower()!r})"
    ]
