"""``fetch_image_asset`` - a public image becomes a conversation file in one call."""

from __future__ import annotations

import asyncio
import dataclasses
import io
import socket
import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from app.llm.base import ChatMessage, ChatResponse
from app.services.builtin_tools import build_web_fetch_tool, image_assets
from app.services.builtin_tools.image_assets import (
    ALLOWED_IMAGE_TYPES,
    DEFAULT_HOST_THROTTLE_SECONDS,
    MAX_CONCURRENT_DOWNLOADS_PER_HOST,
    MAX_HOST_THROTTLE_SECONDS,
    MAX_IMAGE_ASSET_BYTES,
    HostThrottledError,
    ImageAssetError,
    declared_image_type,
    download_public_image,
    host_key,
    host_throttle,
    normalize_asset_filename,
    record_host_throttle,
    retry_after_seconds,
    sniff_raster_type,
    svg_dimensions,
)
from app.tools import agent_loop as al
from app.tools.base import CompositeToolExecutor, FileContext, ToolCall
from tests.test_hubspot_rate_limit import _FakeClock
from tests.test_tool_call_dedup import _quiet_langfuse, _ScriptedLLM, _tool_call_response  # noqa: F401


def _png(width: int = 4, height: int = 3) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (15, 188, 255)).save(buffer, "PNG")
    return buffer.getvalue()


def _raster(fmt: str, width: int = 4, height: int = 3) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (15, 188, 255)).save(buffer, fmt)
    return buffer.getvalue()


SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 40"><rect width="120" height="40" fill="#000091"/></svg>'


# ── Pure helpers ──────────────────────────────────────────────────────────


def test_declared_image_type_is_an_allow_list():
    assert declared_image_type("image/png") == "image/png"
    assert declared_image_type("IMAGE/JPEG; charset=binary") == "image/jpeg"
    assert declared_image_type("image/svg+xml") == "image/svg+xml"
    assert declared_image_type("text/html") is None
    assert declared_image_type("application/octet-stream") is None
    assert declared_image_type(None) is None
    assert set(ALLOWED_IMAGE_TYPES) >= {"image/png", "image/jpeg", "image/svg+xml", "image/webp"}


def test_filename_is_made_safe_and_carries_the_types_extension():
    assert normalize_asset_filename("anssi logo", "https://x.test/a", "image/svg+xml") == "anssi-logo.svg"
    assert normalize_asset_filename(None, "https://x.test/dir/cyber.png?v=2", "image/png") == "cyber.png"
    assert normalize_asset_filename("photo.jpeg", "https://x.test/p", "image/jpeg") == "photo.jpeg"
    assert normalize_asset_filename("../../etc/passwd", "https://x.test/p", "image/png") == "etc-passwd.png"
    assert normalize_asset_filename("", "https://x.test/", "image/webp") == "image.webp"
    # A name spelling another image type is re-typed by the decided type, never double-suffixed.
    assert normalize_asset_filename("logo.png", "https://x.test/p", "image/jpeg") == "logo.jpg"
    assert normalize_asset_filename(None, "https://x.test/cdn/mark.PNG", "image/webp") == "mark.webp"
    assert normalize_asset_filename("logo.PNG", "https://x.test/p", "image/jpeg") == "logo.jpg"


def test_sniff_raster_type_reads_the_magic_number_not_a_header():
    assert sniff_raster_type(_raster("PNG")) == "image/png"
    assert sniff_raster_type(_raster("JPEG")) == "image/jpeg"
    assert sniff_raster_type(_raster("GIF")) == "image/gif"
    assert sniff_raster_type(_raster("WEBP")) == "image/webp"
    assert sniff_raster_type(_raster("BMP")) is None, "decodable, but not a type a slide carries"
    assert sniff_raster_type(SVG) is None
    assert sniff_raster_type(b"<html>not a png</html>") is None
    assert sniff_raster_type(b"") is None


def test_svg_dimensions_read_width_height_then_viewbox():
    assert svg_dimensions(SVG) == (120, 40)
    assert svg_dimensions(b'<svg width="300px" height="150"></svg>') == (300, 150)
    assert svg_dimensions(b'<svg width="100%" viewBox="0,0,50,20"></svg>') == (50, 20)
    assert svg_dimensions(b"<not-svg/>") is None


# ── The download under the outbound policy ────────────────────────────────


class _Route:
    """A scripted HTTP surface: url -> (status, headers, body)."""

    def __init__(self, routes: dict[str, tuple[int, dict[str, str], bytes]]) -> None:
        self.routes = routes
        self.requested: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requested.append(str(request.url))
        status, headers, body = self.routes[str(request.url)]
        return httpx.Response(status, headers=headers, content=body, request=request)


@pytest.fixture
def _open_policy(monkeypatch):
    """Let the URL validator pass any http(s) URL (the test hosts are fake)."""

    async def _validate(url: str, *, allow_private: bool = False) -> str:
        if not url.startswith(("http://", "https://")):
            raise ValueError("scheme")
        if "blocked.internal" in url:
            raise ValueError("address is not globally routable")
        return url

    monkeypatch.setattr("app.services.web_fetch.validate_outbound_url_async", _validate)


@pytest.fixture
def _http(monkeypatch):
    holder: dict[str, _Route] = {}

    def _install(routes):
        route = _Route(routes)
        holder["route"] = route
        real = httpx.AsyncClient

        def _client(**kwargs):
            kwargs.pop("follow_redirects", None)
            kwargs.pop("transport", None)  # the mock stands for the network
            return real(transport=httpx.MockTransport(route.handler), follow_redirects=False, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _client)
        return route

    return _install


