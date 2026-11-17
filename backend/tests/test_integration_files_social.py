"""Social posts and their media: the pipe and the download.

LinkedIn, X, Facebook and Bluesky post tools take files of the run by file_id
through each platform's documented upload flow, refuse a file outside the
platform's limits before anything is published, and carry the visual summary
of every image they publish; their read tools list a post's media with its
links and a download tool keeps one as a file of the run. Every provider API
is served by a fake transport shaped like its documented answers.
"""

from __future__ import annotations

import io
import json
import struct
from email.parser import BytesParser
from email.policy import default as _default_policy

import httpx
import pytest
from PIL import Image

from app.integrations import file_delivery
from app.integrations.providers import bluesky, facebook, linkedin, social_media, x
from app.integrations.providers.social_media import mp4_geometry
from app.llm.base import FileRef
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import FileContext
from tests.integration_file_fakes import (
    HOSTILE_FILE_NAMES,
    RecordingPersist,
    assert_name_only_inside,
    call,
    fake_api,
)


def _png(width: int = 4, height: int = 3, color: tuple[int, int, int] = (15, 188, 255)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, "PNG")
    return buffer.getvalue()


def _box(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(body), kind) + body


def _mp4(
    width: int = 1280,
    height: int = 720,
    seconds: float = 10.0,
    pad: int = 0,
    rotated: bool = False,
    brand: bytes = b"isom",
) -> bytes:
    """An MP4 (a QuickTime movie with ``brand=b"qt  "``) whose ``moov`` says *width* x *height*
    and *seconds*, after *pad* bytes of media."""
    mvhd = b"\x00\x00\x00\x00" + struct.pack(">IIII", 0, 0, 1000, int(seconds * 1000)) + b"\x00" * 80
    if rotated:
        matrix = struct.pack(">9i", 0, 0x10000, 0, -0x10000, 0, 0, 0, 0, 0x40000000)
    else:
        matrix = struct.pack(">9i", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    tkhd = b"\x00\x00\x00\x03" + b"\x00" * 20 + b"\x00" * 16 + matrix + struct.pack(">II", width << 16, height << 16)
    ftyp = _box(b"ftyp", brand + b"\x00\x00\x02\x00isomiso2mp41")
    moov = _box(b"moov", _box(b"mvhd", mvhd) + _box(b"trak", _box(b"tkhd", tkhd)))
    return ftyp + _box(b"mdat", b"\x00" * pad) + moov


def _context(*files: tuple[bytes, str, str]) -> tuple[FileContext, list[str]]:
    refs = [FileRef.from_bytes(data=d, filename=n, content_type=t) for d, n, t in files]
    return FileContext.from_file_refs(refs), [r.file_id for r in refs]


def _executor(builder, token: str, persist=None, ctx: FileContext | None = None):
    executor = _SafeIntegrationExecutor()
    builder(token, executor, persist_file_fn=persist)
    executor.file_context = ctx
    return executor


def _json(request: httpx.Request) -> dict:
    return json.loads(request.content or b"{}")


def _form(request: httpx.Request) -> dict[str, bytes]:
    """The fields of a multipart request: ``name -> raw value``."""
    head = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
    message = BytesParser(policy=_default_policy).parsebytes(head + request.content)
    return {
        part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
        for part in message.iter_parts()
    }


@pytest.fixture(autouse=True)
def _no_waits_and_public_urls(monkeypatch):
    """No processing wait sleeps; the outbound URL policy is recorded, not resolved."""
    checked: list[str] = []

    async def _pause(_seconds):
        return None

    async def _validate(url: str) -> str:
        checked.append(url)
        if "internal" in url:
            raise ValueError("URL targets a private or non-routable address: 10.0.0.1")
        return url

    monkeypatch.setattr(social_media, "pause", _pause)
    monkeypatch.setattr(social_media, "check_public_url", _validate)
    # fetch_file checks a redirect to another origin against the same policy.
    monkeypatch.setattr(file_delivery, "validate_outbound_url_async", _validate)
    return checked


def _refused(answer: str) -> bool:
    """A download refused by its own hop check, or by fetch_file before it follows the redirect."""
    return "refused" in answer or "which is not followed" in answer


#: The real outbound URL policy, kept before the autouse fixture records it.
_REAL_CHECK_PUBLIC_URL = social_media.check_public_url

#: What the fake resolver answers; any other name does not resolve.
_DNS = {
    "bsky.social": "93.184.215.10",
    "plc.directory": "93.184.215.11",
    "pds.example.com": "93.184.215.12",
    "cdn.bsky.app": "93.184.215.13",
    "attacker.example": "93.184.215.14",
    "intranet.example": "10.0.0.7",
    "pds.corp.example": "10.1.2.3",
    "bsky.social.intranet.example": "10.9.9.9",
    "metadata.example": "169.254.169.254",
    "www.linkedin.com": "93.184.215.15",
}


@pytest.fixture
def real_outbound_policy(monkeypatch):
    """The real ``validate_outbound_url`` over a resolver answering :data:`_DNS`."""
    import ipaddress
    import socket
    import types

    from app.core import pinned_request

    def _getaddrinfo(host, *_args, **_kwargs):
        try:
            ip = str(ipaddress.ip_address(host))
        except ValueError:
            ip = _DNS.get(host.lower())
        if ip is None:
            raise socket.gaierror(f"unknown host {host}")
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(
        pinned_request,
        "socket",
        types.SimpleNamespace(getaddrinfo=_getaddrinfo, SOCK_STREAM=socket.SOCK_STREAM, gaierror=socket.gaierror),
    )
    monkeypatch.setattr(social_media, "check_public_url", _REAL_CHECK_PUBLIC_URL)


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


def test_mp4_geometry_reads_the_track_size_and_the_duration():
    assert mp4_geometry(_mp4(1280, 720, 12.5, pad=64)) == (1280, 720, 12.5)
    assert mp4_geometry(_mp4(1920, 1080, 3.0, rotated=True)) == (1080, 1920, 3.0)
    assert mp4_geometry(b"not a video") == (None, None, None)


@pytest.mark.asyncio
async def test_a_media_download_stops_at_its_cap_and_keeps_the_link():
    with fake_api(lambda r: httpx.Response(200, content=b"x" * (2 * 1024 * 1024), headers={"content-type": "video/mp4"})):
        answer = await social_media.fetch_public_media(
            "https://cdn.example.com/v.mp4", source="X", max_bytes=1024 * 1024
        )
    assert isinstance(answer, str)
    assert "over the 1 MB download limit" in answer
    assert "Download URL: https://cdn.example.com/v.mp4" in answer


@pytest.mark.asyncio
async def test_a_media_download_checks_every_redirect_hop(_no_waits_and_public_urls):
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://internal.example.com/secret"})

    with fake_api(responder) as sent:
        answer = await social_media.fetch_public_media("https://cdn.example.com/a.png", source="LinkedIn")
    assert isinstance(answer, str) and answer.startswith("Error")
    assert "private or non-routable address" in answer
    assert [str(r.url) for r in sent] == ["https://cdn.example.com/a.png"]
    assert _no_waits_and_public_urls == ["https://cdn.example.com/a.png", "https://internal.example.com/secret"]


@pytest.mark.asyncio
async def test_a_media_download_refuses_a_redirect_off_https():
    with fake_api(lambda r: httpx.Response(302, headers={"location": "http://cdn.example.com/a.png"})) as sent:
        answer = await social_media.fetch_public_media("https://cdn.example.com/a.png", source="X")
    assert isinstance(answer, str) and answer.startswith("Error") and "HTTPS" in answer
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_a_trusted_origin_skips_the_policy_for_itself_only(_no_waits_and_public_urls):
    """The administrator's PDS is not checked; a hop leaving it is, redirects included."""

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.host == "pds.internal.corp":
            return httpx.Response(302, headers={"location": "https://internal.example.com/latest/meta-data"})
        return httpx.Response(200, content=b"never")

    with fake_api(responder) as sent:
        answer = await social_media.fetch_public_media(
            "https://pds.internal.corp/xrpc/com.atproto.sync.getBlob?cid=b",
            source="Bluesky",
            trusted_origin="https://PDS.internal.corp:443/",
        )
    assert isinstance(answer, str) and answer.startswith("Error")
    assert "private or non-routable address" in answer
    assert [r.url.host for r in sent] == ["pds.internal.corp"]
    assert _no_waits_and_public_urls == ["https://internal.example.com/latest/meta-data"]


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://BSKY.social:443/xrpc", "https://bsky.social"),
        ("https://pds.example.com:8443/", "https://pds.example.com:8443"),
        ("https://[::1]:444/x", "https://[::1]:444"),
        ("https://bsky.social@169.254.169.254/", ""),
        ("https://user:pw@bsky.social/", ""),
        ("http://bsky.social/", ""),
        ("https://bsky.social:99999/", ""),
        ("https://bsky.social:0/", ""),
        ("not a url", ""),
    ],
)
def test_https_origin_compares_an_exact_origin(url, origin):
    assert social_media.https_origin(url) == origin


