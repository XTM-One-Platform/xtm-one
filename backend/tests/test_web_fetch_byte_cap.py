"""`fetch_and_extract(max_bytes=...)`: a hostile server's response size never
decides how much a fetch holds (the Deep Investigation site fingerprint).

The default path is untouched and covered by its own suites; what is pinned
here is the opt-in one: the body is streamed uncompressed, reading stops at
the cap whatever the server keeps sending, and a redirect's body is not read.
"""

from __future__ import annotations

import socket

import httpx
import pytest

from app.services import web_fetch

CHUNK = 64 * 1024


@pytest.fixture
def served(monkeypatch):
    """Route requests to a handler; every name resolves to one public address.

    The fetch connects to the address it checked (`send_pinned`), so the
    policy runs as in production and the handler sees the name in `Host`.
    """

    def _getaddrinfo(host, *_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
    real = httpx.AsyncClient
    holder: dict = {}

    def _client(**kwargs):
        kwargs.pop("follow_redirects", None)
        kwargs.pop("transport", None)  # the mock stands for the network
        return real(transport=httpx.MockTransport(holder["handler"]), follow_redirects=False, **kwargs)

    monkeypatch.setattr(web_fetch.httpx, "AsyncClient", _client)

    def _install(handler):
        holder["handler"] = handler

    return _install


class _Endless(httpx.AsyncByteStream):
    """A body that never ends, counting what was pulled from it."""

    def __init__(self) -> None:
        self.pulled = 0

    async def __aiter__(self):
        while True:
            self.pulled += 1
            yield b"<p>" + b"x" * (CHUNK - 3)


@pytest.mark.asyncio
async def test_an_endless_body_is_cut_at_the_cap(served):
    body = _Endless()
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "text/html"}, stream=body)

    served(_handler)
    cap = 3 * CHUNK + 10
    result = await web_fetch.fetch_and_extract("https://site.example/", max_bytes=cap, use_trafilatura=False)
    assert len(result.body) == cap
    assert result.body_capped is True
    assert body.pulled == 4, "reading stops at the chunk that crosses the cap"
    assert seen[0].headers["accept-encoding"] == "identity"


@pytest.mark.asyncio
async def test_a_body_under_the_cap_is_read_whole(served):
    page = b"<html><head><title>T</title></head><body>hello</body></html>"
    served(lambda request: httpx.Response(200, headers={"content-type": "text/html"}, content=page))
    result = await web_fetch.fetch_and_extract("https://site.example/", max_bytes=len(page), use_trafilatura=False)
    assert result.body == page
    assert result.body_capped is False
    assert result.title == "T"


@pytest.mark.asyncio
async def test_a_redirect_body_is_never_read(served):
    redirect_body = _Endless()

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(301, headers={"location": "/home"}, stream=redirect_body)
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"landed")

    served(_handler)
    result = await web_fetch.fetch_and_extract("https://site.example/", max_bytes=1024)
    assert result.url == "https://site.example/home"
    assert result.body == b"landed"
    assert redirect_body.pulled == 0


@pytest.mark.asyncio
async def test_without_a_cap_the_body_is_read_as_before(served):
    served(lambda request: httpx.Response(200, headers={"content-type": "text/plain"}, content=b"x" * 5000))
    result = await web_fetch.fetch_and_extract("https://site.example/", max_chars=100)
    assert len(result.body) == 5000
    assert result.body_capped is False
    assert result.truncated is True