@pytest.mark.asyncio
async def test_download_follows_validated_redirects_and_returns_the_bytes(_open_policy, _http):
    png = _png()
    route = _http(
        {
            "https://x.test/logo": (302, {"location": "/static/logo.png"}, b""),
            "https://x.test/static/logo.png": (200, {"content-type": "image/png"}, png),
        }
    )
    data, mime, final = await download_public_image("https://x.test/logo")
    assert data == png and mime == "image/png"
    assert final == "https://x.test/static/logo.png"
    assert route.requested == ["https://x.test/logo", "https://x.test/static/logo.png"]


@pytest.mark.asyncio
async def test_download_refuses_what_the_policy_and_the_allow_list_refuse(monkeypatch, _open_policy, _http):
    # Five downloads from one host, one after another: past the first three the
    # per-host pace would spend real seconds between them.
    monkeypatch.setattr(image_assets, "DOWNLOAD_STARTS_PER_SECOND_PER_HOST", 1000.0)
    _http(
        {
            "https://x.test/page": (200, {"content-type": "text/html"}, b"<html/>"),
            "https://x.test/big": (200, {"content-type": "image/png", "content-length": str(MAX_IMAGE_ASSET_BYTES + 1)}, b""),
            "https://x.test/stream": (200, {"content-type": "image/png"}, b"x" * (MAX_IMAGE_ASSET_BYTES + 10)),
            "https://x.test/hop": (302, {"location": "https://blocked.internal/secret.png"}, b""),
            "https://x.test/missing": (404, {"content-type": "image/png"}, b""),
        }
    )
    with pytest.raises(ImageAssetError, match="not an image the platform accepts"):
        await download_public_image("https://x.test/page")
    with pytest.raises(ImageAssetError, match=r"above the 5\.0 MB ceiling"):
        await download_public_image("https://x.test/big")
    # The mock transport declares the body's length, so the declared-size
    # check answers first; a chunked body past the ceiling is refused by the
    # streaming count with the other wording. Either way: the ceiling.
    with pytest.raises(ImageAssetError, match=r"5\.0 MB ceiling"):
        await download_public_image("https://x.test/stream")
    with pytest.raises(ImageAssetError, match="redirect refused"):
        await download_public_image("https://x.test/hop")
    with pytest.raises(ImageAssetError, match="HTTP 404"):
        await download_public_image("https://x.test/missing")
    with pytest.raises(ImageAssetError, match="url refused"):
        await download_public_image("https://blocked.internal/a.png")
    with pytest.raises(ImageAssetError, match="must start with http"):
        await download_public_image("ftp://x.test/a.png")


@pytest.mark.asyncio
async def test_a_content_length_no_int_can_hold_is_not_read_as_a_size(monkeypatch, _open_policy):
    """``Content-Length`` is the server's too: a value past Python's int-string
    limit or a non-ASCII digit (one latin-1 byte) raised ``ValueError`` past
    every refusal the tool words. It is not read as a size; the streaming count
    still holds the ceiling."""
    png = _png()

    def handler(request: httpx.Request) -> httpx.Response:
        length = b"9" * 5000 if request.url.path == "/long.png" else b"\xb2"
        headers = [(b"content-type", b"image/png"), (b"content-length", length)]
        return httpx.Response(200, headers=headers, content=png, request=request)

    _install_async_transport(monkeypatch, handler)
    for url in ("https://x.test/long.png", "https://x.test/odd.png"):
        data, mime, _final = await download_public_image(url)
        assert data == png and mime == "image/png"


# ── The tool ──────────────────────────────────────────────────────────────


def _build(monkeypatch, persisted: list[dict]):
    async def _fake_persist(**kwargs):
        persisted.append(kwargs)
        return SimpleNamespace(
            id=uuid.uuid4(),
            filename=kwargs["filename"],
            content_type=kwargs["content_type"],
            size=len(kwargs["raw_bytes"]),
            s3_object_name=f"chat/conv/{kwargs['filename']}",
            source=kwargs["source"],
        )

    monkeypatch.setattr("app.services.builtin_tools._shared._persist_generated_file", _fake_persist)
    return build_web_fetch_tool(db=object(), user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4()))


async def _call(executor, **arguments):
    return await executor.execute_tool(ToolCall(id="c1", name="fetch_image_asset", arguments=arguments))


def test_fetch_image_asset_rides_the_web_fetch_suite():
    defs, _executor = build_web_fetch_tool()
    assert [d.name for d in defs] == ["web_fetch", "fetch_image_asset"]
    assert {d.source for d in defs} == {"builtin:web"}
    asset = defs[1]
    assert asset.input_schema["required"] == ["url"]
    assert "name" in asset.input_schema["properties"]


@pytest.mark.asyncio
async def test_fetch_image_asset_stores_the_file_and_answers_with_the_handle(monkeypatch, _open_policy, _http):
    png = _png(4, 3)
    _http({"https://x.test/cyber.png": (200, {"content-type": "image/png"}, png)})
    persisted: list[dict] = []
    _defs, executor = _build(monkeypatch, persisted)
    ctx = FileContext()
    executor.file_context = ctx

    result = await _call(executor, url="https://x.test/cyber.png", name="anssi-logo")
    assert result.is_error is False, result.content
    assert persisted[0]["raw_bytes"] == png
    assert persisted[0]["filename"] == "anssi-logo.png"
    assert persisted[0]["content_type"] == "image/png"
    assert persisted[0]["source"] == "agent_download"
    # Its geometry is stored with it: an opaque file is its own mark.
    assert persisted[0]["image_meta"]["trim"] == [0.0, 0.0, 1.0, 1.0]
    file_id = ctx.list_files()[0].file_id
    assert f"as file_id {file_id}" in result.content
    assert "Fetched anssi-logo.png (image/png," in result.content
    assert "4x3 px" in result.content
    assert "Visual summary:" in result.content
    assert "attach_template_asset" in result.content
    assert result.files is None, "an asset is a handle, not a vision block"