# ---------------------------------------------------------------------------
# LinkedIn
# ---------------------------------------------------------------------------

_LI = "https://api.linkedin.com"
_LI_UPLOAD = "https://www.linkedin.com/dms-uploads"


def _linkedin(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    path = request.url.path
    if url == f"{_LI}/v2/userinfo":
        return httpx.Response(200, json={"sub": "abc"})
    if path == "/rest/images" and "action=initializeUpload" in url:
        n = len(_linkedin.images)  # type: ignore[attr-defined]
        _linkedin.images.append(n)  # type: ignore[attr-defined]
        return httpx.Response(
            200, json={"value": {"uploadUrl": f"{_LI_UPLOAD}/img{n}", "image": f"urn:li:image:I{n}"}}
        )
    if path == "/rest/documents" and "action=initializeUpload" in url:
        return httpx.Response(200, json={"value": {"uploadUrl": f"{_LI_UPLOAD}/doc", "document": "urn:li:document:D1"}})
    if path == "/rest/videos" and "action=initializeUpload" in url:
        size = _json(request)["initializeUploadRequest"]["fileSizeBytes"]
        half = size // 2
        return httpx.Response(
            200,
            json={
                "value": {
                    "video": "urn:li:video:V1",
                    "uploadToken": "upload-token",
                    "uploadInstructions": [
                        {"uploadUrl": f"{_LI_UPLOAD}/v/p1", "firstByte": half, "lastByte": size - 1},
                        {"uploadUrl": f"{_LI_UPLOAD}/v/p0", "firstByte": 0, "lastByte": half - 1},
                    ],
                }
            },
        )
    if url.startswith(_LI_UPLOAD) and request.method == "PUT":
        return httpx.Response(200, headers={"etag": f'"etag-{path.rsplit("/", 1)[-1]}"'})
    if path == "/rest/videos" and "action=finalizeUpload" in url:
        return httpx.Response(200, json={})
    if path == "/rest/videos/urn:li:video:V1":
        return httpx.Response(200, json={"status": "AVAILABLE"})
    if path == "/rest/posts" and request.method == "POST":
        return httpx.Response(201, headers={"x-restli-id": "urn:li:share:1"})
    if path.startswith("/rest/socialActions/") and path.endswith("/comments") and request.method == "POST":
        return httpx.Response(201, json={})
    return httpx.Response(404, json={"message": f"unexpected {request.method} {url}"})


@pytest.fixture
def linkedin_api():
    _linkedin.images = []  # type: ignore[attr-defined]
    return _linkedin


@pytest.mark.asyncio
async def test_linkedin_publishes_images_by_file_id_as_a_multi_image_post(linkedin_api):
    ctx, ids = _context((_png(), "a.png", "image/png"), (_png(color=(0, 21, 168)), "b.png", "image/png"))
    executor = _executor(linkedin.build_tools, "tok||personal||", ctx=ctx)
    with fake_api(linkedin_api) as sent:
        result = await call(
            executor, "create_linkedin_post", text="Launch", media_file_ids=ids, media_alt_texts=["First", "Second"]
        )
    assert not result.is_error, result.content
    uploads = [r for r in sent if r.method == "PUT"]
    assert [r.content for r in uploads] == [_png(), _png(color=(0, 21, 168))]
    assert all(r.headers["authorization"] == "Bearer tok" for r in uploads)
    init = next(r for r in sent if "action=initializeUpload" in str(r.url))
    assert _json(init)["initializeUploadRequest"]["owner"] == "urn:li:person:abc"
    post = _json(next(r for r in sent if r.url.path == "/rest/posts"))
    assert post["content"] == {
        "multiImage": {
            "images": [{"id": "urn:li:image:I0", "altText": "First"}, {"id": "urn:li:image:I1", "altText": "Second"}]
        }
    }
    assert "Web URL: https://www.linkedin.com/feed/update/urn:li:share:1/" in result.content
    assert f"file {ids[0]}: Visual summary: 4x3 px" in result.content
    assert "a.png" not in result.content
    assert "#0FBCFF" in result.content


@pytest.mark.asyncio
async def test_linkedin_upload_clients_connect_to_a_checked_address(linkedin_api, monkeypatch):
    """An upload URL is an API answer: each upload's client pins its connection."""
    calls: list[dict] = []
    real = linkedin.guarded_client_options

    def spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(linkedin, "guarded_client_options", spy)
    ctx, ids = _context((_png(), "a.png", "image/png"), (_png(color=(0, 21, 168)), "b.png", "image/png"))
    executor = _executor(linkedin.build_tools, "tok||personal||", ctx=ctx)
    with fake_api(linkedin_api) as sent:
        result = await call(executor, "create_linkedin_post", text="Launch", media_file_ids=ids)
    assert not result.is_error, result.content
    assert len([r for r in sent if r.method == "PUT"]) == 2
    assert calls == [{"allow_private": False}, {"allow_private": False}]


@pytest.mark.asyncio
async def test_linkedin_publishes_a_video_through_the_multipart_videos_api(linkedin_api):
    video = _mp4(pad=80 * 1024)
    ctx, ids = _context((video, "demo.mp4", "video/mp4"))
    executor = _executor(linkedin.build_tools, "tok||page||42", ctx=ctx)
    with fake_api(linkedin_api) as sent:
        result = await call(executor, "create_linkedin_post", text="Watch", media_file_ids=ids)
    assert not result.is_error, result.content
    init = _json(next(r for r in sent if "videos?action=initializeUpload" in str(r.url)))
    assert init["initializeUploadRequest"]["owner"] == "urn:li:organization:42"
    assert init["initializeUploadRequest"]["fileSizeBytes"] == len(video)
    parts = sorted((r for r in sent if r.method == "PUT"), key=lambda r: r.url.path)
    assert b"".join(r.content for r in parts) == video
    finalize = _json(next(r for r in sent if "action=finalizeUpload" in str(r.url)))
    assert finalize["finalizeUploadRequest"] == {
        "video": "urn:li:video:V1",
        "uploadToken": "upload-token",
        "uploadedPartIds": ["etag-p0", "etag-p1"],
    }
    post = _json(next(r for r in sent if r.url.path == "/rest/posts"))
    assert post["content"] == {"media": {"id": "urn:li:video:V1", "title": "demo.mp4"}}
    assert post["author"] == "urn:li:organization:42"


@pytest.mark.asyncio
async def test_linkedin_publishes_a_document_post(linkedin_api):
    ctx, ids = _context((b"%PDF-1.4 deck", "deck.pdf", "application/pdf"))
    executor = _executor(linkedin.build_tools, "tok||page||42", ctx=ctx)
    with fake_api(linkedin_api) as sent:
        result = await call(executor, "create_linkedin_post", text="Deck", media_file_ids=ids, media_title="Q3 deck")
    assert not result.is_error, result.content
    post = _json(next(r for r in sent if r.url.path == "/rest/posts"))
    assert post["content"] == {"media": {"id": "urn:li:document:D1", "title": "Q3 deck"}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("files", "extra", "expected"),
    [
        ([(_mp4(pad=80 * 1024, brand=b"qt  "), "clip.mov", "video/quicktime")], {}, "MP4"),
        ([(_mp4(seconds=2.0, pad=80 * 1024), "short.mp4", "video/mp4")], {}, "at least 3 s"),
        ([(_mp4(pad=80 * 1024), "v.mp4", "video/mp4"), (_png(), "a.png", "image/png")], {}, "not 1 video, 1 image"),
        ([(_png(), "a.png", "image/png")], {"article_url": "https://example.com"}, "article OR media"),
        ([(b"PK\x03\x04zip", "archive.zip", "application/zip")], {}, "application/zip"),
    ],
)
async def test_linkedin_refuses_a_file_outside_its_limits_before_publishing(linkedin_api, files, extra, expected):
    ctx, ids = _context(*files)
    executor = _executor(linkedin.build_tools, "tok||page||42", ctx=ctx)
    with fake_api(linkedin_api) as sent:
        result = await call(executor, "create_linkedin_post", text="x", media_file_ids=ids, **extra)
    assert result.is_error
    assert expected in result.content
    assert "Nothing has been published" in result.content
    assert sent == []


_LI_POST = {
    "id": "urn:li:share:7",
    "author": "urn:li:organization:42",
    "commentary": "Our new office",
    "visibility": "PUBLIC",
    "lifecycleState": "PUBLISHED",
    "publishedAt": 1759000000000,
    "content": {
        "multiImage": {"images": [{"id": "urn:li:image:A", "altText": "Front"}, {"id": "urn:li:image:B"}]}
    },
}
_LI_CDN = "https://media.licdn.com/dms/image/A/image-shrink_1280/0/1?e=1&t=sig"


def _linkedin_read(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if request.url.path == "/rest/posts/urn:li:share:7":
        return httpx.Response(200, json=_LI_POST)
    if request.url.path == "/rest/posts" and "q=author" in url:
        return httpx.Response(200, json={"elements": [_LI_POST]})
    if request.url.path == "/rest/images" and "ids=List(" in url:
        return httpx.Response(
            200,
            json={
                "results": {
                    "urn:li:image:A": {"downloadUrl": _LI_CDN, "status": "AVAILABLE"},
                    "urn:li:image:B": {"status": "PROCESSING"},
                }
            },
        )
    if request.url.path == "/rest/images/urn:li:image:A":
        return httpx.Response(200, json={"downloadUrl": _LI_CDN, "status": "AVAILABLE"})
    if url == _LI_CDN:
        return httpx.Response(200, content=_png(), headers={"content-type": "image/png"})
    if "/comments" in request.url.path:
        return httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "actor": "urn:li:person:z",
                        "message": {"text": "Nice"},
                        "content": [{"type": "IMAGE", "entity": {"image": "urn:li:image:A"}}],
                    }
                ]
            },
        )
    return httpx.Response(404, json={})


