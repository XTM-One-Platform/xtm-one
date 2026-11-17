"""An unkeyed contain insert on a chroma screen leaves the screen's key colour in its bands: a finding, never by design.

Issue #11271. On synthetic footage, no provider, no browser: a flat green screen, a 16:9 source on its 4:3 quad with
fit contain.

* key null: the frame audit reads the bands' green (``FrameAudit.band_key``), the render fails
  ``insert_band_key_colour`` with the band share and the fix, and the report no longer lists the bands as by design;
* key green: the bands are filled (the source's edge colour, or the bezel's) and nothing is raised;
* key null on a real (grey) screen: unchanged - the bands show the footage, by design, no finding.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from app.services.video_studio import frame_qc, insert_checks, insert_keep_out, render_checks, screen_insert
from app.services.video_studio.render_checks import INSERT_BAND_KEY_COLOUR
from app.services.video_studio.screen_insert import Footage, InsertSource, KeySpec

W, H = 960, 540
SCREEN = (300, 130, 660, 400)
KEY = "insert/s1.0"
LABEL = "Scene s1: its screen insert 0"
GREEN = (0, 200, 60)
GREY = (70, 72, 78)


def _quad() -> tuple:
    x0, y0, x1, y1 = SCREEN
    return tuple((x / W, y / H) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)))


QUAD = _quad()


def _room(screen: tuple, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    picture = Image.fromarray((rng.random((H, W, 3)) * 20 + 95).astype(np.uint8))
    draw = ImageDraw.Draw(picture)
    x0, y0, x1, y1 = SCREEN
    draw.rectangle((x0 - 8, y0 - 8, x1 + 8, y1 + 8), fill=(28, 28, 32))
    draw.rectangle((x0, y0, x1, y1), fill=screen)
    return np.asarray(picture).copy()


def _screenshot() -> Image.Image:
    picture = Image.new("RGB", (1600, 900), (24, 28, 36))
    draw = ImageDraw.Draw(picture)
    for row in range(80, 820, 40):
        draw.rectangle((120, row, 1480, row + 14), fill=(210, 215, 225))
    return picture


def _insert(key: KeySpec | None) -> InsertSource:
    return InsertSource(
        "p.png",
        "image",
        QUAD,
        fit="contain",
        key=key,
        footage=Footage("f", "video", 24.0, "clip/s1"),
        box=tuple(screen_insert.grown_box(QUAD, W / H)),
        label=LABEL,
    )


def _layers(insert: InsertSource, frames: list[np.ndarray]) -> list:
    if insert.key is not None:
        insert = screen_insert.keyed_on(insert, frames[0])
    state = screen_insert.Matting()
    return [
        screen_insert.compose(insert, _screenshot(), (W, H), n, footage=frame, state=state, window_frame=n)
        for n, frame in enumerate(frames)
    ]


def _audits(layers: list) -> dict:
    return {KEY: {"frames": {str(n): layer.audit.as_dict() for n, layer in enumerate(layers)}, "evidence": {}}}


def _placed(insert: InsertSource) -> dict:
    return {
        KEY: insert_checks.Placed(
            KEY,
            "s1",
            LABEL,
            0.0,
            1.0,
            keyed=insert.key is not None,
            fit=insert.fit,
            key_settings=insert_checks.key_settings(insert.key),
        )
    }


def _band_findings(insert: InsertSource, layers: list) -> list:
    found = insert_checks.findings(_audits(layers), _placed(insert), fps=24)
    return [finding for finding, _proof in found if finding.kind == INSERT_BAND_KEY_COLOUR]


def test_band_key_colour_reads_the_chroma_colour_of_the_bands_alone():
    region = np.zeros((10, 10, 3), dtype=np.uint8)
    region[...] = GREY
    banded = np.zeros((10, 10), dtype=bool)
    banded[:, :3] = True
    assert screen_insert.band_key_colour(region, banded) is None
    region[:, :3] = GREEN
    assert screen_insert.band_key_colour(region, banded) == ("green", 1.0)
    region[:, :3] = (20, 40, 210)
    assert screen_insert.band_key_colour(region, banded) == ("blue", 1.0)
    region[:, :3] = (220, 10, 200)
    assert screen_insert.band_key_colour(region, banded) == ("magenta", 1.0)
    assert screen_insert.band_key_colour(region, np.zeros((10, 10), dtype=bool)) is None


def test_an_unkeyed_contain_insert_on_a_green_screen_fails_with_the_band_share_and_the_fix():
    insert = _insert(None)
    layers = _layers(insert, [_room(GREEN, seed=n) for n in range(6)])
    audit = layers[-1].audit
    assert audit.band_fill == "footage" and audit.band_key == "green" and audit.band_key_share > 0.9
    [finding] = _band_findings(insert, layers)
    assert finding.severity == render_checks.FAIL and finding.kind in render_checks.PICTURE_KINDS
    assert finding.data["band_key"] == "green" and abs(finding.data["band_share"] - 0.25) < 0.02
    assert finding.data["action"]["kind"] == insert_checks.SET_KEY
    assert "set key: green" in finding.fix and "fit: cover" in finding.fix
    assert "of the quad" in finding.message and "no key" in finding.message
    [report] = insert_checks.reports(_audits(layers), _placed(insert), fps=24)
    assert report["contain_band"]["key"] == "green" and report["contain_band"]["filled_with"] == "footage"
    said = [measured.text for measured in insert_checks.classify(report, {INSERT_BAND_KEY_COLOUR})]
    assert not any("showing the footage" in what for what in said), said


def test_the_same_insert_keyed_green_fills_its_bands_and_raises_nothing():
    insert = _insert(KeySpec("green", 0.4, 3.0))
    layers = _layers(insert, [_room(GREEN, seed=10 + n) for n in range(6)])
    audit = layers[-1].audit
    assert audit.band_fill in screen_insert.BAND_FILLS and audit.band_fill not in ("footage", "surface")
    assert audit.band_key is None
    assert _band_findings(insert, layers) == []


def test_an_unkeyed_contain_insert_on_a_real_screen_is_unchanged():
    insert = _insert(None)
    layers = _layers(insert, [_room(GREY, seed=20 + n) for n in range(6)])
    audit = layers[-1].audit
    assert audit.band_fill == "footage" and audit.band_key is None
    assert _band_findings(insert, layers) == []
    [report] = insert_checks.reports(_audits(layers), _placed(insert), fps=24)
    assert "key" not in report["contain_band"]
    said = [measured.text for measured in insert_checks.classify(report)]
    assert any("showing the footage" in what for what in said), said


def test_a_minority_of_keyed_frames_is_no_finding():
    insert = _insert(None)
    frames = [_room(GREY, seed=30 + n) for n in range(5)] + [_room(GREEN, seed=40)]
    layers = _layers(insert, frames)
    assert _band_findings(insert, layers) == []


def test_the_key_null_hint_says_a_contain_fit_then_shows_the_key_in_its_bands():
    measure = insert_keep_out.Overlap(100, "green", 0.04, 12, 0.5, 0.9, 30, 3, 0.12, (0.4, 0.3, 0.5, 0.4), 24.0)
    said = insert_keep_out.key_advisory({frame_qc.QC_KEY: {frame_qc.FOREGROUND_OVERLAP: measure.as_dict()}}) or ""
    assert "key: null" in said and "contain fit left unkeyed shows the screen's green in its bands" in said