@pytest.mark.asyncio
async def test_fetch_image_asset_sanitises_an_svg_and_reads_its_size(monkeypatch, _open_policy, _http):
    dirty = SVG.replace(b"<rect", b"<script>alert(1)</script><rect")
    _http({"https://x.test/mark.svg": (200, {"content-type": "image/svg+xml"}, dirty)})
    persisted: list[dict] = []
    _defs, executor = _build(monkeypatch, persisted)
    executor.file_context = FileContext()

    result = await _call(executor, url="https://x.test/mark.svg")
    assert result.is_error is False, result.content
    assert b"<script" not in persisted[0]["raw_bytes"]
    assert persisted[0]["filename"] == "mark.svg"
    assert "120x40 px" in result.content
    assert "SVG (vector)" in result.content
    # Read through its rendering: the sanitised mark, stored with the file.
    assert persisted[0]["image_meta"]["trim"] == [0.0, 0.0, 1.0, 1.0]


def _persist_reusing(monkeypatch, existing, stored: list[tuple]):
    """`_persist_generated_file` for bytes the conversation already holds as *existing*."""
    from app.services.builtin_tools import _shared

    async def find_existing(**_kwargs):
        return existing

    async def store_image_meta(file_id, image_meta):
        stored.append((file_id, image_meta))

    monkeypatch.setattr(_shared, "_find_existing_tool_file", find_existing)
    monkeypatch.setattr(_shared, "_store_image_meta", store_image_meta)
    return _shared._persist_generated_file(
        user_id=str(uuid.uuid4()),
        filename="logo.png",
        raw_bytes=_png(),
        content_type="image/png",
        conversation_id=str(uuid.uuid4()),
        assignment_run_id=None,
        source="agent_download",
        image_meta={"v": 1, "w": 4, "h": 3, "trim": [0.0, 0.0, 1.0, 1.0]},
    )


@pytest.mark.asyncio
async def test_a_reused_byte_identical_file_is_given_the_geometry_it_lacked(monkeypatch):
    """The dedup hands back the row the conversation already holds: a
    row stored before its geometry existed gets it now, and a row that has it
    is left alone."""
    bare = SimpleNamespace(id=uuid.uuid4(), image_meta=None)
    stored: list[tuple] = []
    assert await _persist_reusing(monkeypatch, bare, stored) is bare
    assert stored == [(bare.id, {"v": 1, "w": 4, "h": 3, "trim": [0.0, 0.0, 1.0, 1.0]})]

    read = SimpleNamespace(id=uuid.uuid4(), image_meta={"v": 1})
    stored.clear()
    assert await _persist_reusing(monkeypatch, read, stored) is read
    assert stored == []


@pytest.mark.asyncio
async def test_fetch_image_asset_refuses_bytes_that_are_not_the_declared_image(monkeypatch, _open_policy, _http):
    """An HTML error page under an image/png header: refused by the magic
    number, before Pillow is ever asked (monkeypatched to prove it); a real
    PNG header over a body that does not decode is refused by the decode."""
    _http(
        {
            "https://x.test/fake.png": (200, {"content-type": "image/png"}, b"<html>not a png</html>"),
            "https://x.test/cut.png": (200, {"content-type": "image/png"}, _png()[:24]),
        }
    )
    persisted: list[dict] = []
    _defs, executor = _build(monkeypatch, persisted)
    executor.file_context = FileContext()
    decoded: list[bytes] = []

    async def _summarize(data):
        decoded.append(data)
        return None

    monkeypatch.setattr("app.services.image_summary.summarize_image", _summarize)
    result = await _call(executor, url="https://x.test/fake.png")
    assert result.is_error is True
    assert "not one of the image types a slide can carry" in result.content
    assert decoded == [], "no decoder ran on bytes whose magic number is not an allow-listed image"
    cut = await _call(executor, url="https://x.test/cut.png")
    assert cut.is_error is True
    assert "do not decode as an image" in cut.content
    assert len(decoded) == 1
    assert persisted == []


@pytest.mark.asyncio
async def test_fetch_image_asset_types_the_file_by_its_bytes_not_by_the_header(monkeypatch, _open_policy, _http):
    """A CDN that serves JPEG bytes under an image/png header: the stored type,
    the extension and the result all say what the bytes ARE."""
    jpeg = _raster("JPEG")
    _http({"https://x.test/cdn/logo.png": (200, {"content-type": "image/png"}, jpeg)})
    persisted: list[dict] = []
    _defs, executor = _build(monkeypatch, persisted)
    executor.file_context = FileContext()

    result = await _call(executor, url="https://x.test/cdn/logo.png", name="anssi-logo.png")
    assert result.is_error is False, result.content
    assert persisted[0]["raw_bytes"] == jpeg
    assert persisted[0]["content_type"] == "image/jpeg"
    assert persisted[0]["filename"] == "anssi-logo.jpg"
    assert "Fetched anssi-logo.jpg (image/jpeg," in result.content


