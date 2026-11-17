"""A template's brand marks drawn as screen inserts, and an insert left out failing its scene.

A screen insert naming the template's ``logo``, ``logo_light`` or
``logo_mark`` draws that SVG to a PNG through the bounded rasteriser's worker
(``screen_insert.svg_to_png``). What is proven:

* every such SVG every built-in template seeds (``template_asset_seed``)
  renders there to a PNG with alpha - transparent where the SVG draws
  nothing - whose opaque pixels are the SVG's own ink, the fills its shapes
  declare;
* a raster worker spawned from a process that maps most of its budget before
  it starts - a render's ``__main__``, re-imported on a host with many CPUs -
  still draws it: the cap bounds what the work adds (``worker_process``);
* the rasteriser says why it drew nothing, and ``svg_to_png`` logs and raises
  it, never a silent ``None``;
* an insert the render cannot draw - its SVG refused, a mark the template has
  no file for - is a failing ``insert_missing`` finding of its scene, in a
  preview's window or not at all.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import defusedxml.ElementTree as DefusedET
import numpy as np
import pytest
from PIL import Image, ImageColor

from app.core import worker_process
from app.services import svg_raster, template_asset_seed
from app.services.video_studio import insert_checks, render, render_checks, screen_insert, timeline
from app.services.video_studio.spec import parse_video_spec, validate_video_spec

BACKEND_ROOT = Path(__file__).resolve().parents[1]
MARK_ROLES = ("logo", "logo_light", "logo_mark")
#: Elements whose shapes are never drawn where they stand: a clip path, a mask, a definition.
_UNDRAWN = frozenset({"defs", "clipPath", "mask", "pattern", "marker", "symbol"})
_SHAPES = frozenset({"path", "rect", "circle", "ellipse", "polygon", "polyline", "line", "text"})


def _brand_marks() -> list[tuple[str, template_asset_seed.SeedAsset]]:
    found = []
    for slug, seeded in sorted(template_asset_seed.merge_manifests(template_asset_seed.load_brand_manifests()).items()):
        found += [(slug, asset) for asset in seeded if asset.role in MARK_ROLES and asset.content_type.startswith("image/svg")]
    return found


BRAND_MARKS = _brand_marks()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _ink(data: bytes) -> set[tuple[int, int, int]]:
    """The colours an SVG's drawn shapes paint: their fills and strokes, inherited, outside its definitions."""
    inks: set[tuple[int, int, int]] = set()

    def walk(element: ET.Element, fill: str, stroke: str) -> None:
        if _local(element.tag) in _UNDRAWN:
            return
        fill, stroke = element.get("fill", fill), element.get("stroke", stroke)
        if _local(element.tag) in _SHAPES:
            inks.update(ImageColor.getrgb(paint)[:3] for paint in (fill, stroke) if paint and paint != "none")
        for child in element:
            walk(child, fill, stroke)

    walk(DefusedET.fromstring(data), "black", "none")
    return inks


def test_every_built_in_template_seeds_svg_brand_marks():
    assert {asset.role for _slug, asset in BRAND_MARKS} == set(MARK_ROLES)
    assert {"filigran-brand-film", "filigran-video", "xtm-one-video", "thread-video"} <= {slug for slug, _a in BRAND_MARKS}


@pytest.mark.parametrize(("slug", "asset"), BRAND_MARKS, ids=[f"{slug}-{asset.role}" for slug, asset in BRAND_MARKS])
def test_a_brand_mark_draws_as_an_insert_to_its_own_ink_on_transparency(slug, asset, tmp_path):
    source = tmp_path / asset.filename
    source.write_bytes(asset.data)
    drawn = asyncio.run(screen_insert.svg_to_png(str(source), f"{slug} {asset.role}"))
    with Image.open(drawn) as opened:
        assert opened.mode == "RGBA", f"{asset.filename}: drawn without alpha"
        assert max(opened.size) == screen_insert.SVG_BOX
        pixels = np.asarray(opened)
    alpha = pixels[..., 3]
    opaque = alpha == 255
    assert (alpha == 0).mean() > 0.05, f"{asset.filename}: drawn on a card, not on transparency"
    assert 0.01 < opaque.mean() < 0.95, f"{asset.filename}: {opaque.mean():.1%} of the box inked"
    inks = np.array(sorted(_ink(asset.data)), dtype=np.int16)
    assert len(inks), f"{asset.filename}: no ink declared"
    shown = pixels[opaque][:, :3].astype(np.int16)
    nearest = np.abs(shown[:, None, :] - inks[None, :, :]).max(axis=-1).min(axis=1)
    assert nearest.max() <= 2, f"{asset.filename}: opaque pixels of a colour it does not paint ({nearest.max()} off)"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the address-space cap is RLIMIT_AS")
