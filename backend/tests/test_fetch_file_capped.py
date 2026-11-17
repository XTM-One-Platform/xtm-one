"""A download that may exceed its cap is streamed and stops at the cap.

``safe_request`` reads a whole body into memory before any cap can be
checked; ``file_delivery.fetch_file`` is what the tools use when the file can
be larger than the cap (a meeting recording, an investigation package) and
its size is not known before the body arrives. Its error also says why, so a
tool can keep a link past the cap, and ``AutoRefreshClient.stream`` gives it
the same single token refresh as every other request.
"""

from __future__ import annotations

import gzip
from collections.abc import AsyncIterator

import httpx
import pytest

from app.integrations.file_delivery import FetchedFile, FetchError, fetch_file
from app.integrations.http import AutoRefreshClient

URL = "https://files.example.test/package.zip"


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _fetch(client, max_bytes: int):
    return await fetch_file(client, "get", URL, provider="Test", action="download the package", max_bytes=max_bytes)


@pytest.mark.asyncio
async def test_a_declared_length_over_the_cap_is_refused_with_its_size():
    async with _client(lambda _r: httpx.Response(200, content=b"x" * 50)) as client:
        result = await _fetch(client, max_bytes=10)

    assert isinstance(result, FetchError) and result.too_large and result.size == 50
    assert result.startswith("Error: the Test file is over")


@pytest.mark.asyncio
async def test_a_body_of_unknown_length_is_dropped_as_soon_as_it_passes_the_cap():
    pulled: list[int] = []

    async def body():
        for index in range(100):
            pulled.append(index)
            yield b"x" * 10

    async with _client(lambda _r: httpx.Response(200, content=body())) as client:
        result = await _fetch(client, max_bytes=25)

    assert isinstance(result, FetchError) and result.too_large and result.size is None
    assert len(pulled) < 10


@pytest.mark.asyncio
async def test_a_refused_answer_carries_its_status():
    async with _client(lambda _r: httpx.Response(403, json={"error": {"message": "no"}})) as client:
        result = await _fetch(client, max_bytes=10)

    assert isinstance(result, FetchError) and result.status == 403 and not result.too_large
    assert result.startswith("Error: Test access denied")


@pytest.mark.asyncio
async def test_the_refreshing_client_streams_with_one_token_refresh(monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        if request.headers["authorization"] == "Bearer old":
            return httpx.Response(401)
        return httpx.Response(200, content=b"video", headers={"Content-Type": "video/mp4"})

    real_client = httpx.AsyncClient

    class _Client(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    async def refresh() -> str:
        return "new"

    async with AutoRefreshClient({"Authorization": "Bearer old"}, refresh) as client:
        result = await _fetch(client, max_bytes=100)

    assert result == FetchedFile(data=b"video", content_type="video/mp4", filename="")
    assert seen == ["Bearer old", "Bearer new"]


class RecordingStream(httpx.AsyncByteStream):
    """A response body that counts the chunks pulled and records its close."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.pulled = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.pulled += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, headers, chunks, pulled",
    [
        (200, {"Content-Length": "50"}, [b"x" * 50], 0),
        (200, {}, [b"x" * 10] * 100, 3),
        (200, {}, [b"ok"], 1),
    ],
    ids=["declared over the cap", "streamed over the cap", "within the cap"],
)
async def test_every_answer_is_closed_before_fetch_file_returns(status, headers, chunks, pulled):
    stream = RecordingStream(chunks)
    async with _client(lambda _r: httpx.Response(status, headers=headers, stream=stream)) as client:
        await _fetch(client, max_bytes=25)

    assert stream.closed and stream.pulled == pulled


@pytest.mark.asyncio
async def test_an_error_answer_is_read_up_to_a_bounded_size_and_closed():
    body = b'{"error": {"message": "gone"}}' + b" " * (200 * 1024)
    stream = RecordingStream([body[i : i + 16 * 1024] for i in range(0, len(body), 16 * 1024)])
    async with _client(
        lambda _r: httpx.Response(404, headers={"Content-Type": "application/json"}, stream=stream)
    ) as client:
        result = await _fetch(client, max_bytes=10)

    assert isinstance(result, FetchError) and result.status == 404
    assert result.startswith("Error: Test resource not found") and "gone" in result
    assert stream.pulled == 4 and stream.closed


@pytest.mark.asyncio
async def test_an_absurd_content_length_is_left_to_the_streamed_count():
    stream = RecordingStream([b"ok"])
    async with _client(lambda _r: httpx.Response(200, headers={"Content-Length": "9" * 5000}, stream=stream)) as client:
        result = await _fetch(client, max_bytes=10)

    assert isinstance(result, FetchedFile) and result.data == b"ok"


@pytest.mark.asyncio
async def test_a_compressed_error_body_is_bounded_once_decoded_too():
    body = gzip.compress(b'{"error": {"message": "gone"}}' + b" " * (20 * 1024 * 1024))
    stream = RecordingStream([body])
    headers = {"Content-Type": "application/json", "Content-Encoding": "gzip"}
    async with _client(lambda _r: httpx.Response(404, headers=headers, stream=stream)) as client:
        result = await _fetch(client, max_bytes=10)

    assert isinstance(result, FetchError) and result.status == 404 and "gone" in result
    assert stream.closed


def test_the_decoded_error_body_never_passes_the_bound():
    from app.integrations import file_delivery

    bomb = gzip.compress(b"x" * (20 * 1024 * 1024))
    decoded = file_delivery._decode_bounded(bomb, "gzip")
    assert len(decoded) == file_delivery._MAX_ERROR_BODY_BYTES
    assert file_delivery._decode_bounded(b"\x00not gzip", "gzip") == b""
    assert file_delivery._decode_bounded(b"raw", "br") == b""


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer presigned"}], ids=["no token", "another credential"])
async def test_a_request_not_sent_with_the_clients_token_is_never_retried_with_it(monkeypatch, headers):
    seen: list[str | None] = []
    refreshed: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(403, text="AuthenticationFailed")

    real_client = httpx.AsyncClient

    class _Client(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    async def refresh() -> str:
        refreshed.append(True)
        return "new"

    async with AutoRefreshClient({"Authorization": "Bearer old"}, refresh) as client:
        result = await fetch_file(
            client, "get", URL, provider="Test", action="download", max_bytes=10, headers=headers
        )

    assert isinstance(result, FetchError) and result.status == 403
    assert seen == [headers.get("Authorization")] and refreshed == []