@pytest.mark.asyncio
async def test_fetch_image_asset_refuses_a_decodable_raster_outside_the_allow_list(monkeypatch, _open_policy, _http):
    """Pillow decodes a BMP happily; a slide cannot carry one, so a BMP under an
    image/png header is refused by the sniff, never stored under the header's type."""
    _http({"https://x.test/mark.png": (200, {"content-type": "image/png"}, _raster("BMP"))})
    persisted: list[dict] = []
    _defs, executor = _build(monkeypatch, persisted)
    executor.file_context = FileContext()

    result = await _call(executor, url="https://x.test/mark.png")
    assert result.is_error is True
    assert "not one of the image types a slide can carry" in result.content
    assert "the server declared image/png" in result.content
    assert persisted == []


@pytest.mark.asyncio
async def test_fetch_image_asset_stores_a_favicon_as_png(monkeypatch, _open_policy, _http):
    """A vendor's favicon.ico is often its only public logo: the download takes
    image/x-icon and stores the largest frame as a PNG a slide can carry."""
    from PIL import Image

    ico = _raster("ICO", 32, 32)
    assert ico.startswith(b"\x00\x00\x01\x00")
    _http(
        {
            "https://x.test/favicon.ico": (200, {"content-type": "image/x-icon"}, ico),
            "https://x.test/broken.ico": (200, {"content-type": "image/vnd.microsoft.icon"}, ico[:10]),
        }
    )
    persisted: list[dict] = []
    _defs, executor = _build(monkeypatch, persisted)
    executor.file_context = FileContext()

    result = await _call(executor, url="https://x.test/favicon.ico")
    assert result.is_error is False, result.content
    assert persisted[0]["content_type"] == "image/png"
    assert persisted[0]["filename"] == "favicon.png"
    assert persisted[0]["raw_bytes"].startswith(b"\x89PNG")
    with Image.open(io.BytesIO(persisted[0]["raw_bytes"])) as stored:
        assert stored.size == (32, 32)
    assert "Fetched favicon.png (image/png," in result.content

    broken = await _call(executor, url="https://x.test/broken.ico")
    assert broken.is_error is True
    assert "does not decode as an ICO file" in broken.content
    assert len(persisted) == 1


def test_ico_to_png_refuses_what_is_not_a_bounded_icon():
    from app.services.builtin_tools.image_assets import ico_to_png

    assert ico_to_png(_png()) is None, "a PNG is not an ICO"
    assert ico_to_png(b"") is None
    assert ico_to_png(b"\x00\x00\x01\x00garbage") is None
    assert ico_to_png(_raster("ICO", 16, 16)).startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_fetch_image_asset_relays_a_download_refusal_and_needs_a_store(monkeypatch, _open_policy, _http):
    _http({"https://x.test/page": (200, {"content-type": "text/html"}, b"<html/>")})
    persisted: list[dict] = []
    _defs, executor = _build(monkeypatch, persisted)
    executor.file_context = FileContext()
    result = await _call(executor, url="https://x.test/page")
    assert result.is_error is True
    assert result.content.startswith("Error: https://x.test/page is not an image the platform accepts")

    _defs, bare = build_web_fetch_tool()
    result = await bare.execute_tool(ToolCall(id="c2", name="fetch_image_asset", arguments={"url": "https://x.test/a.png"}))
    assert result.is_error is True
    assert "needs a conversation or run" in result.content


def test_the_module_never_widens_the_outbound_policy():
    """Every request goes through the shared validator - the URL and each
    redirect hop - and nothing in the module resolves a host on its own."""
    import inspect

    source = inspect.getsource(image_assets)
    assert source.count("validate_outbound_url_async(") >= 2
    assert "socket." not in source
    assert "follow_redirects=False" in source


# ── Pacing and throttling per host ────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_host_state():
    """Throttles and paced slots are process-wide: every test starts from none."""
    from app.integrations import http as integration_http

    def _reset() -> None:
        image_assets._THROTTLED_HOSTS.clear()
        for key in [k for k in integration_http._RATE_LIMITER_REGISTRY if k.startswith("fetch_image_asset:")]:
            del integration_http._RATE_LIMITER_REGISTRY[key]

    _reset()
    yield
    _reset()


def _install_async_transport(monkeypatch, handler) -> None:
    real = httpx.AsyncClient

    def _client(**kwargs):
        kwargs.pop("follow_redirects", None)
        kwargs.pop("transport", None)  # the mock stands for the network
        return real(transport=httpx.MockTransport(handler), follow_redirects=False, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)


async def _until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting for the downloads to start"
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_image_downloads_and_web_fetch_send_one_user_agent_that_says_how_to_reach_the_operator(
    monkeypatch, _open_policy
):
    """One User-Agent for both outbound tools, checked on the wire. Its base,
    ``FiligranCopilot/1.0``, is not a browser's, so adding the contact URL
    public hosts ask for (Wikimedia's policy) puts ``web_fetch`` under no
    site's bot rule it was not under already."""
    from app.services.web_fetch import USER_AGENT, fetch_and_extract

    assert USER_AGENT == "FiligranCopilot/1.0 (+https://filigran.io)"
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent"))
        if request.url.path.endswith(".png"):
            return httpx.Response(200, headers={"content-type": "image/png"}, content=_png(), request=request)
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"hello", request=request)

    _install_async_transport(monkeypatch, handler)
    # `fetch_and_extract` resolves the name itself to pin the connection.
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )
    await download_public_image("https://media.test/a.png")
    page = await fetch_and_extract("https://pages.test/readme.txt")
    assert page.text == "hello"
    assert seen == [USER_AGENT, USER_AGENT]


