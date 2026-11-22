"""sandbox_convert_images: convert, resize and compress workspace images in one write (gap image-convert-tool)."""

from __future__ import annotations

import io
import uuid

import pytest
from PIL import Image

from app.services.sandbox_workspace import images
from tests.test_sandbox_workspace_tools import _call, _composite, _put, _workspace_bytes, store  # noqa: F401


def _png(width: int = 400, height: int = 200, mode: str = "RGBA") -> bytes:
    out = io.BytesIO()
    Image.new(mode, (width, height), (10, 120, 200, 128) if mode == "RGBA" else (10, 120, 200)).save(out, "PNG")
    return out.getvalue()


def _opened(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def test_parse_reads_the_format_from_the_target_suffix():
    [c] = images.parse({"from": "a/shot.png", "to": "a/shot.WEBP", "max_width": "800", "quality": 70})
    assert (c.format, c.max_width, c.quality, c.lossless) == ("WEBP", 800, 70, False)


@pytest.mark.parametrize(
    ("conversions", "message"),
    [
        ([], "at least one"),
        ([{"from": "a.png"}], "needs both"),
        ([{"from": "a.png", "to": "a.gif"}], "suffix of 'to'"),
        ([{"from": "a.png", "to": "b"}], "suffix of 'to'"),
        ([{"from": "a.png", "to": "b.webp"}, {"from": "c.png", "to": "b.webp"}], "two conversions"),
        ([{"from": "a.png", "to": "b.png", "lossless": True}], "lossless applies"),
        ([{"from": "a.png", "to": "b.jpg", "quality": 0}], "between 1 and 100"),
        ([{"from": "a.png", "to": "b.jpg", "max_width": True}], "whole number"),
    ],
)
def test_parse_refusals(conversions, message):
    with pytest.raises(images.ImageError, match=message):
        images.parse(conversions)


def test_convert_png_to_webp_bounded_keeps_ratio_and_alpha():
    [c] = images.parse([{"from": "s.png", "to": "s.webp", "max_width": 100}])
    done = images.convert(c, _png(400, 200))
    out = _opened(done.data)
    assert out.format == "WEBP" and out.size == (100, 50) and out.mode == "RGBA"
    assert done.before_size == (400, 200) and "400x200 -> 100x50" in done.line()


def test_convert_never_enlarges_and_jpeg_flattens_alpha():
    [c] = images.parse([{"from": "s.png", "to": "s.jpg", "max_width": 4000}])
    out = _opened(images.convert(c, _png(40, 20)).data)
    assert out.format == "JPEG" and out.size == (40, 20) and out.mode == "RGB"


def test_convert_resizes_a_palette_image_with_lanczos_not_nearest_neighbour():
    source = Image.new("RGB", (400, 200), (0, 0, 0))
    source.paste((255, 0, 0), (0, 0, 201, 200))
    out = io.BytesIO()
    source.convert("P").save(out, "PNG")
    [c] = images.parse([{"from": "s.png", "to": "t.png", "max_width": 100}])
    result = _opened(images.convert(c, out.getvalue()).data)
    assert result.size == (100, 50) and result.mode == "RGB"
    # Nearest-neighbour keeps the two source colours only; LANCZOS blends the edge.
    assert len(result.getcolors(maxcolors=10_000)) > 2


def test_convert_refuses_what_is_not_an_image_and_an_animation():
    [c] = images.parse([{"from": "x.png", "to": "x.webp"}])
    with pytest.raises(images.ImageError, match="not an image"):
        images.convert(c, b"not a png")
    frames = [Image.new("RGB", (8, 8), color) for color in ((255, 0, 0), (0, 255, 0))]
    gif = io.BytesIO()
    frames[0].save(gif, "GIF", save_all=True, append_images=frames[1:])
    with pytest.raises(images.ImageError, match="animated"):
        images.convert(c, gif.getvalue())


def test_convert_refuses_a_decompression_bomb_before_decoding(monkeypatch):
    monkeypatch.setattr(images, "MAX_PIXELS", 1000)
    [c] = images.parse([{"from": "x.png", "to": "x.webp"}])
    with pytest.raises(images.ImageError, match="not decoded"):
        images.convert(c, _png(100, 100))


async def test_tool_converts_in_one_write(store):  # noqa: F811
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    await _put(composite, "repo/docs/a.png", _png(400, 200))
    await _put(composite, "repo/docs/b.png", _png(30, 30, mode="RGB"))
    done = await _call(
        composite,
        "sandbox_convert_images",
        conversions=[
            {"from": "repo/docs/a.png", "to": "repo/docs/a.webp", "max_width": 200},
            {"from": "repo/docs/b.png", "to": "repo/docs/b.png", "max_width": 10},
        ],
    )
    assert not done.is_error, done.content
    assert done.content.startswith("Converted 2 image(s) in one write")
    assert _opened(await _workspace_bytes(store, conversation, "repo/docs/a.webp")).size == (200, 100)
    assert _opened(await _workspace_bytes(store, conversation, "repo/docs/b.png")).size == (10, 10)


async def test_tool_refusals_write_nothing(store):  # noqa: F811
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    await _put(composite, "a.png", _png(20, 20))
    await _put(composite, "a.webp", b"old")
    await _put(composite, "notes.txt", b"hello\n")
    exists = await _call(composite, "sandbox_convert_images", conversions=[{"from": "a.png", "to": "a.webp"}])
    assert exists.is_error and "overwrite=true" in exists.content
    missing = await _call(composite, "sandbox_convert_images", conversions=[{"from": "zz.png", "to": "zz.webp"}])
    assert missing.is_error and "not a file of the workspace" in missing.content
    partly = await _call(
        composite,
        "sandbox_convert_images",
        conversions=[{"from": "a.png", "to": "c.webp"}, {"from": "notes.txt", "to": "d.webp"}],
    )
    assert partly.is_error and "Nothing was written" in partly.content
    assert await _workspace_bytes(store, conversation, "a.webp") == b"old"
    listing = await _call(composite, "sandbox_list_files")
    assert "c.webp" not in listing.content
