"""How much of a channel file the router reads before it refuses it.

``_resolve_channel_files`` read every inbound attachment whole into memory
and only then compared its length with ``MAX_UPLOAD_SIZE_BYTES``: the limit
decided whether the file was stored, not how much was read. httpx's timeout is
per read, not a total, and a Teams attachment declares no size, so one large
file could make the web process buffer gigabytes to drop them.

Pinned here, through a real ``httpx.AsyncClient`` on a mock transport: a file
declared over the limit is never requested, a ``Content-Length`` over it is
never read, a body with no length stops being read as soon as it passes it, a
compressed body is inflated a bounded piece at a time (a compression bomb never
lands in memory), a coding the read does not inflate is refused, a file within
the limit is stored whole, redirects are still followed for Microsoft Graph
content endpoints and Google Chat's media endpoint only, and no line logs the URL.
"""

from __future__ import annotations

import gzip
import logging
import tracemalloc
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.channels import router
from app.channels.base import ChannelType, InboundFileAttachment, InboundMessage
from app.services import file_storage

LIMIT = 1000
TOKEN = "xoxb-slack-bot-token"
SLACK_URL = "https://files.slack.com/files-pri/T1-F1/download/payslip.pdf"
GRAPH_URL = "https://graph.microsoft.com/v1.0/shares/u!abc/driveItem/content"
CHAT_MEDIA_URL = "https://chat.googleapis.com/v1/media/spaces/AAA/messages/1/attachments/2?alt=media"
_RealAsyncClient = httpx.AsyncClient


class _Chunks(httpx.AsyncByteStream):
    """A response body served chunk by chunk, counting what was read."""

    def __init__(self, chunk: bytes, count: int):
        self.chunk, self.count, self.served = chunk, count, 0

    async def __aiter__(self):
        for _ in range(self.count):
            self.served += 1
            yield self.chunk

    async def aclose(self) -> None:
        pass


def _streamed(status: int, body: bytes, headers: dict[str, str] | None = None) -> httpx.Response:
    """A response whose body is streamed, as the network's is (one built from ``content=`` is already read)."""
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))