def test_retry_after_reads_both_forms_and_is_bounded():
    now = datetime(2026, 9, 26, 9, 44, 21, tzinfo=UTC)
    assert retry_after_seconds("120", now=now) == 120
    assert retry_after_seconds(" 30 ", now=now) == 30
    assert retry_after_seconds("Sat, 26 Sep 2026 09:46:21 GMT", now=now) == 120
    assert retry_after_seconds("Fri, 31 Dec 9999 23:59:59 GMT", now=now) == MAX_HOST_THROTTLE_SECONDS
    assert retry_after_seconds("86400", now=now) == MAX_HOST_THROTTLE_SECONDS
    for unusable in (None, "", "0", "-5", "1.5", "soon", "Sat, 26 Sep 2026 09:40:00 GMT"):
        assert retry_after_seconds(unusable, now=now) is None, unusable


def test_retry_after_never_raises_on_what_a_server_can_send():
    """``Retry-After`` is untrusted input. An all-digit value past Python's
    int-string limit (4300 digits) and a non-ASCII digit - U+00B2 passes
    ``str.isdigit`` and arrives from ONE latin-1 byte - raised ``ValueError``
    out of the parser: the 429 was neither remembered nor ``target_throttled``."""
    now = datetime(2026, 9, 26, 9, 44, 21, tzinfo=UTC)
    assert retry_after_seconds("9" * 5000, now=now) == MAX_HOST_THROTTLE_SECONDS
    assert retry_after_seconds("12345678901234567890", now=now) == MAX_HOST_THROTTLE_SECONDS
    for odd in ("\u00b2", "1\u00b2", "\u0663", "\uff11\uff12"):
        assert retry_after_seconds(odd, now=now) is None, repr(odd)


@pytest.mark.asyncio
async def test_a_hostile_retry_after_still_throttles_its_host(monkeypatch, _open_policy):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "long.test":
            return httpx.Response(429, headers=[(b"retry-after", b"9" * 5000)], request=request)
        return httpx.Response(429, headers=[(b"retry-after", b"\xb2")], request=request)

    _install_async_transport(monkeypatch, handler)
    _defs, executor = _build(monkeypatch, [])
    executor.file_context = FileContext()

    long_wait = await _call(executor, url="https://long.test/a.png")
    assert long_wait.is_error and long_wait.target_throttled, long_wait.content
    assert "long.test answered HTTP 429" in long_wait.content
    remaining = host_throttle("long.test").deadline - time.monotonic()
    assert MAX_HOST_THROTTLE_SECONDS - 5 < remaining <= MAX_HOST_THROTTLE_SECONDS

    odd = await _call(executor, url="https://odd.test/a.png")
    assert odd.is_error and odd.target_throttled, odd.content
    assert "it gave no Retry-After, so the platform waits 5 min" in odd.content
    assert host_throttle("odd.test") is not None


def test_one_host_is_one_key_whatever_its_spelling():
    """The throttle memory and the pace are keyed on the host: case, the dot of
    a fully-qualified name, the Unicode and the punycode spelling of one IDN
    and two spellings of one IPv6 literal are ONE key, or a throttled host is
    asked again under another spelling. The port is not part of it: a throttle
    is the server's, whatever port it answered on."""
    assert host_key("https://Upload.Wikimedia.ORG/a.png") == "upload.wikimedia.org"
    assert host_key("https://upload.wikimedia.org./a.png") == "upload.wikimedia.org"
    assert host_key("https://b\u00fccher.de/a.png") == "xn--bcher-kva.de"
    assert host_key("https://xn--bcher-kva.de/b.png") == "xn--bcher-kva.de"
    assert host_key("https://[2001:DB8:0:0::1]:443/a.png") == "2001:db8::1"
    assert host_key("https://[2001:db8::1]/b.png") == "2001:db8::1"
    assert host_key("https://user:secret@media.test:8443/a.png") == "media.test"
    assert host_key("https://1.2.3.4/a.png") == "1.2.3.4"


@pytest.mark.asyncio
async def test_a_throttled_host_is_not_asked_again_under_another_spelling(monkeypatch, _open_policy):
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(429, headers={"retry-after": "120"}, request=request)

    _install_async_transport(monkeypatch, handler)
    _defs, executor = _build(monkeypatch, [])
    executor.file_context = FileContext()

    for first, spellings in (
        ("https://MEDIA.test./a.png", ("https://media.test/b.png", "https://Media.Test:8443/c.png")),
        ("https://b\u00fccher.test/a.png", ("https://xn--bcher-kva.test/b.png",)),
    ):
        opened = await _call(executor, url=first)
        assert opened.target_throttled and "answered HTTP 429" in opened.content, opened.content
        sent = len(requested)
        for spelling in spellings:
            again = await _call(executor, url=spelling)
            assert again.target_throttled, again.content
            assert again.content.startswith("Error: not fetched:"), again.content
        assert len(requested) == sent, f"{spellings} reached the network: {requested}"


def test_a_shorter_retry_after_never_reopens_a_host_early():
    first = record_host_throttle("media.test", "600")
    assert record_host_throttle("media.test", "10") is first
    longer = record_host_throttle("media.test", "800")
    assert longer.deadline > first.deadline
    assert longer.since == first.since, "still the throttle that began with the first 429"


def test_the_throttle_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(image_assets, "_MAX_THROTTLED_HOSTS", 3)
    for i in range(5):
        record_host_throttle(f"h{i}.test", str(100 + i))
    assert set(image_assets._THROTTLED_HOSTS) == {"h2.test", "h3.test", "h4.test"}, (
        "the throttles that lift soonest make room"
    )