@pytest.mark.asyncio
async def test_linkedin_reads_a_post_with_its_media_links():
    executor = _executor(linkedin.build_tools, "tok||page||42")
    with fake_api(_linkedin_read):
        post = await call(executor, "get_linkedin_post", post_urn="urn:li:share:7")
        listing = await call(executor, "list_linkedin_posts")
        comments = await call(executor, "get_linkedin_comments", post_urn="urn:li:share:7")
    for result in (post, listing):
        assert not result.is_error, result.content
        assert "Media 1: image urn:li:image:A - alt: Front - Download URL: " + _LI_CDN in result.content
        assert "Media 2: image urn:li:image:B - status: PROCESSING - no download link" in result.content
        assert "https://www.linkedin.com/feed/update/urn:li:share:7/" in result.content
        assert "download_linkedin_media" in result.content
    assert f"image: urn:li:image:A - Download URL: {_LI_CDN}" in comments.content


@pytest.mark.asyncio
async def test_linkedin_downloads_a_media_without_the_token():
    persist = RecordingPersist()
    executor = _executor(linkedin.build_tools, "tok||page||42", persist)
    with fake_api(_linkedin_read) as sent:
        result = await call(executor, "download_linkedin_media", media_urn="urn:li:image:A")
    assert not result.is_error, result.content
    assert persist.calls == [("linkedin-image-A.png", _png(), "image/png")]
    (ref,) = result.files
    assert f"[[FILE:{ref.file_id}]]" in result.content
    assert f"Download URL: {_LI_CDN}" in result.content
    cdn = next(r for r in sent if str(r.url) == _LI_CDN)
    assert "authorization" not in cdn.headers


@pytest.mark.asyncio
async def test_linkedin_comments_with_an_image_of_the_run(linkedin_api):
    ctx, ids = _context((_png(), "shot.png", "image/png"))
    executor = _executor(linkedin.build_tools, "tok||page||42", ctx=ctx)
    with fake_api(linkedin_api) as sent:
        result = await call(
            executor, "create_linkedin_comment", post_urn="urn:li:share:7", text="See", image_file_id=ids[0]
        )
    assert not result.is_error, result.content
    comment = _json(next(r for r in sent if r.url.path.endswith("/comments")))
    assert comment["content"] == [{"entity": {"image": "urn:li:image:I0"}}]
    assert comment["actor"] == "urn:li:organization:42"
    assert "Visual summary" in result.content


# ---------------------------------------------------------------------------
# X
# ---------------------------------------------------------------------------

_X = "https://api.x.com/2"