def test_a_worker_spawned_under_a_heavy_main_still_draws_the_logo(tmp_path):
    """A ``__main__`` mapping its budget but 64 MiB before the worker starts: a fixed cap left the render nothing."""
    logo = tmp_path / "filigran-logo.svg"
    logo.write_bytes(next(asset.data for slug, asset in BRAND_MARKS if slug == "filigran-brand-film" and asset.role == "logo"))
    ran = subprocess.run(  # noqa: S603
        [sys.executable, str(BACKEND_ROOT / "tests" / "svg_raster_heavy_main.py"), str(logo)],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(BACKEND_ROOT),
        env={**os.environ, "PYTHONPATH": str(BACKEND_ROOT)},
    )
    assert ran.returncode == 0, ran.stderr[-2000:]
    assert ran.stdout.strip().splitlines()[-1].startswith("drawn "), ran.stdout + ran.stderr[-2000:]


@pytest.mark.skipif(os.name != "posix", reason="RLIMIT_AS")
def test_a_workers_cap_is_its_budget_past_what_it_maps_within_its_hard_limit(monkeypatch):
    import resource

    set_to: list[tuple[int, int]] = []
    monkeypatch.setattr(worker_process, "address_space_in_use", lambda: 3 << 30)
    monkeypatch.setattr(resource, "setrlimit", lambda _which, limits: set_to.append(limits))
    monkeypatch.setattr(resource, "getrlimit", lambda _which: (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
    assert worker_process.limit_address_space(2 << 30, "test worker") == 5 << 30
    assert set_to[-1] == (5 << 30, resource.RLIM_INFINITY)
    monkeypatch.setattr(resource, "getrlimit", lambda _which: (4 << 30, 4 << 30))
    assert worker_process.limit_address_space(2 << 30, "test worker") == 4 << 30
    assert set_to[-1] == (4 << 30, 4 << 30)


def test_the_rasteriser_says_why_it_drew_nothing_and_svg_to_png_logs_and_raises_it(tmp_path, caplog):
    assert svg_raster.render_svg(b"<html/>", box=64) == svg_raster.SvgRender(None, "its root is not <svg>")
    assert svg_raster.render_svg(b"", box=64).reason == "the SVG is empty"
    assert asyncio.run(svg_raster.render_svg_async(b"<svg", box=64)).reason.startswith("it could not be read:")
    source = tmp_path / "logo.svg"
    source.write_bytes(b"<html/>")
    with caplog.at_level("WARNING", logger=screen_insert.__name__), pytest.raises(screen_insert.SvgNotDrawnError) as refused:
        asyncio.run(screen_insert.svg_to_png(str(source), "Scene cat-cape: its screen insert 0"))
    assert str(refused.value) == "its root is not <svg>"
    assert "Scene cat-cape: its screen insert 0: its SVG (logo.svg, 7 bytes) was not drawn: its root is not <svg>" in caplog.text


def test_a_worker_that_dies_rendering_says_so(monkeypatch):
    async def died(_executor, _timeout, what, *_args):
        return None if what == "worker start" else svg_raster._Failed(f"the SVG raster worker died during its {what}")

    svg_raster.clear_svg_raster_cache()
    monkeypatch.setattr(svg_raster, "_in_worker", died)
    rendered = asyncio.run(svg_raster.render_svg_async(b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"/>', box=64))
    assert rendered == svg_raster.SvgRender(None, "the SVG raster worker died during its render")
    # A death says something about the moment, not the bytes: never cached.
    assert len(svg_raster._render_cache) == 0
    svg_raster.shutdown_svg_raster_worker()


@pytest.mark.chromium
def test_the_logo_is_drawn_on_a_model_green_billboard_by_the_render_with_its_own_colours(tmp_path):
    """Scene cat-cape: ``{asset: logo}`` keyed green on the billboard a video model painted, in Chromium."""
    from app.services.video_studio import compositor, encoder
    from app.services.video_studio.spec import QUALITY_PRESETS

    if not encoder.encoder_available():
        pytest.skip("ffmpeg is not installed")
    from PIL import ImageDraw

    width, height = 640, 360
    quad = ((0.25, 0.25), (0.75, 0.27), (0.74, 0.62), (0.26, 0.6))
    room = Image.new("RGB", (width, height), (70, 80, 90))
    ImageDraw.Draw(room).polygon([(x * width, y * height) for x, y in quad], fill=(32, 255, 30))
    frames = tmp_path / "frames"
    frames.mkdir()
    for index in range(15):
        room.save(frames / f"f{index:02d}.png")
    footage = str(tmp_path / "billboard.mp4")
    names = [(index / 15, f"f{index:02d}.png") for index in range(15)]
    asyncio.run(encoder.frames_to_mezzanine(names, end_time=1.0, out_path=footage, fps=15, crf=12, preset="ultrafast", workdir=str(frames)))
    logo = tmp_path / "filigran-logo.svg"
    logo.write_bytes(next(asset.data for slug, asset in BRAND_MARKS if slug == "filigran-brand-film" and asset.role == "logo"))
    drawn = asyncio.run(screen_insert.svg_to_png(str(logo), "Scene cat-cape: its screen insert 0"))
    key = "insert/cat-cape.0"
    # The box the page and the driver place every frame on, as ``render._matte_inserts`` sets it.
    box = screen_insert.grown_box(quad, width / height)
    insert = screen_insert.InsertSource(
        drawn,
        "image",
        quad,
        fit="contain",
        glow=0.5,
        key=screen_insert.KeySpec("green"),
        footage=screen_insert.Footage(footage, "video", 15.0, "clip/cat-cape"),
        logo="logo",
        label="Scene cat-cape: its screen insert 0",
        box=tuple(box),
    )
    insert = screen_insert.keyed_on(insert, np.asarray(room))
    assets = compositor.CompositorAssets(sources={"clip/cat-cape": compositor.FrameSource(footage, 15.0)}, inserts={key: insert})
    payload = {
        "src": key,
        "kind": "image",
        "quad": [list(p) for p in quad],
        "box": [list(p) for p in box],
        "fit": "contain",
        "glow": 0.5,
        "t0": 0.0,
        "t1": 1.0,
        "matte": {"fps": 15.0, "frames": 15},
    }
    scene = {
        "id": "cat-cape",
        "type": "clip",
        "src": "clip/cat-cape",
        "fit": "cover",
        "time_map": [[0, 0], [1, 1]],
        "src_fps": 15,
        "src_frames": 15,
        "src_w": width,
        "src_h": height,
        "transition_in": {"type": "cut", "duration": 0},
        "overlays": [],
        "inserts": [payload],
        "start": 0.0,
        "end": 1.0,
    }
    document = {
        "version": 1,
        "width": width,
        "height": height,
        "fps": 15,
        "duration": 1.0,
        "frames": 15,
        "fade_in": 0.0,
        "fade_out": 0.0,
        "theme": {},
        "watermark": "none",
        "captions": {"enabled": False, "style": "pill", "position": "bottom", "cues": []},
        "scenes": [scene],
    }
    audits: dict = {}
    try:
        video = asyncio.run(
            compositor.render_frames(
                document,
                assets,
                preset=QUALITY_PRESETS["draft"],
                output_size=(width, height),
                workdir=str(tmp_path / "render"),
                font_css="",
                pages=1,
                audits=audits,
            )
        )
    except Exception as exc:
        if "Executable doesn't exist" not in str(exc) and "playwright install" not in str(exc).lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")

    async def middle() -> np.ndarray:
        frames_read = [raw async for raw in encoder.iter_rgb_frames(video, width=width, height=height)]
        return np.frombuffer(frames_read[7], dtype=np.uint8).reshape(height, width, 3).astype(int)

    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).polygon([(x * width, y * height) for x, y in quad], fill=255)
    pixels = asyncio.run(middle())
    if os.environ.get("KEEP_FRAME"):
        Image.fromarray(pixels.astype(np.uint8)).save(os.environ["KEEP_FRAME"])
    shown = pixels[np.asarray(mask) > 0]
    cyan = (np.abs(shown - (15, 188, 255)).max(axis=1) < 40).mean()
    white = (np.abs(shown - (242, 242, 243)).max(axis=1) < 40).mean()
    assert cyan > 0.01 and white > 0.02, f"the logo is not on the billboard: {cyan:.1%} cyan, {white:.1%} white"
    # No card: nothing dark behind the mark, and no green left of the surface it covers. (The board itself is the
    # model's green with the key clamped out, lit by the mark's glow - a level in the forties; a card is far under it.)
    assert (shown.mean(axis=1) < 30).mean() < 0.02
    assert (shown[:, 1] - np.maximum(shown[:, 0], shown[:, 2]) > 60).mean() < 0.02, "the surface shows green through"
    # A halo: the board just below the mark is lit by it, brighter than the board far from any ink.
    near, far = pixels[196, 203].sum(), pixels[205, 440].sum()
    assert near - far > 12, f"no halo on the board around the mark ({near} beside it, {far} far from it)"
    logo_frames = audits[key]["frames"]
    assert logo_frames and all((a["logo_visible"] or 0) >= screen_insert.LOGO_MIN_VISIBLE for a in logo_frames.values())


# ── An insert left out ────────────────────────────────────────────────────

CLIP = "00000000-0000-0000-0000-000000006177"
QUAD = [[0.3, 0.2], [0.7, 0.22], [0.69, 0.7], [0.31, 0.68]]


def _spec(start: float = 1.0):
    insert = {"type": "screen_insert", "source": {"asset": "logo"}, "quad": QUAD, "start": start, "duration": 2.0}
    return parse_video_spec(
        validate_video_spec(
            {
                "title": "Billboard",
                "freestyle": True,
                "scenes": [
                    {"id": "intro", "type": "title", "title": "Cats", "duration": 3},
                    {"id": "cat-cape", "type": "clip", "clip_id": CLIP, "overlays": [insert]},
                ],
            }
        )
    )


def _document(spec) -> dict:
    return {"scenes": [{"id": "intro", "start": 0.0, "duration": 3.0}, {"id": "cat-cape", "start": 3.0, "duration": 6.0}]}


def test_an_insert_left_out_is_a_failing_finding_of_its_scene_at_its_time():
    spec = _spec()
    key = timeline.insert_key("cat-cape", 0)
    found = insert_checks.missing({key: "its SVG was not drawn: its root is not <svg>"}, timeline.insert_sources(spec), _document(spec))
    assert [(f.kind, f.severity, f.scene_id, f.at, f.end) for f in found] == [
        (render_checks.INSERT_MISSING, render_checks.FAIL, "cat-cape", 4.0, 6.0)
    ]
    assert "is not drawn (its SVG was not drawn: its root is not <svg>)" in found[0].message
    assert found[0].message.endswith("instead of the template's logo") and found[0].data["asset"] == "logo"
    assert render_checks.INSERT_MISSING in render_checks.PICTURE_KINDS
    # A preview's window: timed from its start, and one that does not reach the insert is not this preview's.
    windowed = insert_checks.missing({key: "x"}, timeline.insert_sources(spec), _document(spec), offset=2.5, span=(2.5, 4.0))
    assert [(f.at, f.end) for f in windowed] == [(1.5, 3.5)]
    assert insert_checks.missing({key: "x"}, timeline.insert_sources(spec), _document(spec), span=(0.0, 3.5)) == []


def test_a_render_whose_insert_svg_is_not_drawn_records_it_missing(monkeypatch, tmp_path):
    from app.services.video_studio import assets

    spec = _spec()
    key = timeline.insert_key("cat-cape", 0)

    async def download_file(_file_id, path):
        Path(path).write_bytes(b"<html/>")
        return SimpleNamespace(filename="filigran-logo.svg", content_type="image/svg+xml")

    monkeypatch.setattr(assets, "download_file", download_file)
    media = render._Media()
    asyncio.run(render._gather_inserts(media, spec, {"insert_files": {key: "logo-file"}}, tmp_path))
    assert key not in media.assets.inserts
    assert media.missing_inserts == {key: "its SVG was not drawn: its root is not <svg>"}


def test_a_mark_the_template_has_no_file_for_is_recorded_missing_never_another_mark(monkeypatch):
    from contextlib import asynccontextmanager

    from app.services import presentations
    from app.services.video_studio import lexicon_store

    @asynccontextmanager
    async def session():
        yield SimpleNamespace()

    async def nothing(*_args, **_kwargs):
        return None

    async def no_groups(*_args, **_kwargs):
        return []

    async def palette(*_args, **_kwargs):
        # The template draws a bare mark, and has no logo.
        return {"mark_file_id": "00000000-0000-0000-0000-00000000aaaa"}

    monkeypatch.setattr(render, "async_session", session)
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", no_groups)
    monkeypatch.setattr(lexicon_store, "for_voiceover", nothing)
    monkeypatch.setattr(presentations, "resolve_template_theme", palette)
    spec = parse_video_spec(
        validate_video_spec(
            {
                "title": "Billboard",
                "freestyle": True,
                "scenes": [
                    {
                        "id": "cat-cape",
                        "type": "statement",
                        "lines": ["A billboard"],
                        "backdrop": {"file_id": "00000000-0000-0000-0000-00000000bbbb"},
                        "overlays": [{"type": "screen_insert", "source": {"asset": "logo"}, "quad": QUAD}],
                    }
                ],
            }
        )
    )
    found = asyncio.run(
        render._read_references(spec, SimpleNamespace(id=uuid.uuid4(), role="user"), "00000000-0000-0000-0000-00000000cccc")
    )
    key = timeline.insert_key("cat-cape", 0)
    assert key not in found["insert_files"], "the bare mark never stands for the logo"
    assert found["missing_inserts"] == {key: "the template has no file for its logo"}