@pytest.mark.asyncio
async def test_a_429_throttles_its_host_and_later_calls_send_no_request(monkeypatch, _open_policy, _http):
    png = _png()
    route = _http(
        {
            "https://media.test/a.svg": (429, {"retry-after": "120"}, b""),
            "https://media.test/b.png": (200, {"content-type": "image/png"}, png),
            "https://other.test/c.png": (200, {"content-type": "image/png"}, png),
        }
    )
    _defs, executor = _build(monkeypatch, [])
    executor.file_context = FileContext()

    first = await _call(executor, url="https://media.test/a.svg")
    assert first.is_error and first.target_throttled
    assert "media.test answered HTTP 429 (Too Many Requests)" in first.content
    assert "as its Retry-After asks" in first.content
    assert "another source" in first.content

    second = await _call(executor, url="https://media.test/b.png")
    assert second.is_error and second.target_throttled
    assert second.content.startswith(
        "Error: not fetched: https://media.test/b.png - media.test has been throttling downloads"
    )
    assert route.requested == ["https://media.test/a.svg"], "a throttled host is refused without a request"

    other = await _call(executor, url="https://other.test/c.png")
    assert other.is_error is False and other.target_throttled is False, other.content
    assert route.requested == ["https://media.test/a.svg", "https://other.test/c.png"]


@pytest.mark.asyncio
async def test_without_retry_after_the_host_waits_the_default_then_is_asked_again(monkeypatch, _open_policy, _http):
    png = _png()
    route = _http(
        {
            "https://media.test/a.png": (429, {}, b""),
            "https://media.test/b.png": (200, {"content-type": "image/png"}, png),
        }
    )
    _defs, executor = _build(monkeypatch, [])
    executor.file_context = FileContext()

    first = await _call(executor, url="https://media.test/a.png")
    assert "it gave no Retry-After, so the platform waits 5 min" in first.content
    throttle = host_throttle("media.test")
    assert throttle is not None and not throttle.retry_after
    assert DEFAULT_HOST_THROTTLE_SECONDS - 5 < throttle.deadline - time.monotonic() <= DEFAULT_HOST_THROTTLE_SECONDS

    image_assets._THROTTLED_HOSTS["media.test"] = dataclasses.replace(throttle, deadline=time.monotonic() - 1)
    again = await _call(executor, url="https://media.test/b.png")
    assert again.is_error is False, again.content
    assert route.requested == ["https://media.test/a.png", "https://media.test/b.png"]
    assert host_throttle("media.test") is None


@pytest.mark.asyncio
async def test_downloads_are_capped_per_host_and_hosts_do_not_wait_on_each_other(monkeypatch, _open_policy):
    monkeypatch.setattr(image_assets, "DOWNLOAD_STARTS_PER_SECOND_PER_HOST", 1000.0)
    png = _png()
    in_flight = {"media.test": 0, "other.test": 0}
    peak = dict(in_flight)
    gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        in_flight[host] += 1
        peak[host] = max(peak[host], in_flight[host])
        await gate.wait()
        in_flight[host] -= 1
        return httpx.Response(200, headers={"content-type": "image/png"}, content=png, request=request)

    _install_async_transport(monkeypatch, handler)
    urls = [f"https://media.test/{i}.png" for i in range(8)] + [f"https://other.test/{i}.png" for i in range(3)]
    tasks = [asyncio.ensure_future(download_public_image(url)) for url in urls]
    await _until(lambda: in_flight["media.test"] == MAX_CONCURRENT_DOWNLOADS_PER_HOST and in_flight["other.test"] == 3)
    await asyncio.sleep(0.05)
    assert in_flight["media.test"] == MAX_CONCURRENT_DOWNLOADS_PER_HOST, "the other five wait for a slot"
    gate.set()
    results = await asyncio.gather(*tasks)
    assert [data for data, _mime, _url in results] == [png] * len(urls)
    assert peak == {"media.test": MAX_CONCURRENT_DOWNLOADS_PER_HOST, "other.test": 3}


@pytest.mark.asyncio
async def test_a_call_waiting_for_its_slot_is_refused_once_a_429_lands(monkeypatch, _open_policy):
    monkeypatch.setattr(image_assets, "DOWNLOAD_STARTS_PER_SECOND_PER_HOST", 1000.0)
    requested: list[str] = []
    gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        await gate.wait()
        return httpx.Response(429, headers={"retry-after": "60"}, request=request)

    _install_async_transport(monkeypatch, handler)
    tasks = [asyncio.ensure_future(download_public_image(f"https://media.test/{i}.png")) for i in range(5)]
    await _until(lambda: len(requested) == MAX_CONCURRENT_DOWNLOADS_PER_HOST)
    await asyncio.sleep(0.05)
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(r, HostThrottledError) for r in results), results
    assert len(requested) == MAX_CONCURRENT_DOWNLOADS_PER_HOST, "the two that waited never sent a request"
    assert sum("answered HTTP 429" in str(r) for r in results) == 3
    assert sum(str(r).startswith("not fetched:") for r in results) == 2


def _paced_limiter(host: str, sleep_fn) -> None:
    """Register *host*'s shared limiter on a fake clock, with the tool's own
    pace, so a test can see every wait for a start without spending one."""
    from app.integrations import http as integration_http

    clock = _FakeClock()

    async def _sleep(seconds: float) -> None:
        await sleep_fn(seconds)
        clock.now += seconds

    integration_http._RATE_LIMITER_REGISTRY[f"fetch_image_asset:{host}"] = integration_http.RateLimiter(
        rate=image_assets.DOWNLOAD_STARTS_PER_SECOND_PER_HOST,
        capacity=MAX_CONCURRENT_DOWNLOADS_PER_HOST,
        max_concurrency=MAX_CONCURRENT_DOWNLOADS_PER_HOST,
        time_fn=clock.time,
        sleep_fn=_sleep,
    )