def _x(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    path = request.url.path
    if path == "/2/media/upload/initialize":
        return httpx.Response(200, json={"data": {"id": "111", "media_key": "3_111"}})
    if path == "/2/media/upload/111/append":
        return httpx.Response(200, json={"data": {}})
    if path == "/2/media/upload/111/finalize":
        category = _x.category  # type: ignore[attr-defined]
        if category == "video":
            return httpx.Response(
                200, json={"data": {"id": "111", "processing_info": {"state": "pending", "check_after_secs": 1}}}
            )
        return httpx.Response(200, json={"data": {"id": "111"}})
    if path == "/2/media/upload" and "command=STATUS" in url:
        return httpx.Response(200, json={"data": {"id": "111", "processing_info": {"state": "succeeded"}}})
    if path == "/2/media/metadata":
        return httpx.Response(200, json={"data": {"id": "111"}})
    if path == "/2/tweets" and request.method == "POST":
        return httpx.Response(201, json={"data": {"id": "999", "text": "hi"}})
    if path.startswith("/2/dm_conversations/with/"):
        return httpx.Response(201, json={"data": {"dm_event_id": "42"}})
    return httpx.Response(404, json={"title": "Not Found"})


@pytest.mark.asyncio
async def test_x_tweets_an_image_through_the_chunked_v2_upload():
    _x.category = "image"  # type: ignore[attr-defined]
    ctx, ids = _context((_png(), "chart.png", "image/png"))
    executor = _executor(x.build_tools, "xtok", ctx=ctx)
    with fake_api(_x) as sent:
        result = await call(executor, "create_x_tweet", text="Q3", media_file_ids=ids, media_alt_texts="Revenue chart")
    assert not result.is_error, result.content
    init = _json(next(r for r in sent if r.url.path.endswith("/initialize")))
    assert init == {"media_type": "image/png", "total_bytes": len(_png()), "media_category": "tweet_image"}
    append = next(r for r in sent if r.url.path.endswith("/append"))
    assert append.headers["content-type"].startswith("multipart/form-data")
    assert _form(append) == {"segment_index": b"0", "media": _png()}
    metadata = _json(next(r for r in sent if r.url.path == "/2/media/metadata"))
    assert metadata == {"id": "111", "metadata": {"alt_text": {"text": "Revenue chart"}}}
    tweet = _json(next(r for r in sent if r.url.path == "/2/tweets"))
    assert tweet == {"text": "Q3", "media": {"media_ids": ["111"]}}
    assert "https://x.com/i/status/999" in result.content
    assert f"file {ids[0]}: Visual summary: 4x3 px" in result.content
    assert "chart.png" not in result.content


@pytest.mark.asyncio
async def test_x_waits_for_a_video_to_be_processed_before_tweeting(monkeypatch):
    _x.category = "video"  # type: ignore[attr-defined]
    monkeypatch.setattr(x, "_CHUNK_BYTES", 1024)
    video = _mp4(pad=2500)
    ctx, ids = _context((video, "clip.mp4", "video/mp4"))
    executor = _executor(x.build_tools, "xtok", ctx=ctx)
    with fake_api(_x) as sent:
        result = await call(executor, "create_x_tweet", text="Clip", media_file_ids=ids)
    assert not result.is_error, result.content
    assert _json(next(r for r in sent if r.url.path.endswith("/initialize")))["media_category"] == "tweet_video"
    segments = [_form(r) for r in sent if r.url.path.endswith("/append")]
    assert [s["segment_index"] for s in segments] == [str(i).encode() for i in range(len(segments))]
    assert b"".join(s["media"] for s in segments) == video
    paths = [r.url.path for r in sent]
    assert paths.index("/2/media/upload") < paths.index("/2/tweets")
    assert not any(r.url.path == "/2/media/metadata" for r in sent)


@pytest.mark.asyncio
async def test_x_upload_segments_carry_the_token_refreshed_mid_upload(monkeypatch):
    _x.category = "video"  # type: ignore[attr-defined]
    monkeypatch.setattr(x, "_CHUNK_BYTES", 1024)
    video = _mp4(pad=2500)
    ctx, ids = _context((video, "clip.mp4", "video/mp4"))
    refused: list[str] = []

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/append") and not refused:
            refused.append(request.headers["authorization"])
            return httpx.Response(401, json={"title": "Unauthorized"})
        return _x(request)

    async def refresh() -> str:
        return "fresh"

    executor = _SafeIntegrationExecutor()
    x.build_tools("stale", executor, refresh_fn=refresh)
    executor.file_context = ctx
    with fake_api(responder) as sent:
        result = await call(executor, "create_x_tweet", text="Clip", media_file_ids=ids)
    assert not result.is_error, result.content
    assert refused == ["Bearer stale"]
    later = [r for r in sent if r.url.path.endswith(("/append", "/finalize"))][1:]
    assert later and all(r.headers["authorization"] == "Bearer fresh" for r in later)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ([(_png(), f"{i}.png", "image/png") for i in range(5)], "5 files given"),
        ([(b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024 + 1), "big.png", "image/png")], "over the 5 MB"),
        ([(_mp4(seconds=0.2), "blink.mp4", "video/mp4")], "at least 0.5 s"),
        ([(_mp4(), "v.mp4", "video/mp4"), (_png(), "a.png", "image/png")], "not 1 video, 1 image"),
    ],
)
async def test_x_refuses_media_outside_its_limits_before_uploading(files, expected):
    ctx, ids = _context(*files)
    executor = _executor(x.build_tools, "xtok", ctx=ctx)
    with fake_api(_x) as sent:
        result = await call(executor, "create_x_tweet", text="x", media_file_ids=ids)
    assert result.is_error
    assert expected in result.content
    assert sent == []


@pytest.mark.asyncio
async def test_x_says_the_media_write_scope_is_missing():
    ctx, ids = _context((_png(), "a.png", "image/png"))
    executor = _executor(x.build_tools, "xtok", ctx=ctx)
    with fake_api(lambda r: httpx.Response(403, json={"title": "Forbidden"})) as sent:
        result = await call(executor, "create_x_tweet", text="x", media_file_ids=ids)
    assert result.is_error
    assert "media.write" in result.content
    assert not any(r.url.path == "/2/tweets" for r in sent)


def test_x_asks_for_the_media_write_scope():
    assert "media.write" in x.XIntegration().get_oauth_defaults().scopes


@pytest.mark.asyncio
async def test_x_sends_a_dm_with_a_media_of_the_run():
    _x.category = "image"  # type: ignore[attr-defined]
    ctx, ids = _context((_png(), "a.png", "image/png"))
    executor = _executor(x.build_tools, "xtok", ctx=ctx)
    with fake_api(_x) as sent:
        result = await call(executor, "send_x_dm", participant_id="77", text="Here", media_file_id=ids[0])
    assert not result.is_error, result.content
    assert _json(next(r for r in sent if r.url.path.endswith("/initialize")))["media_category"] == "dm_image"
    dm = _json(next(r for r in sent if r.url.path.startswith("/2/dm_conversations")))
    assert dm == {"text": "Here", "attachments": [{"media_id": "111"}]}


_X_PHOTO = "https://pbs.twimg.com/media/Ez1.jpg"
_X_VIDEO_HI = "https://video.twimg.com/ext_tw_video/1/vid/1280x720/hi.mp4"
_X_TWEET = {
    "data": {"id": "500", "text": "Look", "author_id": "1", "attachments": {"media_keys": ["3_1", "7_2"]}},
    "includes": {
        "users": [{"id": "1", "username": "acme"}],
        "media": [
            {"media_key": "3_1", "type": "photo", "url": _X_PHOTO, "width": 800, "height": 600, "alt_text": "Office"},
            {
                "media_key": "7_2",
                "type": "video",
                "preview_image_url": "https://pbs.twimg.com/thumb.jpg",
                "duration_ms": 9000,
                "variants": [
                    {"content_type": "application/x-mpegURL", "url": "https://video.twimg.com/pl.m3u8"},
                    {"content_type": "video/mp4", "bit_rate": 256000, "url": "https://video.twimg.com/lo.mp4"},
                    {"content_type": "video/mp4", "bit_rate": 2176000, "url": _X_VIDEO_HI},
                ],
            },
        ],
    },
}


