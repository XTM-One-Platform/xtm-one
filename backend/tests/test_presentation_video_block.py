# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""The ``video`` block (xtm-one #11686): validation, save-time resolution, PPTX movie."""

from __future__ import annotations

import io
import struct
import uuid
import zlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from pptx import Presentation as PptxPresentation

from app.models.video_studio import JOB_READY
from app.services import pptx_writer, presentations
from app.services.presentation_guidance import BLOCK_EXAMPLES
from app.services.presentation_spec import (
    BLOCK_TYPES,
    VIDEO_EMBED_ORIGINS,
    PresentationSpecError,
    validate_spec,
    video_embed,
)

if TYPE_CHECKING:
    from app.models.user import User

FILE = "11111111-1111-4111-8111-111111111111"
POSTER = "22222222-2222-4222-8222-222222222222"
CAPTIONS = "33333333-3333-4333-8333-333333333333"
VIDEO = "44444444-4444-4444-8444-444444444444"


def _deck(*blocks: dict) -> dict:
    return {"title": "Deck", "slides": [{"layout": "content", "title": "Tour", "blocks": list(blocks)}]}


def _block(spec: dict) -> dict:
    return spec["slides"][0]["blocks"][0]


def test_video_is_a_block_type_with_a_catalog_example():
    assert "video" in BLOCK_TYPES
    assert BLOCK_EXAMPLES["video"]["type"] == "video"


def test_a_video_block_with_a_file_validates_with_its_defaults():
    block = _block(validate_spec(_deck({"type": "video", "file_id": FILE})))
    assert block["file_id"] == FILE
    assert block["autoplay"] is False
    assert block["controls"] is True
    assert block["loop"] is False
    assert block["captions"] is True
    assert block["frame"] == "none"


def test_a_video_block_with_a_video_id_validates():
    block = _block(validate_spec(_deck({"type": "video", "video_id": VIDEO, "autoplay": True, "frame": "browser"})))
    assert block["video_id"] == VIDEO
    assert block["autoplay"] is True


def test_a_video_block_needs_a_source():
    with pytest.raises(PresentationSpecError, match="video_id"):
        validate_spec(_deck({"type": "video", "alt": "Tour"}))


def test_a_video_block_end_must_come_after_start():
    with pytest.raises(PresentationSpecError, match="end"):
        validate_spec(_deck({"type": "video", "file_id": FILE, "start": 10, "end": 5}))


def test_a_video_block_rejects_an_unknown_key():
    with pytest.raises(PresentationSpecError):
        validate_spec(_deck({"type": "video", "file_id": FILE, "volume": 3}))


def test_a_video_block_can_be_the_media_cell_of_a_bento():
    spec = {
        "title": "Deck",
        "slides": [
            {
                "layout": "content",
                "title": "Bento",
                "bento": True,
                "blocks": [
                    {"type": "video", "file_id": FILE},
                    {"type": "text", "content": "One"},
                    {"type": "text", "content": "Two"},
                ],
            }
        ],
    }
    assert validate_spec(spec)["slides"][0]["blocks"][0]["type"] == "video"


def _render(file_id: str | None = FILE, status: str = JOB_READY):
    return SimpleNamespace(
        status=status,
        file_id=uuid.UUID(file_id) if file_id else None,
        poster_file_id=uuid.UUID(POSTER),
        captions_file_id=uuid.UUID(CAPTIONS),
    )


@pytest.mark.asyncio
async def test_resolve_fills_the_render_files_of_a_visible_video(monkeypatch):
    project = SimpleNamespace(id=uuid.UUID(VIDEO))

    async def _visible(_db, _id, _user):
        return project

    async def _current(_db, _project):
        return _render()

    monkeypatch.setattr("app.services.video_studio.projects.load_visible_project", _visible)
    monkeypatch.setattr("app.services.video_studio.public_links.current_render", _current)
    spec = validate_spec(_deck({"type": "video", "video_id": VIDEO}))
    vouched = await presentations.resolve_video_blocks(None, spec, SimpleNamespace(id=uuid.uuid4()))
    block = _block(spec)
    assert block["file_id"] == FILE
    assert block["poster_file_id"] == POSTER
    assert block["captions_file_id"] == CAPTIONS
    assert vouched == {FILE, POSTER, CAPTIONS}