@pytest.mark.asyncio
async def test_calls_queued_behind_a_429_are_refused_without_waiting_for_their_start(monkeypatch, _open_policy):
    """Five parallel calls to one host at the real pace: three go out and are
    answered 429, two wait for a slot. When a slot frees, a waiting call is
    refused on the spot - it never waits 1/rate for a start just to be told no
    (a 17-call batch spent ~7 s being refused, 0.5 s per queued call)."""
    starts_waited: list[float] = []

    async def _record(seconds: float) -> None:
        starts_waited.append(seconds)

    _paced_limiter("media.test", _record)
    requested: list[str] = []
    gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        await gate.wait()
        return httpx.Response(429, headers={"retry-after": "60"}, request=request)

    _install_async_transport(monkeypatch, handler)
    tasks = [asyncio.ensure_future(download_public_image(f"https://media.test/{i}.png")) for i in range(5)]
    await _until(lambda: len(requested) == MAX_CONCURRENT_DOWNLOADS_PER_HOST)
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(r, HostThrottledError) for r in results), results
    assert len(requested) == MAX_CONCURRENT_DOWNLOADS_PER_HOST
    assert starts_waited == [], "a call the throttle refuses never waits for a start"


@pytest.mark.asyncio
async def test_a_redirect_gives_its_hosts_slot_back_before_it_asks_for_the_next(monkeypatch, _open_policy):
    """Three downloads redirected from a.test to b.test and stuck there: a.test's
    slots are free again, so a fourth download from a.test goes out at once. A
    hop keeping a.test's slot while it waits on b.test would starve a.test."""
    monkeypatch.setattr(image_assets, "DOWNLOAD_STARTS_PER_SECOND_PER_HOST", 1000.0)
    png = _png()
    gate = asyncio.Event()
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "a.test" and request.url.path.startswith("/r"):
            return httpx.Response(302, headers={"location": f"https://b.test{request.url.path}"}, request=request)
        if request.url.host == "b.test":
            await gate.wait()
        return httpx.Response(200, headers={"content-type": "image/png"}, content=png, request=request)

    _install_async_transport(monkeypatch, handler)
    chains = [asyncio.ensure_future(download_public_image(f"https://a.test/r{i}.png")) for i in range(3)]
    await _until(lambda: sum(url.startswith("https://b.test/") for url in requested) == 3)
    direct, _mime, _final = await asyncio.wait_for(download_public_image("https://a.test/logo.png"), timeout=2)
    assert direct == png
    gate.set()
    finals = [final for _data, _mime, final in await asyncio.gather(*chains)]
    assert finals == [f"https://b.test/r{i}.png" for i in range(3)]


@pytest.mark.asyncio
async def test_redirect_chains_crossing_two_full_hosts_never_deadlock(monkeypatch, _open_policy):
    """Three chains a.test -> b.test and three b.test -> a.test at once, every
    slot of both hosts taken when they redirect: each hop gives its slot back
    before it asks for the other host's, so all six complete."""
    monkeypatch.setattr(image_assets, "DOWNLOAD_STARTS_PER_SECOND_PER_HOST", 1000.0)
    png = _png()
    all_first_hops_in = asyncio.Barrier(2 * MAX_CONCURRENT_DOWNLOADS_PER_HOST)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/x"):
            await all_first_hops_in.wait()
            other = "b.test" if request.url.host == "a.test" else "a.test"
            location = f"https://{other}/y{request.url.path[2:]}"
            return httpx.Response(302, headers={"location": location}, request=request)
        return httpx.Response(200, headers={"content-type": "image/png"}, content=png, request=request)

    _install_async_transport(monkeypatch, handler)
    urls = [f"https://{host}/x{i}.png" for host in ("a.test", "b.test") for i in range(MAX_CONCURRENT_DOWNLOADS_PER_HOST)]
    results = await asyncio.wait_for(asyncio.gather(*(download_public_image(url) for url in urls)), timeout=2)
    assert [data for data, _mime, _final in results] == [png] * len(urls)


@pytest.mark.asyncio
async def test_a_cancelled_download_gives_its_slot_back(monkeypatch, _open_policy):
    """A turn cancelled mid-download, or a tool timeout, cancels the call: its
    slot - held or waited for - is released, or the host would be left short
    of slots for the life of the process."""
    from app.integrations import http as integration_http

    monkeypatch.setattr(image_assets, "DOWNLOAD_STARTS_PER_SECOND_PER_HOST", 1000.0)
    png = _png()
    in_flight = 0
    never = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight
        if request.url.path.startswith("/stuck"):
            in_flight += 1
            await never.wait()
        return httpx.Response(200, headers={"content-type": "image/png"}, content=png, request=request)

    _install_async_transport(monkeypatch, handler)
    stuck = [asyncio.ensure_future(download_public_image(f"https://media.test/stuck{i}.png")) for i in range(4)]
    await _until(lambda: in_flight == MAX_CONCURRENT_DOWNLOADS_PER_HOST)
    for task in stuck:
        task.cancel()
    outcomes = await asyncio.gather(*stuck, return_exceptions=True)
    assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes), outcomes
    assert integration_http._RATE_LIMITER_REGISTRY["fetch_image_asset:media.test"].in_use == 0
    fresh = await asyncio.wait_for(
        asyncio.gather(*(download_public_image(f"https://media.test/ok{i}.png") for i in range(3))), timeout=2
    )
    assert [data for data, _mime, _final in fresh] == [png] * 3