def _x_read(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/2/tweets/500":
        return httpx.Response(200, json=_X_TWEET)
    if str(request.url) == _X_VIDEO_HI:
        return httpx.Response(200, content=_mp4(), headers={"content-type": "video/mp4"})
    return httpx.Response(404, json={})


@pytest.mark.asyncio
async def test_x_reads_a_tweet_with_its_media_and_downloads_one():
    persist = RecordingPersist()
    executor = _executor(x.build_tools, "xtok", persist)
    with fake_api(_x_read) as sent:
        tweet = await call(executor, "get_x_tweet", tweet_id="500")
        several = await call(executor, "download_x_media", tweet_id="500")
        video = await call(executor, "download_x_media", tweet_id="500", media_key="7_2")
    assert f"Media: photo (media_key 3_1) - 800x600 - Download URL: {_X_PHOTO}" in tweet.content
    assert f"Download URL: {_X_VIDEO_HI}" in tweet.content
    assert "download_x_media" in tweet.content
    read = next(r for r in sent if r.url.path == "/2/tweets/500")
    assert "attachments.media_keys" in read.url.params["expansions"]
    assert several.is_error and "3_1, 7_2" in several.content
    assert not video.is_error, video.content
    assert persist.calls == [("x-500-7_2.mp4", _mp4(), "video/mp4")]
    assert "Web URL: https://x.com/i/status/500" in video.content
    cdn = next(r for r in sent if str(r.url) == _X_VIDEO_HI)
    assert "authorization" not in cdn.headers


# ---------------------------------------------------------------------------
# Facebook
# ---------------------------------------------------------------------------

_FB = "https://graph.facebook.com/v21.0"
_FB_TOKEN = "utok||page||PAGE1||ptok"


def _facebook(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url == f"{_FB}/PAGE1/photos":
        fields = _form(request)
        if fields.get("published") == b"false":
            _facebook.photos.append(fields)  # type: ignore[attr-defined]
            return httpx.Response(200, json={"id": f"PH{len(_facebook.photos)}"})  # type: ignore[attr-defined]
        return httpx.Response(200, json={"id": "PH0", "post_id": "PAGE1_55"})
    if url == f"{_FB}/PAGE1/feed":
        return httpx.Response(200, json={"id": "PAGE1_66"})
    if url == "https://graph-video.facebook.com/v21.0/PAGE1/videos":
        return httpx.Response(200, json={"id": "V9"})
    if url == f"{_FB}/POST1/comments":
        return httpx.Response(200, json={"id": "C1"})
    return httpx.Response(404, json={"error": {"message": "unexpected"}})


@pytest.fixture
def facebook_api():
    _facebook.photos = []  # type: ignore[attr-defined]
    return _facebook


@pytest.mark.asyncio
async def test_facebook_publishes_one_photo_with_its_caption(facebook_api):
    ctx, ids = _context((_png(), "a.png", "image/png"))
    executor = _executor(facebook.build_tools, _FB_TOKEN, ctx=ctx)
    with fake_api(facebook_api) as sent:
        result = await call(
            executor, "create_facebook_post", message="Hello", media_file_ids=ids, media_alt_texts=["Team"]
        )
    assert not result.is_error, result.content
    fields = _form(next(r for r in sent if r.url.path.endswith("/photos")))
    assert fields["caption"] == b"Hello"
    assert fields["access_token"] == b"ptok"
    assert fields["alt_text_custom"] == b"Team"
    assert fields["source"] == _png()
    assert "Post ID: PAGE1_55" in result.content
    assert "Visual summary" in result.content


@pytest.mark.asyncio
async def test_facebook_publishes_several_photos_as_one_post(facebook_api):
    ctx, ids = _context((_png(), "a.png", "image/png"), (_png(color=(1, 2, 3)), "b.png", "image/png"))
    executor = _executor(facebook.build_tools, _FB_TOKEN, ctx=ctx)
    with fake_api(facebook_api) as sent:
        result = await call(executor, "create_facebook_post", message="Album", media_file_ids=ids)
    assert not result.is_error, result.content
    assert len(facebook_api.photos) == 2
    feed = dict(httpx.QueryParams(next(r for r in sent if r.url.path.endswith("/feed")).content.decode()))
    assert feed["message"] == "Album"
    assert json.loads(feed["attached_media[0]"]) == {"media_fbid": "PH1"}
    assert json.loads(feed["attached_media[1]"]) == {"media_fbid": "PH2"}
    assert "Post ID: PAGE1_66" in result.content


@pytest.mark.asyncio
async def test_facebook_says_which_unpublished_photos_a_failed_post_leaves(facebook_api):
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/feed"):
            return httpx.Response(400, json={"error": {"message": "Invalid parameter"}})
        return facebook_api(request)

    ctx, ids = _context((_png(), "a.png", "image/png"), (_png(color=(1, 2, 3)), "b.png", "image/png"))
    executor = _executor(facebook.build_tools, _FB_TOKEN, ctx=ctx)
    with fake_api(responder):
        result = await call(executor, "create_facebook_post", message="Album", media_file_ids=ids)
    assert result.is_error
    assert "Nothing has been published: 2 photo(s) were uploaded unpublished (IDs PH1, PH2)" in result.content
    assert "within 24 hours" in result.content


@pytest.mark.asyncio
async def test_facebook_publishes_a_video_on_graph_video(facebook_api):
    ctx, ids = _context((_mp4(), "clip.mp4", "video/mp4"))
    executor = _executor(facebook.build_tools, _FB_TOKEN, ctx=ctx)
    with fake_api(facebook_api) as sent:
        result = await call(
            executor, "create_facebook_post", message="Watch", media_file_ids=ids, media_title="Demo"
        )
    assert not result.is_error, result.content
    fields = _form(next(r for r in sent if r.url.host == "graph-video.facebook.com"))
    assert fields["description"] == b"Watch"
    assert fields["title"] == b"Demo"
    assert fields["source"] == _mp4()
    assert "https://www.facebook.com/PAGE1/videos/V9" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("token", "extra", "files", "expected"),
    [
        ("utok||personal||||", {}, [(_png(), "a.png", "image/png")], "Page only"),
        (_FB_TOKEN, {"link": "https://example.com"}, [(_png(), "a.png", "image/png")], "link OR photos"),
        (_FB_TOKEN, {}, [(b"RIFF\x00\x00\x00\x00WEBPVP8 ", "a.webp", "image/webp")], "image/webp"),
    ],
)
async def test_facebook_refuses_media_it_cannot_publish(facebook_api, token, extra, files, expected):
    ctx, ids = _context(*files)
    executor = _executor(facebook.build_tools, token, ctx=ctx)
    with fake_api(facebook_api) as sent:
        result = await call(executor, "create_facebook_post", message="x", media_file_ids=ids, **extra)
    assert result.is_error
    assert expected in result.content
    assert sent == []


_FB_PHOTO = "https://scontent.xx.fbcdn.net/v/p.jpg?oh=sig"
_FB_VIDEO = "https://video.xx.fbcdn.net/v/v.mp4?oh=sig"
_FB_POST = {
    "id": "PAGE1_5",
    "message": "Event",
    "created_time": "2026-09-01T10:00:00+0000",
    "permalink_url": "https://www.facebook.com/PAGE1/posts/5",
    "attachments": {
        "data": [
            {
                "type": "album",
                "subattachments": {
                    "data": [
                        {"type": "photo", "media": {"image": {"src": _FB_PHOTO}}, "url": "https://www.facebook.com/photo/1"},
                        {
                            "type": "video_inline",
                            "media": {"image": {"src": "https://scontent.xx.fbcdn.net/poster.jpg"}, "source": _FB_VIDEO},
                            "url": "https://www.facebook.com/video/2",
                        },
                    ]
                },
            }
        ]
    },
}


def _facebook_read(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if request.url.path == "/v21.0/PAGE1_5":
        return httpx.Response(200, json=_FB_POST)
    if request.url.path == "/v21.0/PAGE1_5/comments":
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "PAGE1_5_9",
                        "message": "Nice",
                        "from": {"name": "Ana"},
                        "attachment": {"type": "photo", "media": {"image": {"src": _FB_PHOTO}}},
                    }
                ]
            },
        )
    if url == _FB_VIDEO:
        return httpx.Response(200, content=_mp4(), headers={"content-type": "video/mp4"})
    return httpx.Response(404, json={})


@pytest.mark.asyncio
async def test_facebook_reads_a_post_with_its_media_and_downloads_one():
    persist = RecordingPersist()
    executor = _executor(facebook.build_tools, _FB_TOKEN, persist)
    with fake_api(_facebook_read) as sent:
        post = await call(executor, "get_facebook_post", post_id="PAGE1_5")
        comments = await call(executor, "get_facebook_comments", post_id="PAGE1_5")
        video = await call(executor, "download_facebook_media", post_id="PAGE1_5", index=2)
    assert f"Media 1: photo - image: {_FB_PHOTO}" in post.content
    assert f"Media 2: video_inline - video: {_FB_VIDEO}" in post.content
    assert "download_facebook_media" in post.content
    read = next(r for r in sent if r.url.path == "/v21.0/PAGE1_5")
    assert "attachments{" in read.url.params["fields"]
    assert "comment ID: PAGE1_5_9" in comments.content and _FB_PHOTO in comments.content
    assert not video.is_error, video.content
    assert persist.calls == [("facebook-PAGE1_5-2.mp4", _mp4(), "video/mp4")]
    cdn = next(r for r in sent if str(r.url) == _FB_VIDEO)
    assert "access_token" not in str(cdn.url)