def _gzip_bomb(decoded_bytes: int) -> bytes:
    """A gzip body of *decoded_bytes* zeros, compressed without ever holding them."""
    deflater = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    block = b"\0" * 1_000_000
    parts = [deflater.compress(block) for _ in range(decoded_bytes // len(block))]
    return b"".join([*parts, deflater.flush()])


@pytest.fixture
def platform(monkeypatch):
    """A platform behind ``httpx.MockTransport``: ``respond`` answers, requests and client options are recorded."""
    state = SimpleNamespace(requests=[], clients=[], stored=[], rows=[], respond=None)

    def handle(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        return state.respond(request)

    transport = httpx.MockTransport(handle)

    class _Client(_RealAsyncClient):
        def __init__(self, *args, **kwargs):
            state.clients.append(kwargs)
            super().__init__(*args, transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr(file_storage, "MAX_UPLOAD_SIZE_BYTES", LIMIT)
    monkeypatch.setattr(
        file_storage.file_storage, "_client", SimpleNamespace(put_object=lambda *args: state.stored.append(args))
    )
    state.db = SimpleNamespace(add=state.rows.append, flush=AsyncMock())
    return state


def _inbound(*attachments: InboundFileAttachment) -> InboundMessage:
    return InboundMessage(channel=ChannelType.SLACK, text="See the attached file", file_attachments=list(attachments))


def _attachment(url: str = SLACK_URL, *, size: int = 0, content_type: str = "application/pdf") -> InboundFileAttachment:
    return InboundFileAttachment(
        url=url,
        filename="payslip.pdf",
        content_type=content_type,
        size=size,
        auth_headers={"Authorization": f"Bearer {TOKEN}"},
    )


def _too_large_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("Channel file too large")]


def _stored_bytes(platform) -> bytes:
    [(_bucket, _key, stream, length, _type)] = platform.stored
    data = stream.getvalue()
    assert len(data) == length
    return data


@pytest.mark.asyncio
async def test_a_file_declared_over_the_limit_is_never_requested(platform, caplog):
    caplog.set_level(logging.DEBUG, logger="app")

    refs = await router._resolve_channel_files(platform.db, _inbound(_attachment(size=LIMIT + 1)), None)

    assert refs == [] and platform.requests == []
    assert _too_large_lines(caplog) == [f"Channel file too large ({LIMIT + 1} bytes > {LIMIT}): application/pdf"]


@pytest.mark.asyncio
async def test_a_content_length_over_the_limit_is_not_read(platform, caplog):
    caplog.set_level(logging.DEBUG, logger="app")
    body = _Chunks(b"x" * 256, 40)
    platform.respond = lambda _request: httpx.Response(200, headers={"content-length": str(256 * 40)}, stream=body)

    refs = await router._resolve_channel_files(platform.db, _inbound(_attachment()), None)

    assert refs == [] and len(platform.requests) == 1
    assert body.served == 0, "the body is not read"
    assert _too_large_lines(caplog) == [f"Channel file too large ({256 * 40} bytes > {LIMIT}): application/pdf"]


@pytest.mark.asyncio
async def test_a_body_without_a_length_stops_being_read_once_past_the_limit(platform, caplog):
    """A chunked body declares nothing: the read stops at the first chunk past the limit, not at the end."""
    caplog.set_level(logging.DEBUG, logger="app")
    body = _Chunks(b"x" * 256, 100_000)
    platform.respond = lambda _request: httpx.Response(200, stream=body)

    refs = await router._resolve_channel_files(platform.db, _inbound(_attachment()), None)

    assert refs == [] and platform.stored == [] and platform.rows == []
    assert body.served == 4, "256 x 4 is the first count past 1000"
    assert _too_large_lines(caplog) == [f"Channel file too large (over {LIMIT} bytes): application/pdf"]


@pytest.mark.asyncio
async def test_the_download_asks_for_the_file_as_it_is_stored(platform):
    platform.respond = lambda _request: _streamed(200, b"%PDF")

    await router._resolve_channel_files(platform.db, _inbound(_attachment()), None)

    [request] = platform.requests
    assert request.headers["accept-encoding"] == "identity"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"


@pytest.mark.asyncio
async def test_a_gzip_body_within_the_limit_is_stored_decoded(platform):
    """A server may still encode the body: gzip and deflate are inflated, as httpx did."""
    document = b"%PDF-1.7 " + b"quarterly figures " * 40
    for coding, encoded in (("gzip", gzip.compress(document)), ("deflate", zlib.compress(document))):
        platform.stored.clear()
        platform.respond = lambda _request, coding=coding, encoded=encoded: _streamed(
            200, encoded, {"content-encoding": coding}
        )

        [_ref] = await router._resolve_channel_files(platform.db, _inbound(_attachment()), None)

        assert _stored_bytes(platform) == document, coding


@pytest.mark.asyncio
async def test_a_bare_deflate_body_is_stored_decoded(platform):
    """HTTP's deflate is a zlib stream by the RFC and sometimes a bare one: httpx read both, so does the bounded read."""
    document = b"%PDF-1.7 " + b"quarterly figures " * 40
    deflater = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    bare = deflater.compress(document) + deflater.flush()
    platform.respond = lambda _request: _streamed(200, bare, {"content-encoding": "deflate"})

    [_ref] = await router._resolve_channel_files(platform.db, _inbound(_attachment()), None)

    assert _stored_bytes(platform) == document


class _EmptyDeflateBlocks(httpx.AsyncByteStream):
    """A zlib stream that never ends and never inflates to anything: empty stored blocks."""

    def __init__(self):
        self.served = 0

    async def __aiter__(self):
        # A zlib header, then non-final stored blocks of length 0 (BTYPE 00,
        # LEN 0000, NLEN FFFF), 5 bytes each: 1 KB of them per chunk.
        yield b"\x78\x9c"
        block = b"\x00\x00\x00\xff\xff" * 205
        while True:
            self.served += 1
            yield block

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_a_compressed_body_that_inflates_to_nothing_is_not_read_forever(platform, caplog):
    """The raw read is capped too: a legitimate compressed file is barely larger than the file."""
    caplog.set_level(logging.DEBUG, logger="app")
    body = _EmptyDeflateBlocks()
    platform.respond = lambda _request: httpx.Response(200, headers={"content-encoding": "deflate"}, stream=body)

    refs = await router._resolve_channel_files(platform.db, _inbound(_attachment()), None)

    assert refs == [] and platform.stored == []
    assert body.served <= 3, "stopped just past the raw cap (limit + 1% + 1 KB), 1 KB a block"
    assert _too_large_lines(caplog) == [f"Channel file too large (over {LIMIT} bytes): application/pdf"]


@pytest.mark.asyncio
async def test_a_compressed_body_is_counted_as_it_is_stored(platform, caplog):
    """A Content-Length counts the encoded body: a small gzip body can decode past the limit."""
    caplog.set_level(logging.DEBUG, logger="app")
    compressed = gzip.compress(b"\0" * (LIMIT * 50))
    assert len(compressed) < LIMIT
    platform.respond = lambda _request: _streamed(
        200, compressed, {"content-encoding": "gzip", "content-length": str(len(compressed))}
    )

    refs = await router._resolve_channel_files(platform.db, _inbound(_attachment()), None)

    assert refs == [] and platform.stored == []
    assert _too_large_lines(caplog) == [f"Channel file too large (over {LIMIT} bytes): application/pdf"]


@pytest.mark.asyncio
async def test_a_compression_bomb_is_never_inflated_whole(platform):
    """One 50 KB gzip chunk inflates to 50 MB: it is inflated a bounded piece at a time, never at once."""
    bomb = _gzip_bomb(50_000_000)
    assert len(bomb) < 100_000
    platform.respond = lambda _request: _streamed(200, bomb, {"content-encoding": "gzip"})

    tracemalloc.start()
    try:
        refs = await router._resolve_channel_files(platform.db, _inbound(_attachment()), None)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert refs == [] and platform.stored == []
    assert peak < 5_000_000, f"{peak} bytes held while refusing a 50 MB bomb"


@pytest.mark.asyncio
async def test_a_coding_the_read_does_not_inflate_is_refused(platform, caplog):
    caplog.set_level(logging.DEBUG, logger="app")
    platform.respond = lambda _request: _streamed(200, b"\x1b\x00\x00", {"content-encoding": "br"})

    refs = await router._resolve_channel_files(platform.db, _inbound(_attachment()), None)

    assert refs == [] and platform.stored == []
    failures = [r for r in caplog.records if r.getMessage().startswith("Failed to resolve channel file attachment")]
    assert len(failures) == 1
    assert "unsupported content encoding 'br'" in logging.Formatter().format(failures[0])


@pytest.mark.asyncio
async def test_a_file_within_the_limit_is_stored_whole(platform, caplog):
    caplog.set_level(logging.DEBUG, logger="app")
    body = _Chunks(b"%PDF", 3)
    platform.respond = lambda _request: httpx.Response(200, headers={"content-type": "application/pdf"}, stream=body)
    attachment = _attachment(content_type="application/octet-stream", size=12)

    [ref] = await router._resolve_channel_files(platform.db, _inbound(attachment), None)

    [row] = platform.rows
    assert (row.size, row.content_type) == (12, "application/pdf"), "the type the response declares, as before"
    assert _stored_bytes(platform) == b"%PDF" * 3
    assert ref.file_id == str(row.id)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert TOKEN not in logged and SLACK_URL not in logged and "payslip" not in logged


@pytest.mark.asyncio
async def test_only_a_graph_or_google_chat_media_url_follows_redirects(platform):
    """Graph and Chat media endpoints may answer 302 to the bytes; every other attachment URL keeps redirects off."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "graph.microsoft.com":
            return httpx.Response(302, headers={"location": "https://contoso.sharepoint.com/download/q3.pdf"})
        if request.url.host == "contoso.sharepoint.com":
            return _streamed(200, b"%PDF graph")
        if request.url.host == "chat.googleapis.com":
            return httpx.Response(302, headers={"location": "https://chat-media.googleusercontent.com/q3.pdf"})
        if request.url.host == "chat-media.googleusercontent.com":
            return _streamed(200, b"%PDF chat")
        return httpx.Response(302, headers={"location": "https://elsewhere.example/payslip.pdf"})

    platform.respond = respond

    refs = await router._resolve_channel_files(
        platform.db,
        _inbound(_attachment(GRAPH_URL), _attachment(SLACK_URL), _attachment(CHAT_MEDIA_URL)),
        None,
    )

    assert [client["follow_redirects"] for client in platform.clients] == [True, False, True]
    assert len(refs) == 2, "the Graph and Chat files are followed and stored; the other 302 is a failed download"
    assert [str(r.url.host) for r in platform.requests] == [
        "graph.microsoft.com",
        "contoso.sharepoint.com",
        "files.slack.com",
        "chat.googleapis.com",
        "chat-media.googleusercontent.com",
    ]
    for hop in (platform.requests[1], platform.requests[4]):
        assert "authorization" not in hop.headers, "httpx drops the bearer token on the cross-origin hop"