@pytest.mark.asyncio
async def test_resolve_keeps_a_poster_the_block_names(monkeypatch):
    own_poster = "55555555-5555-4555-8555-555555555555"

    async def _visible(_db, _id, _user):
        return SimpleNamespace(id=uuid.UUID(VIDEO))

    async def _current(_db, _project):
        return _render()

    monkeypatch.setattr("app.services.video_studio.projects.load_visible_project", _visible)
    monkeypatch.setattr("app.services.video_studio.public_links.current_render", _current)
    spec = validate_spec(_deck({"type": "video", "video_id": VIDEO, "poster_file_id": own_poster}))
    vouched = await presentations.resolve_video_blocks(None, spec, SimpleNamespace(id=uuid.uuid4()))
    assert _block(spec)["poster_file_id"] == own_poster
    assert own_poster not in vouched


@pytest.mark.asyncio
async def test_resolve_refuses_a_video_without_a_ready_render(monkeypatch):
    async def _visible(_db, _id, _user):
        return SimpleNamespace(id=uuid.UUID(VIDEO))

    async def _current(_db, _project):
        return None

    monkeypatch.setattr("app.services.video_studio.projects.load_visible_project", _visible)
    monkeypatch.setattr("app.services.video_studio.public_links.current_render", _current)
    spec = validate_spec(_deck({"type": "video", "video_id": VIDEO}))
    with pytest.raises(PresentationSpecError, match="no ready render"):
        await presentations.resolve_video_blocks(None, spec, SimpleNamespace(id=uuid.uuid4()))


@pytest.mark.asyncio
async def test_resolve_refuses_a_video_the_writer_cannot_open(monkeypatch):
    async def _hidden(_db, _id, _user):
        return None

    monkeypatch.setattr("app.services.video_studio.projects.load_visible_project", _hidden)
    spec = validate_spec(_deck({"type": "video", "video_id": VIDEO}))
    with pytest.raises(PresentationSpecError, match="not a Video Studio video you can open"):
        await presentations.resolve_video_blocks(None, spec, SimpleNamespace(id=uuid.uuid4()))


def test_a_block_with_its_file_is_not_resolved_again():
    spec = validate_spec(_deck({"type": "video", "video_id": VIDEO, "file_id": FILE}))
    assert presentations._unresolved_video_blocks(spec) == []


# -- External videos (xtm-one #11701): YouTube / Vimeo players and imported MP4 / WebM links.


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", ("youtube", "dQw4w9WgXcQ")),
        ("https://youtu.be/dQw4w9WgXcQ?t=10", ("youtube", "dQw4w9WgXcQ")),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", ("youtube", "dQw4w9WgXcQ")),
        ("https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ", ("youtube", "dQw4w9WgXcQ")),
        ("https://vimeo.com/76979871", ("vimeo", "76979871")),
        ("https://player.vimeo.com/video/76979871", ("vimeo", "76979871")),
        ("https://cdn.example.com/tour.mp4", None),
    ],
)
def test_video_embed_reads_the_provider_and_its_video(url: str, expected: tuple[str, str] | None) -> None:
    assert video_embed(url) == expected


@pytest.mark.parametrize("url", ["https://www.youtube.com/@filigran", "https://vimeo.com/channels/staffpicks"])
def test_a_player_link_that_names_no_video_is_refused(url: str) -> None:
    with pytest.raises(PresentationSpecError, match="names no video"):
        validate_spec(_deck({"type": "video", "url": url}))


@pytest.mark.parametrize(
    "url", ["http://cdn.example.com/tour.mp4", "javascript:alert(1)", "ftp://x.test/a.mp4", "https:///nohost.mp4"]
)
def test_a_video_url_must_be_https(url: str) -> None:
    with pytest.raises(PresentationSpecError, match="https://"):
        validate_spec(_deck({"type": "video", "url": url}))


def test_a_video_block_plays_one_source() -> None:
    with pytest.raises(PresentationSpecError, match="not both"):
        validate_spec(_deck({"type": "video", "video_id": VIDEO, "url": "https://vimeo.com/76979871"}))


def test_the_frame_src_allows_only_the_two_players() -> None:
    from app.core.csp import BASE_DIRECTIVES

    assert set(VIDEO_EMBED_ORIGINS.values()) <= set(BASE_DIRECTIVES["frame-src"])
    assert set(BASE_DIRECTIVES["frame-src"]) == {"'self'", *VIDEO_EMBED_ORIGINS.values()}


def test_a_player_link_is_never_imported() -> None:
    spec = validate_spec(_deck({"type": "video", "url": "https://youtu.be/dQw4w9WgXcQ"}))
    assert presentations._unimported_video_links(spec) == []
    assert _block(spec)["url"] == "https://youtu.be/dQw4w9WgXcQ"
    assert not _block(spec).get("file_id")