@pytest.mark.asyncio
async def test_facebook_comments_with_a_photo_of_the_run(facebook_api):
    ctx, ids = _context((_png(), "a.png", "image/png"))
    executor = _executor(facebook.build_tools, _FB_TOKEN, ctx=ctx)
    with fake_api(facebook_api) as sent:
        result = await call(executor, "create_facebook_comment", post_id="POST1", message="See", image_file_id=ids[0])
    assert not result.is_error, result.content
    fields = _form(next(r for r in sent if r.url.path.endswith("/comments")))
    assert fields["message"] == b"See"
    assert fields["source"] == _png()


# ---------------------------------------------------------------------------
# Bluesky
# ---------------------------------------------------------------------------

_BSKY = "https://bsky.social/xrpc"
_IMG_BLOB = {"$type": "blob", "ref": {"$link": "bafyimg"}, "mimeType": "image/png", "size": 70}
_VID_BLOB = {"$type": "blob", "ref": {"$link": "bafyvid"}, "mimeType": "video/mp4", "size": 400}


def _bluesky(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    path = request.url.path
    if path == "/xrpc/com.atproto.server.createSession":
        return httpx.Response(200, json={"accessJwt": "jwt", "did": "did:plc:me", "handle": "me.bsky.social"})
    if path == "/xrpc/com.atproto.repo.uploadBlob":
        blob = _VID_BLOB if request.headers["content-type"] == "video/mp4" else _IMG_BLOB
        return httpx.Response(200, json={"blob": blob})
    if path == "/xrpc/com.atproto.repo.createRecord":
        return httpx.Response(200, json={"uri": "at://did:plc:me/app.bsky.feed.post/3abc", "cid": "bafypost"})
    if path == "/xrpc/app.bsky.feed.getPosts":
        uri = request.url.params["uris"]
        did = uri.split("/")[2]
        embed = (
            {"$type": "app.bsky.embed.video", "video": _VID_BLOB}
            if did == "did:plc:other"
            else {"$type": "app.bsky.embed.images", "images": [{"alt": "Chart", "image": _IMG_BLOB}]}
        )
        return httpx.Response(
            200,
            json={
                "posts": [
                    {
                        "uri": uri,
                        "cid": "bafypost",
                        "author": {"did": did, "handle": "someone"},
                        "record": {"text": "hi", "embed": embed},
                        "embed": {"$type": "app.bsky.embed.video#view", "playlist": "https://video.bsky.app/pl.m3u8"},
                    }
                ]
            },
        )
    if url == "https://plc.directory/did:plc:other":
        return httpx.Response(
            200,
            json={
                "id": "did:plc:other",
                "service": [
                    {"id": "#atproto_pds", "type": "AtprotoPersonalDataServer", "serviceEndpoint": "https://pds.example.com"}
                ],
            },
        )
    if path == "/xrpc/com.atproto.sync.getBlob":
        body = _mp4() if request.url.params["cid"] == "bafyvid" else _png()
        return httpx.Response(200, content=body, headers={"content-type": "application/octet-stream"})
    return httpx.Response(404, json={"error": "NotFound"})


@pytest.mark.asyncio
async def test_bluesky_posts_images_as_blobs_with_alt_and_aspect_ratio():
    ctx, ids = _context((_png(), "a.png", "image/png"), (_png(8, 2), "b.png", "image/png"))
    executor = _executor(bluesky.build_tools, "me.bsky.social||pw", ctx=ctx)
    with fake_api(_bluesky) as sent:
        result = await call(
            executor, "create_bluesky_post", text="Charts", media_file_ids=ids, media_alt_texts=["Left", "Right"]
        )
    assert not result.is_error, result.content
    uploads = [r for r in sent if r.url.path.endswith("uploadBlob")]
    assert [r.content for r in uploads] == [_png(), _png(8, 2)]
    assert all(r.headers["content-type"] == "image/png" for r in uploads)
    record = _json(next(r for r in sent if r.url.path.endswith("createRecord")))["record"]
    assert record["embed"] == {
        "$type": "app.bsky.embed.images",
        "images": [
            {"alt": "Left", "image": _IMG_BLOB, "aspectRatio": {"width": 4, "height": 3}},
            {"alt": "Right", "image": _IMG_BLOB, "aspectRatio": {"width": 8, "height": 2}},
        ],
    }
    assert "https://bsky.app/profile/did:plc:me/post/3abc" in result.content
    assert f"file {ids[1]}: Visual summary: 8x2 px" in result.content
    assert "b.png" not in result.content


@pytest.mark.asyncio
async def test_bluesky_posts_an_mp4_video_embed():
    ctx, ids = _context((_mp4(1080, 1920), "reel.mp4", "video/mp4"))
    executor = _executor(bluesky.build_tools, "me.bsky.social||pw", ctx=ctx)
    with fake_api(_bluesky) as sent:
        result = await call(executor, "create_bluesky_post", text="Reel", media_file_ids=ids)
    assert not result.is_error, result.content
    record = _json(next(r for r in sent if r.url.path.endswith("createRecord")))["record"]
    assert record["embed"] == {
        "$type": "app.bsky.embed.video",
        "video": _VID_BLOB,
        "aspectRatio": {"width": 1080, "height": 1920},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ([(b"\x89PNG\r\n\x1a\n" + b"\x00" * 2_000_000, "big.png", "image/png")], "2 MB (2,000,000 bytes)"),
        ([(_mp4(brand=b"qt  "), "clip.mov", "video/quicktime")], "MP4"),
        ([(_png(), f"{i}.png", "image/png") for i in range(5)], "5 files given"),
    ],
)
async def test_bluesky_refuses_media_outside_its_lexicon_limits(files, expected):
    ctx, ids = _context(*files)
    executor = _executor(bluesky.build_tools, "me.bsky.social||pw", ctx=ctx)
    with fake_api(_bluesky) as sent:
        result = await call(executor, "create_bluesky_post", text="x", media_file_ids=ids)
    assert result.is_error
    assert expected in result.content
    assert sent == []


def test_bluesky_reads_list_the_media_and_the_cid():
    post = {
        "uri": "at://did:plc:me/app.bsky.feed.post/3abc",
        "cid": "bafypost",
        "author": {"did": "did:plc:me", "handle": "me"},
        "record": {"text": "hi"},
        "embed": {
            "$type": "app.bsky.embed.recordWithMedia#view",
            "media": {
                "$type": "app.bsky.embed.images#view",
                "images": [{"fullsize": "https://cdn.bsky.app/img/full.jpg", "thumb": "t", "alt": "Chart"}],
            },
        },
    }
    text = bluesky._format_bsky_post(post)
    assert "Media 1: image - https://cdn.bsky.app/img/full.jpg (alt: Chart)" in text
    assert "CID: bafypost" in text
    assert "URL: https://bsky.app/profile/did:plc:me/post/3abc" in text