def _composite(monkeypatch) -> CompositeToolExecutor:
    defs, executor = _build(monkeypatch, [])
    composite = CompositeToolExecutor()
    composite.add_tools(defs, executor)
    composite.set_file_context(FileContext())
    return composite


def _fetch(call_id: str, url: str) -> ToolCall:
    return ToolCall(id=call_id, name="fetch_image_asset", arguments={"url": url})


def _tool_result(messages: list[ChatMessage], call_id: str) -> ChatMessage:
    return next(m for m in messages if m.role == "tool" and m.tool_call_id == call_id)


_FAILURE_NUDGE_PREFIX = al._REPEATED_FAILURE_NUDGE.split("]", 1)[0]


@pytest.mark.asyncio
async def test_a_throttled_host_never_takes_the_tool_away_from_other_hosts(monkeypatch, _open_policy, _http, _quiet_langfuse):  # noqa: F811
    """The 26 Sep 2026 run, one call a step: a 429 put ``fetch_image_asset`` on
    cooldown for EVERY host and each refusal counted toward the failure brake.
    Five refusals in a row, then a logo from another host: the tool stays
    offered, nothing is withdrawn, the last fetch runs."""
    png = _png()
    route = _http(
        {
            "https://media.test/0.svg": (429, {"retry-after": "120"}, b""),
            "https://other.test/logo.png": (200, {"content-type": "image/png"}, png),
        }
    )
    composite = _composite(monkeypatch)
    script = [_tool_call_response(_fetch(f"m{i}", f"https://media.test/{i}.svg")) for i in range(5)]
    script.append(_tool_call_response(_fetch("o", "https://other.test/logo.png")))
    script.append(ChatResponse(content="Done.", model="stub"))
    llm = _ScriptedLLM(script)
    messages: list[ChatMessage] = [ChatMessage(role="user", content="logos")]

    response = await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=20
    )

    assert response.content == "Done."
    assert all("fetch_image_asset" in offered for offered in llm.offered_tools), llm.offered_tools
    assert route.requested == ["https://media.test/0.svg", "https://other.test/logo.png"]
    assert all(_tool_result(messages, f"m{i}").content.startswith("Error: ") for i in range(5))
    assert _tool_result(messages, "o").content.startswith("Fetched logo.png")
    assert not any(m.role == "system" and m.content.startswith(_FAILURE_NUDGE_PREFIX) for m in messages)


@pytest.mark.asyncio
async def test_a_throttled_host_in_a_parallel_batch_withdraws_nothing(monkeypatch, _open_policy, _http, _quiet_langfuse):  # noqa: F811
    """The same in one parallel batch - the shape the model actually sends."""
    png = _png()
    routes = {f"https://media.test/{i}.svg": (429, {}, b"") for i in range(5)}
    routes["https://other.test/a.png"] = (200, {"content-type": "image/png"}, png)
    routes["https://other.test/b.png"] = (200, {"content-type": "image/png"}, png)
    route = _http(routes)
    composite = _composite(monkeypatch)
    batch = [_fetch("oa", "https://other.test/a.png")]
    batch += [_fetch(f"m{i}", f"https://media.test/{i}.svg") for i in range(5)]
    llm = _ScriptedLLM(
        [
            _tool_call_response(*batch),
            _tool_call_response(_fetch("ob", "https://other.test/b.png")),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="logos")]

    response = await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=20
    )

    assert response.content == "Done."
    assert all("fetch_image_asset" in offered for offered in llm.offered_tools), llm.offered_tools
    media_requests = [u for u in route.requested if u.startswith("https://media.test/")]
    assert 1 <= len(media_requests) <= MAX_CONCURRENT_DOWNLOADS_PER_HOST
    assert all(_tool_result(messages, f"m{i}").content.startswith("Error: ") for i in range(5))
    assert _tool_result(messages, "oa").content.startswith("Fetched a.png")
    assert _tool_result(messages, "ob").content.startswith("Fetched b.png")
    assert not any(m.role == "system" and m.content.startswith(_FAILURE_NUDGE_PREFIX) for m in messages)


@pytest.mark.asyncio
async def test_the_same_throttled_url_asked_again_and_again_is_still_stopped_by_the_loop_breaker(
    monkeypatch, _open_policy, _http, _quiet_langfuse  # noqa: F811
):
    """``target_throttled`` exempts a result from the cooldown and the failure
    streak, nothing else: a model asking for the same throttled URL again is
    warned at the third identical call and refused from the fifth, and a
    refused call never reaches the tool. Errors are never cached, so each
    allowed call is the tool's own instant refusal, not a replay."""
    route = _http({"https://media.test/logo.svg": (429, {"retry-after": "120"}, b"")})
    composite = _composite(monkeypatch)
    llm = _ScriptedLLM(
        [_tool_call_response(_fetch(f"c{i}", "https://media.test/logo.svg")) for i in range(6)]
        + [ChatResponse(content="Done.", model="stub")]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="logo")]

    response = await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=20
    )

    assert response.content == "Done."
    assert route.requested == ["https://media.test/logo.svg"], "one request; every later answer is the memory's"
    assert all(_tool_result(messages, f"c{i}").content.startswith("Error: ") for i in range(4))
    nudges = [m for m in messages if m.role == "system" and m.content.startswith("[SYSTEM - repeated identical call]")]
    assert len(nudges) == 1 and "'fetch_image_asset' with identical arguments 3 times" in nudges[0].content
    refusals = [m for m in messages if m.role == "tool" and m.content.startswith("[Loop breaker] Refused")]
    assert [m.tool_call_id for m in refusals] == ["c4", "c5"]
