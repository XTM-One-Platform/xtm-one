"""A screen insert whose text is drawn too small to read is said at save.

Every alignment, key and colour gate passed in production on a wall of screens
nobody could read. Synthetic fixtures only - UI screens drawn with Pillow's
own font, no browser, no database, no storage. What is proven:

* the size of a picture's smallest text is read off the picture alone, at
  pixel ratio 1 and 2, on light and dark panels, through a video's blur and
  compression (``insert_legibility.text_size``); a picture with no text has
  none;
* each surface of a screen insert - each pane of a wall - is judged on the
  1080p frame: a 3x3 wall of a 3200 px recording is said, the same recording
  full frame is not; the finding gives the width it needs, fewer panes and a
  push on the insert, and a higher browser zoom or a narrower viewport only
  where the recorder saw the page keep its layout, each bringing its
  text to 12 px;
* only a recording or an uploaded file is judged; text under 10 px is an
  advisory, under 6 px a finding the render waits for;
* the soft-source gate reads the same geometry as before (``insert_checks.quad_on_frame``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import io

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.models.video_studio import JOB_READY
from app.services.video_studio import insert_checks, insert_legibility, quality, render_checks, timeline
from app.services.video_studio.insert_legibility import Legibility, TextSize
from app.services.video_studio.spec import parse_video_spec

CLIP = "00000000-0000-0000-0000-00000000c284"
TAKE = "00000000-0000-0000-0000-00000000a284"
FILE = "00000000-0000-0000-0000-00000000f284"
WORDS = [
    "Detection",
    "rules",
    "updated",
    "Threat",
    "actors",
    "linked",
    "to",
    "campaign",
    "Indicators",
    "of",
    "compromise",
    "Last",
    "seen",
]


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)


def _screen(smallest: int, ratio: int = 1) -> np.ndarray:
    """A 1600 x 760 CSS px application at pixel *ratio*: a dark sidebar of 14 px labels, a 28 px heading, a bordered
    table of 16 px cells, six caption lines of *smallest* px, a row of buttons."""
    s = ratio
    image = Image.new("RGB", (1600 * s, 760 * s), (246, 247, 249))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 240 * s, 760 * s), fill=(24, 28, 36))
    for row in range(12):
        draw.text(
            (24 * s, (40 + row * 36) * s),
            " ".join(WORDS[row % 5 : row % 5 + 2]),
            fill=(220, 225, 235),
            font=_font(14 * s),
        )
        draw.rectangle((8 * s, (42 + row * 36) * s, 18 * s, (52 + row * 36) * s), fill=(80, 160, 255))
    draw.text((280 * s, 30 * s), "Investigation overview", fill=(20, 22, 28), font=_font(28 * s))
    for row in range(14):
        y = (100 + row * 30) * s
        draw.line((270 * s, y - 6 * s, 1580 * s, y - 6 * s), fill=(210, 214, 220), width=s)
        for col in range(4):
            words = " ".join(WORDS[(row + col) % 8 : (row + col) % 8 + 2])
            draw.text(((280 + col * 230) * s, y), words, fill=(40, 44, 52), font=_font(16 * s))
    for row in range(6):
        draw.text(
            (280 * s, (540 + row * 18) * s),
            "Updated 3 minutes ago by analyst - source feed",
            fill=(110, 116, 128),
            font=_font(smallest * s),
        )
    for col in range(5):
        draw.rounded_rectangle(
            ((280 + col * 140) * s, 680 * s, (400 + col * 140) * s, 716 * s), radius=6 * s, fill=(15, 188, 255)
        )
    return np.asarray(image)


# ── The text a picture shows ─────────────────────────────────────────────


@pytest.mark.parametrize("smallest", [11, 12, 14])
@pytest.mark.parametrize("ratio", [1, 2])
def test_a_screens_smallest_text_is_read_off_the_picture(smallest, ratio):
    found = insert_legibility.text_size(_screen(smallest, ratio))
    assert found is not None and found.lines >= insert_legibility._MIN_LINES
    assert abs(found.px - smallest * ratio) <= 0.15 * smallest * ratio


def test_a_frame_blurred_and_compressed_as_a_video_reads_the_same():
    image = Image.fromarray(_screen(12, 2)).filter(ImageFilter.GaussianBlur(0.8))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=70)
    found = insert_legibility.text_size(np.asarray(Image.open(io.BytesIO(buffer.getvalue())).convert("RGB")))
    assert found is not None and abs(found.px - 24) <= 0.15 * 24
    # Drawn 900 px wide, what a pane of a wall shows: the text read there is the text drawn there.
    small = np.asarray(Image.fromarray(_screen(12, 2)).resize((900, 427), Image.LANCZOS))
    assert abs(insert_legibility.text_size(small).px - 24 * 900 / 3200) <= 1.5


def test_a_picture_with_no_text_has_none():
    rng = np.random.default_rng(6284)
    yy, xx = np.mgrid[0:1080, 0:1920]
    sky = np.stack([120 + 80 * yy / 1080, 160 + 40 * xx / 1920, 220 - 60 * yy / 1080], axis=-1) + rng.normal(
        0, 6, (1080, 1920, 3)
    )
    assert insert_legibility.text_size(np.clip(sky, 0, 255).astype(np.uint8)) is None
    assert insert_legibility.text_size(rng.integers(0, 255, (1080, 1920, 3)).astype(np.uint8)) is None
    dots = np.full((1080, 1920, 3), 240, dtype=np.uint8)
    dots[::12, ::12] = dots[1::12, ::12] = 20
    assert insert_legibility.text_size(dots) is None


# ── Each surface, judged on the 1080p frame ──────────────────────────────


def _spec(overlay: dict) -> object:
    return parse_video_spec(
        {
            "title": "Wall",
            "freestyle": True,
            "scenes": [{"id": "s1", "type": "clip", "clip_id": CLIP, "overlays": [{**overlay, "duration": 5}]}],
        }
    )


_WALL = {
    "type": "screen_insert",
    "quad": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
    "panes": [3, 3],
    "sources": [{"take_id": TAKE}] * 9,
}
_FULL = {"type": "screen_insert", "quad": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]], "source": {"take_id": TAKE}}
#: The 1600 x 760 CSS px application recorded at pixel ratio 2, its smallest text 12 CSS px.
_GEOMETRY = {
    "sources": {("take", TAKE): (3200, 1520), ("file", FILE): (1600, 760), ("clip", CLIP): (1280, 720)},
    "footage": {"s1": (1920, 1080)},
    "pixel_ratios": {TAKE: 2.0},
}
_TEXTS = {("take", TAKE): TextSize(24.0, 60), ("file", FILE): TextSize(12.0, 60), ("clip", CLIP): TextSize(6.0, 10)}


def test_a_wall_of_a_recording_is_said_pane_by_pane_and_the_same_recording_full_frame_is_not():
    wall = insert_legibility.judge(_spec(_WALL), _TEXTS, **_GEOMETRY)
    assert len(wall) == 9 and [item.pane for item in wall] == list(range(9))
    said = insert_legibility.findings(wall)
    assert len(said) == 9 and {scene for scene, _words, _severity, _surface in said} == {"s1"}
    assert said[0][3] == f"insert/s1.0.0|take:{TAKE}"
    first = wall[0]
    # A pane about 500 px wide on the 1080p frame: the recording at under 0.2x, its 12 CSS px text about 4 px.
    assert 480 <= first.quad_width <= 510 and first.scale < 0.2 and first.drawn_text < 5 and not first.readable
    _scene, words, severity, _surface = said[0]
    assert severity == "dense" and first.illegible
    assert words.startswith(
        f"its screen insert 0 (pane 1) draws recording `{TAKE}` (3200x1520 px) at 0.18x on the 1080p frame"
    )
    assert (
        "its smallest text, about 24 px in the source, reads about 4 px (advisory under 10 px, a finding under 6 px)"
        in words
    )
    # What keeps its layout: fewer panes - its row's two other panes, each asking as much, leave no larger
    # surface - never a higher browser zoom for a page whose layout nobody probed: it lays the page out as
    # narrow as a smaller window would. Drawn under 0.3x, the pane shows its page whole and a push would crop it:
    # none offered, said why, and scenery is offered ambient.
    assert "of the 1080p frame's width (" in words and ": fewer panes on the wall)" in words
    assert "a larger surface" not in words
    # A pane needing more than its whole wall: fewer panes on a larger wall.
    narrow = {
        **_WALL,
        "quad": [[0.3, 0.3], [0.7, 0.3], [0.7, 0.6], [0.3, 0.6]],
        "panes": 3,
        "sources": [{"take_id": TAKE}] * 3,
    }
    [pane, *_rest] = insert_legibility.judge(_spec({**narrow, "fit": "contain"}), _TEXTS, **_GEOMETRY)
    assert pane.needed_width > pane.quad_width + pane.panes_width and ": fewer panes on a larger wall)" in pane.fix()
    assert "push in on it" not in words
    assert "no push: drawn at 0.18x, under 0.3x, it shows as a whole page and a push would crop it" in words
    assert words.endswith(insert_legibility.AMBIENT_OFFER)
    assert "browser zoom" not in words and "viewport" not in words
    # A pane under 40% of the frame's width of a page that keeps its layout: the browser zoom to record at,
    # never a viewport that narrow - the one that brings its 4 px to 12 px at the same pane.
    held = dataclasses.replace(first, layout={"holds_down_to": 400, "changes_at": None, "why": None}).fix()
    zoom = int(held.split("browser zoom of ")[1].split("%")[0])
    assert 250 <= zoom <= 320 and zoom % 10 == 0 and "viewport" not in held
    # Full frame, the same recording reads about 17 px: nothing to say.
    [full] = insert_legibility.judge(_spec(_FULL), _TEXTS, **_GEOMETRY)
    assert full.readable and 16 <= full.drawn_text <= 18 and insert_legibility.findings([full]) == []


def test_a_file_is_told_the_viewport_its_own_width_stands_for_and_an_ai_shot_is_not_judged():
    on_file = {**_FULL, "quad": [[0.3, 0.3], [0.7, 0.3], [0.7, 0.7], [0.3, 0.7]], "source": {"file_id": FILE}}
    [item] = insert_legibility.judge(_spec(on_file), _TEXTS, **_GEOMETRY)
    assert item.viewport is None and not item.readable
    assert "push in on it with zoom" in item.message() and "viewport" not in item.message()
    assert "smaller window" not in item.message() and "fewer panes" not in item.message()
    on_shot = {**on_file, "source": {"clip_id": CLIP}}
    assert insert_legibility.judge(_spec(on_shot), _TEXTS, **_GEOMETRY) == []
    # Unknown size or unread text: not judged.
    assert insert_legibility.judge(_spec(on_file), {}, **_GEOMETRY) == []
    assert insert_legibility.judge(_spec(on_file), _TEXTS, sources={}, footage={}, pixel_ratios={}) == []


def test_text_under_6_px_is_a_finding_and_under_10_px_advice():
    def found(overlay: dict, texts: dict) -> list:
        spec = _spec(overlay)
        document = timeline.plan(
            spec, timeline.PlanMedia(clips={"s1": timeline.ClipMedia(8.0, 1920, 1080, 24.0, 192)}), fps=30, theme={}
        ).document
        facts = quality.ContentFacts(
            shots={CLIP: quality.ShotFacts("Wall", JOB_READY, width=1920, height=1080)},
            ready_takes={TAKE},
            legibility=insert_legibility.judge(spec, texts, **_GEOMETRY),
        )
        return [f for f in quality.content_findings(spec, document, facts, None) if f.kind == "insert_legibility"]

    # A 3x3 wall draws its 24 px text about 4 px: no viewer reads it - a finding the render waits for.
    wall = found(_WALL, _TEXTS)
    assert len(wall) == 9 and {f.severity for f in wall} == {"dense"} and {f.group for f in wall} == {"content"}
    assert {f.severity for f in wall} <= set(render_checks.BLOCKING_SEVERITIES)
    # Full frame with 12 px text in the source, about 8 px on the frame: read with difficulty - advice, never a block.
    small = {**_TEXTS, ("take", TAKE): TextSize(12.0, 60)}
    [advice] = found(_FULL, small)
    assert advice.severity == "advisory" and advice.severity not in render_checks.BLOCKING_SEVERITIES
    # What brings it to 12 px and keeps the page's layout: the viewport only for a page the recorder saw keep
    # its layout that narrow.
    assert "push in on it with zoom" in advice.message and "viewport" not in advice.message
    [held] = insert_legibility.judge(_spec(_FULL), small, **_GEOMETRY, layouts={TAKE: {"holds_down_to": 640}})
    viewport = int(held.fix().split("record it again with a viewport about ")[1].split(" ")[0])
    # Covering the frame at 0.71x, its 8.5 px read 12 px from a window about 1136 px wide.
    assert 1120 <= viewport <= 1150


async def test_measure_reads_each_source_once_and_skips_what_it_cannot_read(monkeypatch):
    spec = _spec(_WALL)
    reads: list[tuple[str, bool, float]] = []

    async def geometry(_db, _spec):
        return render_checks.InsertGeometry(_GEOMETRY["sources"], _GEOMETRY["footage"], _GEOMETRY["pixel_ratios"], {})

    async def file_of(_db, kind, ref):
        return ("video-file", False) if (kind, ref) == ("take", TAKE) else None

    async def read(file_id, picture, seconds, _workdir):
        reads.append((file_id, picture, seconds))
        return TextSize(24.0, 60)

    monkeypatch.setattr(render_checks, "insert_geometry", geometry)
    monkeypatch.setattr(insert_legibility, "_file_of", file_of)
    monkeypatch.setattr(insert_legibility, "_read", read)
    measured = await insert_legibility.measure(None, spec)
    # Nine panes, one source: read once, a second into it.
    assert reads == [("video-file", False, 1.0)] and len(measured) == 9
    # A source that cannot be read leaves the save as it was.

    async def broken(*_args):
        raise OSError("decoder gone")

    monkeypatch.setattr(insert_legibility, "_read", broken)
    assert await insert_legibility.measure(None, spec) == []
    # A video showing no recording nor file reads nothing.
    on_shot = _spec({**_FULL, "source": {"clip_id": CLIP}})
    assert await insert_legibility.measure(None, on_shot) == []


def test_the_soft_source_gate_reads_the_same_quad_on_the_frame():
    quad = [(0.2, 0.2), (0.7, 0.2), (0.7, 0.7), (0.2, 0.7)]
    assert insert_checks.quad_on_frame(quad, (1920, 1080), (1920, 1080)) == pytest.approx((960.0, 540.0))
    # A 4:3 footage covers a 16:9 frame: drawn wider than the frame's share.
    width, height = insert_checks.quad_on_frame(quad, (1440, 1080), (1920, 1080))
    assert width == pytest.approx(960.0) and height == pytest.approx(720.0)
    # Unknown footage: the frame's own size.
    assert insert_checks.quad_on_frame(quad, None, (3840, 2160)) == pytest.approx((1920.0, 1080.0))


def test_a_legibility_names_its_numbers():
    item = Legibility(
        timeline.insert_key("s1", 0), ("take", TAKE), (3200, 1520), 960.0, 0.3, TextSize(24.0, 40), viewport=1600.0
    )
    assert item.drawn_text == pytest.approx(7.2) and not item.readable and not item.illegible
    assert "of the 1080p frame's width (83% of it: a larger surface)" in item.fix()
    assert "push in on it with zoom {start, end, scale: 1.7}" in item.fix()
    # No layout probed: no narrower viewport.
    assert "viewport" not in item.fix()
    # Half the frame's width: a viewport that much narrower still fits a screen of its shape - the one that draws its
    # 7.2 px at 12 px on the same quad - for a page the recorder saw keep its layout down to 640 px; one that
    # stacks at 1200 px is never told it.
    held = dataclasses.replace(item, layout={"holds_down_to": 640, "changes_at": None, "why": None})
    assert "record it again with a viewport about 960 px wide (its layout holds down to 640 px" in held.fix()
    stacks = dataclasses.replace(
        item, layout={"holds_down_to": 1210, "changes_at": 1200, "why": "blocks it shows side by side stack"}
    )
    assert "record it again" not in stacks.fix()
    assert stacks.fix().endswith(
        "; a narrower viewport would change its layout (blocks it shows side by side stack at 1200 px); "
        + insert_legibility.AMBIENT_OFFER
    )


# ── Scenery, and the browser zoom a narrow recording is recorded at ──────


def _monitors(**marked) -> object:
    """Three monitors sharing one frame, each showing the recording on a quad 35% of the frame wide: at 0.21x."""
    overlays = [
        {
            "type": "screen_insert",
            "quad": [[x, 0.3], [x + 0.35, 0.3], [x + 0.35, 0.58], [x, 0.58]],
            "source": {"take_id": TAKE},
            "duration": 5,
            **marked,
        }
        for x in (0.0, 0.33, 0.66)
    ]
    return parse_video_spec(
        {
            "title": "Ops room",
            "freestyle": True,
            "scenes": [{"id": "s1", "type": "clip", "clip_id": CLIP, "overlays": overlays}],
        }
    )


def _found(spec, measured) -> list:
    document = timeline.plan(
        spec, timeline.PlanMedia(clips={"s1": timeline.ClipMedia(8.0, 1920, 1080, 24.0, 192)}), fps=30, theme={}
    ).document
    facts = quality.ContentFacts(
        shots={CLIP: quality.ShotFacts("Ops room", JOB_READY, width=1920, height=1080)},
        ready_takes={TAKE},
        legibility=measured,
    )
    return [f for f in quality.content_findings(spec, document, facts, None) if f.kind == "insert_legibility"]


def test_three_monitors_marked_ambient_are_said_with_their_text_size_and_judged_as_recordings():
    spec = _monitors(legibility="ambient")
    measured = insert_legibility.judge(spec, _TEXTS, **_GEOMETRY)
    # Measured and said: the author still reads the number; the legibility check itself never judges scenery.
    assert len(measured) == 3 and all(item.ambient for item in measured)
    assert insert_legibility.findings(measured) == []
    # Issue #10093: three monitors sharing one frame at 0.21x are the scenery they are declared - nothing is listed.
    assert _found(spec, measured) == []
    said = insert_legibility.unchecked(spec, measured)
    fact = "its smallest text, about 24 px in the source, reads about 5 px on the 1080p frame (drawn at 0.21x)"
    assert said == [("s1", f"its screen insert {index}: ambient, not judged - {fact}") for index in range(3)]
    report = quality.QualityReport(1, unchecked=tuple(f"scene `{sid}` {words}" for sid, words in said))
    text = quality.format_quality_report(report)
    assert text.startswith("Quality check: in the 1 scene, nothing covers anything")
    assert (
        f"Not judged, as the video declares (no finding):\n- scene `s1` its screen insert 0: ambient, not judged - {fact}"
        in text
    )
    assert quality.QualityReport.from_dict(report.as_dict()) == report
    # A source that could not be read is said as such, never as a size.
    assert insert_legibility.unchecked(spec)[0] == (
        "s1",
        "its screen insert 0: ambient, not judged (its text not read)",
    )


def test_the_same_monitors_unmarked_are_each_told_the_browser_zoom_to_record_at():
    # The page keeps its layout down to 300 px: a zoom laying it out that narrow keeps it.
    held = {TAKE: {"holds_down_to": 300, "changes_at": None, "why": None}}
    for marked in ({}, {"legibility": "legible"}):
        spec = _monitors(**marked)
        measured = insert_legibility.judge(spec, _TEXTS, **_GEOMETRY, layouts=held)
        assert [round(item.scale, 2) for item in measured] == [0.21] * 3
        # About 5 px on the frame: under 6 px, a finding the render waits for.
        found = _found(spec, measured)
        assert len(found) == 3 and {f.severity for f in found} == {"dense"}
        for finding in found:
            assert (
                "record it again with a browser zoom of 240% (browser_zoom: 240), its window and frame keeping"
                in finding.message
            )
            assert "largest safe zoom 500 on this page (its layout holds down to 300 px" in finding.message
        assert insert_legibility.unchecked(spec, measured) == []
    # Each asks 83% of the frame beside two others asking as much: no surface is offered, said why.
    [first, *_rest] = insert_legibility.judge(_monitors(), _TEXTS, **_GEOMETRY, layouts=held)
    assert first.crowded and first.inserts_beside == 2 and "give it about" not in first.fix()
    assert first.fix().startswith(
        "the scene has no such surface for it: about 1600 px of the 1080p frame's width (83% of it) beside the 2 other "
        "inserts side by side with it (167% of it, each at the size it asks), 250% together; "
    )
    assert "or a tighter page: a single panel of it" in first.fix()
    # Recorded at 250% already: told from where it stands - the largest zoom its page keeps its layout at, and the
    # text size it brings.
    [first, *_rest] = insert_legibility.judge(_monitors(), _TEXTS, **_GEOMETRY, browser_zooms={TAKE: 250}, layouts=held)
    assert (
        "a browser zoom of 500% (browser_zoom: 500)" in first.fix()
        and "its smallest text then about 10 px" in first.fix()
    )
    # A page whose layout stacks at 900 px is never told a zoom that lays it out at 667 px: the largest it keeps.
    stacks = {TAKE: {"holds_down_to": 910, "changes_at": 900, "why": "blocks it shows side by side stack"}}
    [stacked, *_rest] = insert_legibility.judge(_monitors(), _TEXTS, **_GEOMETRY, layouts=stacks)
    assert "record it again with a browser zoom of 170% (browser_zoom: 170)" in stacked.fix()
    assert "largest safe zoom 170 on this page (its layout holds down to 910 px" in stacked.fix()
    assert (
        "; a browser zoom of 240% would change its layout (blocks it shows side by side stack at 900 px);"
        in stacked.fix()
    )
    # Never probed: no zoom; drawn at 0.21x, no push either - it would crop the page -, a tighter page.
    [unprobed, *_rest] = insert_legibility.judge(_monitors(), _TEXTS, **_GEOMETRY)
    assert "record it again with a browser zoom" not in unprobed.fix() and "push in on it" not in unprobed.fix()
    assert "no browser zoom offered: how far down its page keeps its layout was not probed" in unprobed.fix()
    assert "a tighter page" in unprobed.fix() and "no push: drawn at 0.21x" in unprobed.fix()


async def test_an_ambient_insert_is_read_and_its_scenery_honoured(monkeypatch):
    reads: list = []

    async def geometry(_db, _spec):
        return render_checks.InsertGeometry(_GEOMETRY["sources"], _GEOMETRY["footage"], _GEOMETRY["pixel_ratios"], {})

    async def file_of(_db, _kind, _ref):
        return ("video-file", False)

    async def read(*args):
        reads.append(args)
        return TextSize(24.0, 60)

    monkeypatch.setattr(render_checks, "insert_geometry", geometry)
    monkeypatch.setattr(insert_legibility, "_file_of", file_of)
    monkeypatch.setattr(insert_legibility, "_read", read)
    # Scenery is read like any: its size is said, never judged by the legibility check itself.
    spec = _monitors(legibility="ambient")
    measured = await insert_legibility.measure(None, spec)
    assert len(measured) == 3 and all(item.ambient for item in measured) and len(reads) == 1
    assert insert_legibility.findings(measured) == []
    # Issue #10093: declared scenery sharing its frame is honoured - no finding, no advice.
    assert _found(spec, measured) == []
    assert len(await insert_legibility.measure(None, _monitors())) == 3 and len(reads) == 2


# ── The surface a scene can give ──────────────────────────────────


def _pair(*boxes: tuple[float, float, float, float], starts=None, sources=None, **marked) -> object:
    """One insert per ``(left, top, right, bottom)`` box (0-1 of the footage), each showing its entry of *sources*
    (the 3200x1800 recording) from its entry of *starts* (0 s)."""
    overlays = [
        {
            "type": "screen_insert",
            "quad": [[left, top], [right, top], [right, bottom], [left, bottom]],
            "source": (sources or [{"take_id": TAKE}] * len(boxes))[index],
            "start": (starts or [0.0] * len(boxes))[index],
            "duration": 5,
            **marked,
        }
        for index, (left, top, right, bottom) in enumerate(boxes)
    ]
    return parse_video_spec(
        {
            "title": "Wall",
            "freestyle": True,
            "scenes": [{"id": "s1", "type": "clip", "clip_id": CLIP, "overlays": overlays}],
        }
    )


#: The 1600x900 window recorded at pixel ratio 2, its smallest text 14 CSS px, its layout kept down to 1066 px.
_PAIR_GEOMETRY = {**_GEOMETRY, "sources": {("take", TAKE): (3200, 1800)}}
_PAIR_TEXT = {("take", TAKE): TextSize(28.0, 60)}
_PAIR_LAYOUT = {TAKE: {"holds_down_to": 1066, "changes_at": 1060, "why": "blocks it shows side by side stack"}}
_SIDE_BY_SIDE = ((0.05, 0.3, 0.46, 0.7), (0.54, 0.3, 0.95, 0.7))


def test_two_inserts_sharing_the_frames_width_are_offered_the_largest_safe_zoom_never_a_surface_they_cannot_have():
    spec = _pair(*_SIDE_BY_SIDE)
    pair = insert_legibility.judge(spec, _PAIR_TEXT, **_PAIR_GEOMETRY, layouts=_PAIR_LAYOUT)
    assert len(pair) == 2
    for item in pair:
        # Each 41% of the frame wide, the recording at 0.25x: its 28 px text about 7 px, advice.
        assert round(item.quad_width / item.frame_width, 2) == 0.41 and round(item.scale, 2) == 0.25
        assert not item.readable and not item.illegible and item.largest_zoom == 150
        assert item.crowded and item.inserts_beside == 1
    words = _found(spec, pair)[0].message
    # 71% each, 143% together: the 68%-style surface is never offered, the scene saying why.
    assert "give it about" not in words and "a larger surface" not in words
    assert "the scene has no such surface for it: about 1372 px of the 1080p frame's width (71% of it)" in words
    assert "143% together" in words
    # What needs no more of the frame: the largest zoom its page keeps its layout at, or a tighter page.
    assert "record it again with a browser zoom of 150% (browser_zoom: 150)" in words
    assert "largest safe zoom 150 on this page (its layout holds down to 1066 px" in words
    assert "its smallest text then about 10 px" in words and "or a tighter page: a single panel of it" in words
    assert "a browser zoom of 180% would change its layout (blocks it shows side by side stack at 1060 px)" in words
    # Declared ambient, the same pair is no finding, its smallest text said as a fact.
    scenery = _pair(*_SIDE_BY_SIDE, legibility="ambient")
    measured = insert_legibility.judge(scenery, _PAIR_TEXT, **_PAIR_GEOMETRY, layouts=_PAIR_LAYOUT)
    assert _found(scenery, measured) == []
    assert insert_legibility.unchecked(scenery, measured)[0] == (
        "s1",
        "its screen insert 0: ambient, not judged - its smallest text, about 28 px in the source, reads about 7 px on "
        "the 1080p frame (drawn at 0.25x)",
    )


def test_inserts_that_do_not_share_the_frames_width_are_each_offered_their_surface():
    # One above the other, or one after the other: each has the frame's width to itself.
    stacked = _pair((0.05, 0.05, 0.46, 0.45), (0.05, 0.55, 0.46, 0.95))
    timed = _pair(*_SIDE_BY_SIDE, starts=[0.0, 5.0])
    for spec in (stacked, timed):
        for item in insert_legibility.judge(spec, _PAIR_TEXT, **_PAIR_GEOMETRY, layouts=_PAIR_LAYOUT):
            assert not item.crowded and item.inserts_beside == 0
            assert "give it about 1372 px of the 1080p frame's width (71% of it: a larger surface)" in item.fix()
    # A readable insert beside one too small takes the width it has, no more: 71% + 25% fits, the surface is offered.
    readable = _pair(_SIDE_BY_SIDE[0], (0.7, 0.3, 0.95, 0.7), sources=[{"take_id": TAKE}, {"file_id": FILE}])
    texts = {**_PAIR_TEXT, ("file", FILE): TextSize(100.0, 60)}
    geometry = {**_PAIR_GEOMETRY, "sources": {("take", TAKE): (3200, 1800), ("file", FILE): (3200, 1800)}}
    small, large = insert_legibility.judge(readable, texts, **geometry, layouts=_PAIR_LAYOUT)
    assert large.readable and small.inserts_beside == 1 and not small.crowded
    assert "give it about 1372 px of the 1080p frame's width (71% of it: a larger surface)" in small.fix()


def test_a_quads_box_on_the_frame_is_where_the_soft_gate_places_it():
    quad = [(0.2, 0.25), (0.7, 0.2), (0.7, 0.7), (0.2, 0.75)]
    assert insert_checks.box_on_frame(quad, (1920, 1080), (1920, 1080)) == pytest.approx((384.0, 216.0, 1344.0, 810.0))
    # A 4:3 footage covers a 16:9 frame: placed as wide as the quad's own size says.
    left, _top, right, _bottom = insert_checks.box_on_frame(quad, (1440, 1080), (1920, 1080))
    assert right - left == pytest.approx(
        insert_checks.quad_on_frame([(0.2, 0.2), (0.7, 0.2), (0.7, 0.7), (0.2, 0.7)], (1440, 1080), (1920, 1080))[0]
    )


def test_legibility_is_a_screen_inserts_and_never_changes_what_its_scene_draws():
    from app.services.video_studio.spec import VideoSpecError, validate_video_spec

    def video(**marked) -> dict:
        return validate_video_spec(
            {
                "title": "Ops room",
                "freestyle": True,
                "scenes": [
                    {
                        "id": "s1",
                        "type": "clip",
                        "clip_id": CLIP,
                        "overlays": [{**_FULL, "duration": 5, **marked}, {"type": "callout", "text": "Live", **marked}],
                    }
                ],
            }
        )

    marked = video(legibility="ambient")
    assert marked["scenes"][0]["overlays"][0]["legibility"] == "ambient"
    # A callout takes none: dropped like every field of another type.
    assert "legibility" not in marked["scenes"][0]["overlays"][1]
    with pytest.raises(VideoSpecError):
        video(legibility="blurry")

    class _NoMedia:
        async def get(self, *_args, **_kwargs):
            return None

    # How an insert is judged is no part of its drawing: the scene's passing preview still counts.
    drawn = asyncio.run(render_checks.digest_scenes(_NoMedia(), video()))
    assert asyncio.run(render_checks.digest_scenes(_NoMedia(), marked)) == drawn
    assert asyncio.run(render_checks.digest_scenes(_NoMedia(), video(glow=0.4))).whole != drawn.whole


def test_a_browser_zoom_lays_the_page_out_as_a_zoomed_browser_on_the_same_frame():
    from app.services.video_studio.spec import VideoSpecError, validate_take_script, zoomed_window

    css, ratio = zoomed_window((1600, 900), 2, 280)
    assert css == (571, 321) and round(css[0] * ratio) == 3200 and round(css[1] * ratio) == 1799
    assert zoomed_window((1600, 900), 2, 100) == ((1600, 900), 2.0)
    script = validate_take_script({"steps": [{"action": "wait", "seconds": 1}], "browser_zoom": 280})
    assert script["browser_zoom"] == 280
    assert "browser_zoom" not in validate_take_script({"steps": [{"action": "wait", "seconds": 1}]})
    for wrong in (50, 501):
        with pytest.raises(VideoSpecError, match="browser_zoom"):
            validate_take_script({"steps": [{"action": "wait", "seconds": 1}], "browser_zoom": wrong})