@pytest.mark.asyncio
async def test_bluesky_downloads_the_original_blob_from_the_authors_pds(_no_waits_and_public_urls):
    persist = RecordingPersist()
    executor = _executor(bluesky.build_tools, "me.bsky.social||pw", persist)
    with fake_api(_bluesky) as sent:
        mine = await call(executor, "download_bluesky_media", post_uri="at://did:plc:me/app.bsky.feed.post/3abc")
        theirs = await call(executor, "download_bluesky_media", post_uri="at://did:plc:other/app.bsky.feed.post/9z")
    assert not mine.is_error, mine.content
    assert not theirs.is_error, theirs.content
    assert persist.calls == [
        ("bluesky-3abc-1.png", _png(), "image/png"),
        ("bluesky-9z-1.mp4", _mp4(), "video/mp4"),
    ]
    blobs = [str(r.url) for r in sent if r.url.path.endswith("getBlob")]
    assert blobs == [
        "https://bsky.social/xrpc/com.atproto.sync.getBlob?did=did:plc:me&cid=bafyimg",
        "https://pds.example.com/xrpc/com.atproto.sync.getBlob?did=did:plc:other&cid=bafyvid",
    ]
    # Another author's PDS is a public host like any other: the outbound
    # policy is applied to it (and to its DID document), never to the
    # administrator-configured PDS.
    assert "https://plc.directory/did:plc:other" in _no_waits_and_public_urls
    assert blobs[1] in _no_waits_and_public_urls
    assert blobs[0] not in _no_waits_and_public_urls
    assert "Web URL: https://bsky.app/profile/did:plc:other/post/9z" in theirs.content


# ---------------------------------------------------------------------------
# The outbound URL policy on every request of a media download
# ---------------------------------------------------------------------------


def _pds_document(did: str, endpoint: str) -> dict:
    return {
        "id": did,
        "service": [{"id": "#atproto_pds", "type": "AtprotoPersonalDataServer", "serviceEndpoint": endpoint}],
    }


def _bluesky_author(did: str, document: dict | httpx.Response | None = None, blob: httpx.Response | None = None):
    """The Bluesky API for the session ``did:plc:me`` and one video post by *did*."""

    def responder(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/xrpc/com.atproto.server.createSession":
            return httpx.Response(200, json={"accessJwt": "jwt", "did": "did:plc:me", "handle": "me.bsky.social"})
        if path == "/xrpc/app.bsky.feed.getPosts":
            post = {
                "uri": f"at://{did}/app.bsky.feed.post/9z",
                "author": {"did": did, "handle": "someone"},
                "record": {"text": "hi", "embed": {"$type": "app.bsky.embed.video", "video": _VID_BLOB}},
            }
            return httpx.Response(200, json={"posts": [post]})
        if request.url.host == "plc.directory" or path == "/.well-known/did.json":
            return document if isinstance(document, httpx.Response) else httpx.Response(200, json=document)
        if path == "/xrpc/com.atproto.sync.getBlob" or request.url.host == "cdn.bsky.app":
            return blob or httpx.Response(200, content=_mp4(), headers={"content-type": "video/mp4"})
        return httpx.Response(404, json={"error": "NotFound"})

    return responder


async def _download_bluesky(responder, did: str = "did:plc:other"):
    persist = RecordingPersist()
    executor = _executor(bluesky.build_tools, "me.bsky.social||pw", persist)
    with fake_api(responder) as sent:
        result = await call(executor, "download_bluesky_media", post_uri=f"at://{did}/app.bsky.feed.post/9z")
    return result, {r.url.host for r in sent}, persist


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1",
        "https://10.0.0.5:8443",
        "https://192.168.1.20/",
        "https://100.64.0.1",
        "https://169.254.169.254",
        "https://[::1]",
        "https://[fe80::1]",
        "https://[::ffff:169.254.169.254]",
        "https://pds.corp.example",
        "https://metadata.example",
    ],
)
async def test_bluesky_refuses_a_did_document_naming_an_internal_pds(real_outbound_policy, endpoint):
    responder = _bluesky_author("did:plc:other", _pds_document("did:plc:other", endpoint))
    result, hosts, persist = await _download_bluesky(responder)
    assert "refused" in result.content and "private or non-routable" in result.content
    assert hosts == {"bsky.social", "plc.directory"}
    assert persist.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("did", ["did:web:intranet.example", "did:web:metadata.example", "did:web:intranet.example%3A8443"])
async def test_bluesky_never_requests_the_did_document_of_an_internal_did_web_host(real_outbound_policy, did):
    responder = _bluesky_author(did, _pds_document(did, "https://pds.example.com"))
    result, hosts, persist = await _download_bluesky(responder, did)
    assert "refused" in result.content and "DID document" in result.content
    assert hosts == {"bsky.social"}
    assert persist.calls == []


@pytest.mark.asyncio
async def test_bluesky_refuses_the_internal_pds_a_public_did_web_names(real_outbound_policy):
    did = "did:web:attacker.example"
    result, hosts, persist = await _download_bluesky(
        _bluesky_author(did, _pds_document(did, "https://169.254.169.254")), did
    )
    assert "refused" in result.content
    assert hosts == {"bsky.social", "attacker.example"}
    assert persist.calls == []


@pytest.mark.parametrize(
    ("did", "url"),
    [
        ("did:plc:ewvi7nxzyoun6zhxrhs64oiz", "https://plc.directory/did:plc:ewvi7nxzyoun6zhxrhs64oiz"),
        ("did:web:example.com", "https://example.com/.well-known/did.json"),
        ("did:web:Example.COM%3A8443", "https://example.com:8443/.well-known/did.json"),
        ("did:web:127.0.0.1", None),
        ("did:web:169.254.169.254", None),
        ("did:web:%5B%3A%3A1%5D", None),
        ("did:web:localhost%3A8080", None),
        ("did:web:example.com:user:alice", None),
        ("did:web:evil.example%2F%40127.0.0.1", None),
        ("did:web:example.com%3A0", None),
        ("did:key:example", None),
    ],
)
def test_bluesky_resolves_only_the_dids_atproto_accepts(did, url):
    assert bluesky.did_document_url(did) == url


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("https://bsky.social@169.254.169.254", "names no HTTPS PDS"),
        ("https://169.254.169.254/#bsky.social", "names no HTTPS PDS"),
        ("https://169.254.169.254/xrpc?pds=https://bsky.social", "names no HTTPS PDS"),
        ("https://169.254.169.254/bsky.social", "names no HTTPS PDS"),
        ("http://bsky.social", "names no HTTPS PDS"),
        ("https://bsky.social.intranet.example", "private or non-routable"),
    ],
)
async def test_bluesky_never_takes_a_foreign_pds_for_the_configured_one(real_outbound_policy, endpoint, expected):
    """Only the configured PDS origin skips the policy, compared exactly: no spelling of another host passes."""
    result, hosts, persist = await _download_bluesky(
        _bluesky_author("did:plc:other", _pds_document("did:plc:other", endpoint))
    )
    assert expected in result.content
    assert hosts == {"bsky.social", "plc.directory"}
    assert persist.calls == []


@pytest.mark.asyncio
async def test_bluesky_refuses_the_did_document_of_another_did(real_outbound_policy):
    responder = _bluesky_author("did:plc:other", _pds_document("did:plc:victim", "https://pds.example.com"))
    result, hosts, _ = await _download_bluesky(responder)
    assert "is not its DID document" in result.content
    assert hosts == {"bsky.social", "plc.directory"}


@pytest.mark.asyncio
async def test_bluesky_checks_every_hop_of_the_did_document(real_outbound_policy):
    document = httpx.Response(302, headers={"location": "https://10.0.0.8/did.json"})
    result, hosts, _ = await _download_bluesky(_bluesky_author("did:plc:other", document))
    assert "refused" in result.content
    assert hosts == {"bsky.social", "plc.directory"}


@pytest.mark.asyncio
async def test_bluesky_checks_a_redirect_off_the_configured_pds(real_outbound_policy):
    """The session's own blob is read from the configured PDS unchecked, but where it redirects is checked."""
    blob = httpx.Response(302, headers={"location": "https://169.254.169.254/latest/meta-data/"})
    result, hosts, persist = await _download_bluesky(_bluesky_author("did:plc:me", blob=blob), "did:plc:me")
    assert "refused" in result.content
    assert hosts == {"bsky.social"}
    assert persist.calls == []