@pytest.mark.asyncio
async def test_an_mp4_link_is_imported_once_as_a_platform_file(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def _download(url: str, **kwargs: Any) -> tuple[bytes, str, str]:
        calls.append({"url": url, **kwargs})
        return b"\x00\x00\x00\x18ftypmp42", "video/mp4", url

    async def _persist(**kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(id=uuid.UUID(FILE))

    monkeypatch.setattr("app.services.builtin_tools.image_assets.download_public_image", _download)
    monkeypatch.setattr("app.services.builtin_tools._shared._persist_generated_file", _persist)
    spec = validate_spec(_deck({"type": "video", "url": "https://cdn.example.com/media/Product%20tour.mp4"}))
    imported = await presentations.import_video_links(spec, cast("User", SimpleNamespace(id=uuid.uuid4())))
    assert imported == {FILE}
    assert _block(spec)["file_id"] == FILE
    assert calls[0]["allowed_types"] == presentations.IMPORTED_VIDEO_TYPES
    assert calls[0]["max_bytes"] == presentations.MAX_IMPORTED_VIDEO_BYTES
    assert calls[1]["filename"] == "Product tour.mp4"
    assert calls[1]["content_type"] == "video/mp4"
    # Imported: the next save leaves it alone.
    assert presentations._unimported_video_links(spec) == []


@pytest.mark.asyncio
async def test_a_refused_video_link_is_a_field_addressed_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.builtin_tools.image_assets import ImageAssetError

    async def _download(url: str, **_kwargs: Any) -> tuple[bytes, str, str]:
        raise ImageAssetError(f"{url} is not a video the platform accepts (Content-Type 'text/html')")

    monkeypatch.setattr("app.services.builtin_tools.image_assets.download_public_image", _download)
    spec = validate_spec(_deck({"type": "video", "url": "https://example.com/page"}))
    with pytest.raises(PresentationSpecError, match=r"slides\.0\.blocks\.0\.url .*not a video") as caught:
        await presentations.import_video_links(spec, cast("User", SimpleNamespace(id=uuid.uuid4())))
    assert "100 MB" in str(caught.value)


@pytest.mark.asyncio
async def test_the_video_download_refuses_other_types_and_oversize_files() -> None:
    import httpx

    from app.services.builtin_tools.image_assets import ImageAssetError, _read_image

    def _response(content_type: str, body: bytes = b"x", length: str | None = None) -> httpx.Response:
        # A streamed body: no Content-Length unless the test declares one.
        headers = {"content-type": content_type}
        if length is not None:
            headers["content-length"] = length
        return httpx.Response(
            200, headers=headers, stream=httpx.ByteStream(body), request=httpx.Request("GET", "https://x.test/v")
        )

    async def _read(response: httpx.Response) -> tuple[bytes, str, str]:
        return await _read_image(
            response,
            "x.test",
            "https://x.test/v",
            allowed_types=presentations.IMPORTED_VIDEO_TYPES,
            max_bytes=16,
            noun="video",
        )

    with pytest.raises(ImageAssetError, match="is not a video the platform accepts"):
        await _read(_response("text/html"))
    with pytest.raises(ImageAssetError, match=r"the video is .* above the"):
        await _read(_response("video/mp4", length="1000"))
    with pytest.raises(ImageAssetError, match="the video is larger than"):
        await _read(_response("video/webm", body=b"x" * 64))
    data, mime, _ = await _read(_response("video/webm; codecs=vp9", body=b"ok"))
    assert (data, mime) == (b"ok", "video/webm")


def _png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"\x00\xff\x00\x00"
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _slide_with_poster(capture_id: str) -> dict:
    return {
        "background": {"r": 0, "g": 0, "b": 0, "a": 1},
        "items": [
            {"kind": "image", "id": capture_id, "x": 100, "y": 100, "w": 960, "h": 540, "opacity": 1, "video": FILE},
        ],
    }


def test_native_pptx_embeds_the_movie_behind_a_video_poster():
    data = pptx_writer.build_native_pptx(
        [_slide_with_poster("x0-1")],
        captures={"x0-1": _png()},
        aspect_ratio="16:9",
        movies={FILE: (b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64, "video/mp4")},
    )
    prs = PptxPresentation(io.BytesIO(data))
    shapes = list(prs.slides[0].shapes)
    assert len(shapes) == 1
    # A movie is a picture element that carries a video link.
    xml = shapes[0]._element.xml
    assert "videoFile" in xml
    assert any(part.partname.endswith(".mp4") for part in prs.part.package.iter_parts())


def test_native_pptx_keeps_the_poster_when_the_movie_is_not_loaded():
    data = pptx_writer.build_native_pptx([_slide_with_poster("x0-1")], captures={"x0-1": _png()}, aspect_ratio="16:9")
    prs = PptxPresentation(io.BytesIO(data))
    shapes = list(prs.slides[0].shapes)
    assert len(shapes) == 1
    assert "videoFile" not in shapes[0]._element.xml