@pytest.mark.asyncio
async def test_bluesky_downloads_from_a_public_pds_under_the_real_policy(real_outbound_policy):
    base = _bluesky_author("did:plc:other", _pds_document("did:plc:other", "https://pds.example.com/"))

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.host == "pds.example.com":
            return httpx.Response(302, headers={"location": "https://cdn.bsky.app/blob/bafyvid"})
        return base(request)

    result, hosts, persist = await _download_bluesky(responder)
    assert not result.is_error, result.content
    assert persist.calls == [("bluesky-9z-1.mp4", _mp4(), "video/mp4")]
    assert hosts == {"bsky.social", "plc.directory", "pds.example.com", "cdn.bsky.app"}


@pytest.mark.asyncio
async def test_linkedin_refuses_an_upload_url_off_the_public_internet(real_outbound_policy, linkedin_api):
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/images" and "action=initializeUpload" in str(request.url):
            return httpx.Response(
                200, json={"value": {"uploadUrl": "https://169.254.169.254/upload", "image": "urn:li:image:I0"}}
            )
        return linkedin_api(request)

    ctx, ids = _context((_png(), "a.png", "image/png"))
    executor = _executor(linkedin.build_tools, "tok||personal||", ctx=ctx)
    with fake_api(responder) as sent:
        result = await call(executor, "create_linkedin_post", text="Hi", media_file_ids=ids)
    assert "refused" in result.content and "private or non-routable" in result.content
    assert all(r.url.host != "169.254.169.254" for r in sent)
    assert not any(r.url.path == "/rest/posts" for r in sent)

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hops", "checked"),
    [
        (["https://cdn.example.com/a", "https://pds.internal.corp/b"], ["https://cdn.example.com/a", "https://pds.internal.corp/b"]),
        (
            ["https://pds.internal.corp/a", "https://cdn.example.com/b", "https://pds.internal.corp/c"],
            ["https://cdn.example.com/b", "https://pds.internal.corp/c"],
        ),
    ],
)
async def test_a_redirect_back_to_the_trusted_origin_is_checked(_no_waits_and_public_urls, hops, checked):
    """Trust holds only while every request went to the trusted origin: a chain that began or went elsewhere is checked."""

    def responder(request: httpx.Request) -> httpx.Response:
        position = hops.index(str(request.url))
        if position + 1 < len(hops):
            return httpx.Response(302, headers={"location": hops[position + 1]})
        return httpx.Response(200, content=b"blob")

    with fake_api(responder) as sent:
        answer = await social_media.fetch_public_media(hops[0], source="Bluesky", trusted_origin="https://pds.internal.corp")
    assert isinstance(answer, str) and _refused(answer)
    # fetch_file checks a redirect target and the hop check then sees the same request.
    assert list(dict.fromkeys(_no_waits_and_public_urls)) == checked
    assert [str(r.url) for r in sent] == hops[:-1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint",
    [
        # A foreign PDS redirecting to the configured, private PDS.
        "https://pds.example.com",
        # A foreign DID document naming the configured PDS origin itself.
        "https://pds.internal.corp",
    ],
)
async def test_bluesky_never_trusts_the_configured_pds_for_another_author(real_outbound_policy, endpoint):
    base = _bluesky_author("did:plc:other", _pds_document("did:plc:other", endpoint))

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.host == "pds.example.com":
            return httpx.Response(302, headers={"location": "https://pds.internal.corp/xrpc/com.atproto.sync.getBlob"})
        return base(request)

    persist = RecordingPersist()
    executor = _executor(bluesky.build_tools, "me.bsky.social||pw||https://pds.internal.corp", persist)
    with fake_api(responder) as sent:
        result = await call(executor, "download_bluesky_media", post_uri="at://did:plc:other/app.bsky.feed.post/9z")
    assert _refused(result.content)
    assert not any(r.url.host == "pds.internal.corp" and r.url.path.endswith("getBlob") for r in sent)
    assert persist.calls == []


# ---------------------------------------------------------------------------
# The stored name of a posted file never reaches the model in the open
# ---------------------------------------------------------------------------

#: Each provider's post tool: builder, token, tool, its arguments, and the
#: request (URL fragment, status, body) that fails for the error case.
_POSTS = {
    "linkedin": (linkedin.build_tools, "tok||personal||", "create_linkedin_post", {"text": "x"}, ("action=initializeUpload", 500, None)),
    "x": (x.build_tools, "xtok", "create_x_tweet", {"text": "x"}, ("/initialize", 500, None)),
    "facebook": (facebook.build_tools, _FB_TOKEN, "create_facebook_post", {"message": "x"}, ("/photos", 500, None)),
    "bluesky": (bluesky.build_tools, "me.bsky.social||pw", "create_bluesky_post", {"text": "x"}, ("uploadBlob", 200, {})),
}
_POST_APIS = {"linkedin": _linkedin, "x": _x, "facebook": _facebook, "bluesky": _bluesky}


def _failing(responder, broken: str, status: int, body: dict | None):
    """*responder*, except that a request whose URL carries *broken* gets *status* and *body*."""

    def answer(request: httpx.Request) -> httpx.Response:
        if broken in str(request.url):
            return httpx.Response(status, json=body if body is not None else {"message": "down"})
        return responder(request)

    return answer


def _reset_post_fakes() -> None:
    _linkedin.images = []  # type: ignore[attr-defined]
    _facebook.photos = []  # type: ignore[attr-defined]
    _x.category = "image"  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", list(_POSTS))
@pytest.mark.parametrize("hostile", list(HOSTILE_FILE_NAMES))
async def test_a_posted_file_is_named_by_its_id_never_by_its_stored_name(provider, hostile):
    builder, token, tool, args, _failure = _POSTS[provider]
    _reset_post_fakes()
    ctx, ids = _context((_png(), HOSTILE_FILE_NAMES[hostile], "image/png"))
    executor = _executor(builder, token, ctx=ctx)
    with fake_api(_POST_APIS[provider]):
        result = await call(executor, tool, **args, media_file_ids=ids)
    assert not result.is_error, result.content
    assert "PWNED" not in result.content
    assert "<<UNTRUSTED-" not in result.content.upper()
    assert f"file {ids[0]}: Visual summary: 4x3 px" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", list(_POSTS))
@pytest.mark.parametrize("hostile", list(HOSTILE_FILE_NAMES))
async def test_a_failed_media_upload_names_the_file_by_its_id(provider, hostile):
    builder, token, tool, args, (broken, status, body) = _POSTS[provider]
    _reset_post_fakes()
    ctx, ids = _context((_png(), HOSTILE_FILE_NAMES[hostile], "image/png"))
    executor = _executor(builder, token, ctx=ctx)
    with fake_api(_failing(_POST_APIS[provider], broken, status, body)):
        result = await call(executor, tool, **args, media_file_ids=ids)
    assert result.is_error, result.content
    assert "PWNED" not in result.content
    assert f"file {ids[0]}" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", list(HOSTILE_FILE_NAMES))
@pytest.mark.parametrize(
    ("data", "content_type", "expected"),
    [
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024 + 1), "image/png", "over the 5 MB"),
        (b"PK\x03\x04zip", "application/zip", "is of a type X does not take"),
    ],
    ids=["over-5-MB", "zip"],
)
async def test_a_refused_media_file_carries_its_stored_name_only_as_untrusted_data(
    hostile, data, content_type, expected
):
    name = HOSTILE_FILE_NAMES[hostile]
    ctx, ids = _context((data, name, content_type))
    executor = _executor(x.build_tools, "xtok", ctx=ctx)
    with fake_api(_x) as sent:
        result = await call(executor, "create_x_tweet", text="x", media_file_ids=ids)
    assert result.is_error
    assert sent == []
    outside = assert_name_only_inside(result.content, name)
    assert outside.startswith(f"Error: file {ids[0]} ")
    assert expected in outside
    assert "Nothing has been published." in outside
