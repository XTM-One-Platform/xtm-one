"""Two notes a green high render left the administrator that its MP4 contradicts.

1. "insert flickers at t=0:30.33 ... not drawn on frame 1820 (357 frames without it)" on the insert of a statement over
   a clip, while the presence gate read its source present on all 467 encoded frames and the MP4 shows it drawn there,
   blended with the scene before (a crossfade frame): the note was raised from the page log - the frame server's
   audits, ``insert_checks.missing_frames`` -, never from the encoded frame.
2. "insert missing at t=0:50.80 ... SSIM 0.29 under 0.3 against the source frame" on 47 of 806 frames of a statement
   over a picture, the insert drawn on its wall display and the statement's three-line headline over the quad.

Synthetic fixtures only but the last test - no provider, no ffmpeg. Each encoded frame is composed as the page paints
it - the statement's backdrop dimmed, its insert on the wall display, its lines of bright letters -, its crossfade in
blended as the page blends it, then put through a lossy encode; the presence gate (``insert_checks.PresenceGate``)
reads them as a render reads its own MP4:

* a statement over a clip whose page log misses its insert on 300 frames - a crossfade frame among them - that every
  encoded frame shows it on: no finding, no note for the administrator;
* the same with the insert really gone from 3 encoded frames the page log misses: one note, ``insert flickers``, naming
  those 3; gone from 3 frames the page log has: one ``insert missing`` instead; gone from a crossfade frame the page log
  misses: never ``insert flickers``;
* a statement over a picture whose headline covers 55% of the quad over a drawn insert: present on every frame, no
  note - read on what the headline leaves of it; without the page's boxes (an older segment) the same frames
  read missing; the insert removed, ``insert missing`` on every frame it shows whole on; the headline over 70% of the
  quad, not judged;
* the frame server keeping the page's boxes on a presence frame, handed on as the audits' ``over`` and merged;
* in a real Chromium, the page's ``insertRects`` naming the boxes of a statement's lines and chips over its insert.
"""

from __future__ import annotations

import io
import json
import math
import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.services.video_studio import compositor, insert_checks, render_answer, render_checks, render_ranges, timeline
from app.services.video_studio.frame_owners import FrameOwners
from app.services.video_studio.render_checks import INSERT_FLICKER, INSERT_MISSING
from app.services.video_studio.screen_insert import Footage, InsertSource

W, H = 640, 360
FPS = 24
#: The wall display the insert is drawn on, 0-1 of the frame (the backdrop fills it).
QUAD = [[0.3, 0.3], [0.7, 0.3], [0.7, 0.78], [0.3, 0.78]]
BOX = [0.3, 0.3, 0.7, 0.78]
KEY = "insert/stat.0"
#: The statement over a clip comes in by a crossfade of FADE s at START s and plays LENGTH s.
START, FADE, LENGTH = 2.0, 0.5, 14.0
#: The wall display as the footage has it: switched off.
SCREEN = (24, 26, 30)
X0, Y0, X1, Y1 = round(BOX[0] * W), round(BOX[1] * H), round(BOX[2] * W), round(BOX[3] * H)


def _ui(seed: int, size: tuple[int, int]) -> Image.Image:
    """A page of a product: a header, panels and lines of text, its detail everywhere."""
    rng = np.random.default_rng(seed)
    picture = Image.new("RGB", size, (18, 24, 36))
    draw = ImageDraw.Draw(picture)
    w, h = size
    draw.rectangle([0, 0, w, h // 10], fill=(40, 180, 200))
    for _ in range(14):
        x, y = int(rng.integers(0, w - 60)), int(rng.integers(h // 8, h - 40))
        fill = tuple(int(v) for v in rng.integers(40, 230, 3))
        draw.rectangle([x, y, x + int(rng.integers(40, 160)), y + int(rng.integers(20, 90))], fill=fill)
    for row in range(h // 8, h, 14):
        draw.line([(w // 20, row), (w // 20 + int(rng.integers(w // 4, w - w // 10)), row)], fill=(220, 220, 220), width=2)
    return picture


#: The insert's source, at its quad's size: what the page draws on the wall display.
SOURCE = _ui(1, (X1 - X0, Y1 - Y0))
WALL = np.random.default_rng(7).integers(30, 140, (H, W, 3), dtype=np.uint8)
RECORDING = np.asarray(_ui(2, (W, H)), dtype=np.float32)


def _lines(headline: float | None) -> list[tuple[float, float, float, float]]:
    """The statement's lines, in pixels: three across the quad covering *headline* of its height, or one at the top of
    the frame, clear of it."""
    if headline is None:
        return [(0.15 * W, 0.06 * H, 0.85 * W, 0.16 * H)]
    band = headline * (Y1 - Y0) / 3
    gap = (Y1 - Y0 - 3 * band) / 4
    return [(0.15 * W, Y0 + gap * (k + 1) + band * k, 0.85 * W, Y0 + gap * (k + 1) + band * (k + 1)) for k in range(3)]


def _over(headline: float | None) -> list[list[float]]:
    """What the page's ``insertRects`` says its scene draws over the insert, kept 0-1 of the frame (``FrameServer.place``)."""
    return [[round(a / W, 5), round(b / H, 5), round(c / W, 5), round(d / H, 5)] for a, b, c, d in _lines(headline) if b < Y1 and d > Y0]


def _statement(*, shown: bool = True, headline: float | None = None) -> np.ndarray:
    """The statement's own frame: its backdrop dimmed, its insert on the wall display - or the display as the footage
    has it -, its lines of bright letters over it (:func:`_lines`)."""
    rgb = WALL.astype(np.float32) * 0.7
    rgb[Y0:Y1, X0:X1] = np.asarray(SOURCE, dtype=np.float32) * 0.85 if shown else SCREEN
    picture = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))
    draw = ImageDraw.Draw(picture)
    for left, top, right, bottom in _lines(headline):
        step, tall = (bottom - top) * 0.62, bottom - top
        x = left
        while x < right - step:
            draw.rectangle((x, top, x + step * 0.8, bottom), fill=(244, 246, 252))
            draw.rectangle((x + step * 0.25, top + tall * 0.25, x + step * 0.55, bottom - tall * 0.25), fill=(12, 14, 24))
            x += step
    return np.asarray(picture, dtype=np.float32)


def _encoded(rgb: np.ndarray) -> np.ndarray:
    buffer = io.BytesIO()
    Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8)).save(buffer, format="JPEG", quality=90)
    return np.asarray(Image.open(io.BytesIO(buffer.getvalue())).convert("RGB"))


def _in_out(p: float) -> float:
    """The page's ``move`` easing: a crossfade's incoming opacity at its progress *p* (``applyTransition``)."""
    return 4 * p**3 if p < 0.5 else 1 - (-2 * p + 2) ** 3 / 2


def _document(*, backdrop: str = "video", after: str | None = "rec", length: float = LENGTH) -> dict:
    """A statement over a clip (*backdrop* ``video``) or a picture, its insert on the wall display to its end - after a
    full-screen recording it crossfades from (*after*), or alone - planned as ``timeline.plan`` rounds it."""
    start = START if after else 0.0
    insert = {"src": KEY, "quad": QUAD, "t0": 0.0, "t1": length, "to_end": True}
    stat = {
        "id": "stat",
        "type": "statement",
        "start": start,
        "end": round(start + length, 4),
        "transition_in": {"type": "crossfade" if after else "cut", "duration": FADE if after else 0.0},
        "lines": ["Every alert", "triaged", "first"],
        "backdrop": {"src": "backdrop/stat", "kind": backdrop},
        "inserts": [insert],
    }
    scenes = [stat]
    if after:
        scenes.insert(0, {"id": after, "type": "recording", "start": 0.0, "end": round(START + FADE, 4), "transition_in": {"type": "cut", "duration": 0.0}})
    duration = scenes[-1]["end"]
    return {"fps": FPS, "frames": math.ceil(duration * FPS), "duration": duration, "fade_in": 0.0, "fade_out": 0.0, "scenes": scenes}


def _inserts(backdrop: str = "video") -> dict[str, InsertSource]:
    footage = Footage("backdrop.mp4", "video", float(FPS), "backdrop/stat") if backdrop == "video" else Footage("wall.png", "image", key="backdrop/stat")
    return {KEY: InsertSource("stat.png", "image", tuple(tuple(p) for p in QUAD), footage=footage, label="Scene stat: its screen insert 0")}


class _Render:
    """A statement's frames as the page paints them, the frame server logs them and the encoder keeps them: its insert's
    layer audited on every frame it shows any of (the page log) but those *withheld*, placed where ``insertRects``
    places it with how much of it shows and what its scene draws over it (*over*), its scene painted black and white on
    a crossfade's frame (``FrameServer.keep_blend``) - and the insert gone from the encoded frames *gone*."""

    def __init__(
        self,
        document: dict,
        folder: Path,
        *,
        withheld: set[int] = frozenset(),
        gone: set[int] = frozenset(),
        headline: float | None = None,
        boxes: bool = True,
    ) -> None:
        self.document = document
        self.owners = FrameOwners.of(document)
        self.index = len(document["scenes"]) - 1
        stat = document["scenes"][self.index]
        drawn, removed = _statement(headline=headline), _statement(shown=False, headline=headline)
        whole = {True: _encoded(drawn), False: _encoded(removed)}
        self.pictures: dict[int, np.ndarray] = {}
        frames, placed, blend, over = {}, {}, {}, {}
        seconds = float(stat["transition_in"]["duration"])
        for n in self.owners.frames_of(self.index):
            p = min(max((n / FPS - stat["start"]) / seconds, 0.0), 1.0) if seconds > 0 else 1.0
            shown = round(_in_out(p), 3)
            placed[n] = [*BOX, shown]
            if shown > 0 and n not in withheld:
                frames[n] = {"visibility": 1.0}
            if boxes and _over(headline):
                over[n] = _over(headline)
            own = removed if n in gone else drawn
            if shown >= 1.0:
                self.pictures[n] = whole[n not in gone]
                continue
            # The recording stays whole under the statement coming in at *shown* (the page's crossfade).
            self.pictures[n] = _encoded(RECORDING * (1.0 - shown) + own * shown)
            if insert_checks.PRESENCE_MIN_SHOWN <= shown < insert_checks.BLEND_WHOLE:
                kept = {}
                for tone, value in (("black", 0.0), ("white", 255.0)):
                    path = folder / f"blend-stat-{n:07d}-{tone}.png"
                    Image.fromarray(np.clip(RECORDING * (1.0 - shown) + value * shown, 0, 255).astype(np.uint8)).save(path)
                    kept[tone] = str(path)
                blend[n] = kept
        self.audits = {KEY: {"frames": frames, "evidence": {}, "placed": placed, "blend": blend, **({"over": over} if over else {})}}
        self.inserts = _inserts(stat["backdrop"]["kind"])
        self.placed = insert_checks.placed_inserts(document, self.inserts, owners=self.owners)

    def gate(self) -> insert_checks.PresenceGate:
        """The presence gate over every encoded frame it reads, as a render reads its own MP4."""
        gate = insert_checks.PresenceGate(self.audits, self.placed, self.document, self.inserts, fps=FPS)
        for n in sorted(gate.frames):
            gate.read(n, self.pictures[n], {need: SOURCE for need in gate.needs(n)})
        return gate

    def judged(self) -> list[int]:
        """The frames the page log judges its layer drawn on (``Placed.judged_frames``)."""
        return self.placed[KEY].judged_frames(insert_checks.placements_of(self.audits[KEY]))

    def notes(self, result: insert_checks.Presence) -> tuple[list, list]:
        """``(findings, the administrator's notes)`` of the audits' findings and the gate's, as a render parts them."""
        found = [finding for finding, _proof in insert_checks.findings(self.audits, self.placed, fps=FPS)]
        return render_checks.harness_defects([*found, *(finding for finding, _proof in result.findings)], {})


def _report(render: _Render, result: insert_checks.Presence) -> dict:
    return next(r for r in insert_checks.reports(render.audits, render.placed, fps=FPS, presence=result.by_key) if r["key"] == KEY)


def _crossfaded(render: _Render) -> list[int]:
    """The frames of the crossfade in the page log judges, blending the insert with the recording."""
    placements = insert_checks.placements_of(render.audits[KEY])
    return [n for n in render.judged() if placements[n][4] < insert_checks.BLEND_WHOLE]


# ── 1. A statement over a clip: the page log against the encoded frames ───


def test_the_page_log_missing_300_frames_every_encoded_frame_shows_the_insert_on_leaves_no_note(tmp_path):
    document = _document()
    judged = _Render(document, tmp_path).judged()
    withheld = set(judged[:300])
    render = _Render(document, tmp_path, withheld=withheld)
    crossfaded = _crossfaded(render)
    # The page log misses 300 frames its scene owns and shows the insert on - crossfade frames among them, as frame 1820.
    assert crossfaded and set(crossfaded) <= withheld
    gate = render.gate()
    assert gate.undrawn[KEY] == withheld
    result = gate.result()
    assert [finding.message for finding, _proof in result.findings] == []
    summary = result.by_key[KEY]
    # Every frame it shows whole on read present - but the crossfade's first ones, showing too little of it to judge.
    assert summary["missing"] == 0 and "undrawn" not in summary
    assert summary["frames"] + summary["faint"] == len(render.placed[KEY].whole_frames())
    assert set(gate.alphas[KEY]) == set(crossfaded), "the crossfade frames read unblended"
    # Nothing for the video, nothing for the administrator.
    assert render.notes(result) == ([], [])
    report = _report(render, result)
    assert report["missing_frames"] == 0
    assert not any("not drawn" in m.text for m in insert_checks.classify(report))


def test_the_insert_really_gone_from_3_frames_the_page_log_misses_is_one_finding_naming_them(tmp_path):
    document = _document()
    judged = _Render(document, tmp_path).judged()
    withheld = set(judged[:300])
    gone = [judged[100], judged[150], judged[200]]
    render = _Render(document, tmp_path, withheld=withheld, gone=set(gone))
    result = render.gate().result()
    kept, notes = render.notes(result)
    # Lost on more than one frame (``render_checks.HARNESS_MAX_FRAMES``): the video's, a block - one finding.
    assert notes == []
    (finding,) = kept
    assert finding.kind == INSERT_MISSING and finding.data["missed"] == gone and finding.data["absent"] == 3
    assert finding.data["frame"] == gone[0] and finding.data["span"] == [round(gone[0] / FPS, 3), round(gone[-1] / FPS, 3)]
    assert finding.data["why"] == "flat" and finding.data["gate"] == "flicker" and finding.data["action"]["kind"] == insert_checks.NOTHING
    assert finding.message.startswith(
        f"insert missing at t={render_checks.clock(gone[0] / FPS)}: Scene stat: its screen insert 0 is not drawn on frames "
        f"{gone[0]}, {gone[1]} and {gone[2]}, where it is drawn on the frames around it - its quad is one flat colour"
    )
    assert "(3 frames without it)" in finding.message
    # The report counts what the encoded frames read, in its flicker fact too.
    report = _report(render, result)
    assert report["missing_frames"] == 3 and report["flicker_frames"] == 3
    assert report["presence"]["missing"] == 3 and report["presence"]["undrawn"] == 3
    answer = render_answer.of_checks(render_checks.Checks(findings=kept, harness=notes).as_dict())
    assert answer.status != render_answer.GREEN and answer.infra_once == []


def test_the_insert_really_gone_from_1_frame_the_page_log_misses_is_the_harness_s_note(tmp_path):
    document = _document()
    judged = _Render(document, tmp_path).judged()
    gone = judged[150]
    render = _Render(document, tmp_path, withheld=set(judged[:300]), gone={gone})
    kept, notes = render.notes(render.gate().result())
    assert kept == []
    (note,) = notes
    assert note.kind == INSERT_FLICKER and note.data["missed"] == [gone] and note.data["absent"] == 1
    assert note.message.startswith(f"insert flickers at t={render_checks.clock(gone / FPS)}: Scene stat: its screen insert 0 is not drawn on frame {gone},")
    # Said once to the administrator, never a finding of the video.
    answer = render_answer.of_checks(render_checks.Checks(findings=kept, harness=notes).as_dict())
    assert answer.status == render_answer.GREEN and len(answer.infra_once) == 1


def test_the_insert_gone_from_3_frames_the_page_log_has_is_one_insert_missing(tmp_path):
    document = _document()
    judged = _Render(document, tmp_path).judged()
    gone = judged[-3:]
    render = _Render(document, tmp_path, withheld=set(judged[:300]), gone=set(gone))
    kept, notes = render.notes(render.gate().result())
    assert notes == []
    (finding,) = kept
    assert finding.kind == INSERT_MISSING and finding.data["gate"] == "presence" and finding.data["frame"] == gone[0] and finding.data["frames"] == 3


def test_a_crossfade_frame_the_page_log_misses_and_the_insert_is_gone_from_is_never_a_flicker_frame(tmp_path):
    document = _document()
    first = _Render(document, tmp_path)
    crossfaded = _crossfaded(first)
    judged = first.judged()
    render = _Render(document, tmp_path, withheld=set(judged[:300]), gone={crossfaded[0]})
    gate = render.gate()
    assert crossfaded[0] in gate.undrawn[KEY]
    result = gate.result()
    assert not any(finding.kind == INSERT_FLICKER for finding, _proof in result.findings)
    assert "undrawn" not in result.by_key[KEY]


# ── 2. A statement over a picture: its text over the quad ─────────────────


def test_a_headline_over_55_percent_of_the_quad_leaves_a_drawn_insert_present_on_every_frame(tmp_path):
    render = _Render(_document(backdrop="image", after=None, length=3.0), tmp_path, headline=0.55)
    gate = render.gate()
    result = gate.result()
    assert [finding.message for finding, _proof in result.findings] == []
    summary = result.by_key[KEY]
    whole = render.placed[KEY].whole_frames()
    assert summary["frames"] == len(whole) == len(gate.read_frames[KEY]) and summary["missing"] == 0 and "covered" not in summary
    assert render.notes(result) == ([], [])


def test_without_the_page_s_boxes_the_same_headline_reads_a_drawn_insert_missing(tmp_path):
    """The report: the headline over the quad lowers its similarity to the bare source under the threshold."""
    render = _Render(_document(backdrop="image", after=None, length=3.0), tmp_path, headline=0.55, boxes=False)
    result = render.gate().result()
    ((finding, _proof),) = result.findings
    assert finding.kind == INSERT_MISSING and finding.data["why"] == "absent"
    assert finding.data["value"] < insert_checks.PRESENCE_SSIM_MIN == 0.3


def test_the_insert_removed_under_the_same_headline_is_missing_on_every_frame_it_shows_whole_on(tmp_path):
    document = _document(backdrop="image", after=None, length=3.0)
    whole = _Render(document, tmp_path).placed[KEY].whole_frames()
    render = _Render(document, tmp_path, headline=0.55, gone=set(whole))
    ((finding, _proof),) = render.gate().result().findings
    assert finding.kind == INSERT_MISSING and finding.data["frames"] == finding.data["measured"] == len(whole)
    assert finding.data["frame"] == whole[0]


def test_a_headline_over_70_percent_of_the_quad_leaves_the_frame_not_judged(tmp_path):
    document = _document(backdrop="image", after=None, length=3.0)
    whole = _Render(document, tmp_path).placed[KEY].whole_frames()
    render = _Render(document, tmp_path, headline=0.7, gone=set(whole))
    result = render.gate().result()
    assert result.findings == []
    summary = result.by_key[KEY]
    assert summary["covered"] == len(whole) and summary["frames"] == 0
    report = _report(render, result)
    said = next(m.text for m in insert_checks.classify(report) if m.text.startswith("its source present on all"))
    assert f"{len(whole)} frame(s) with what its scene draws over it (its text, chips, subtitles) on more than 60% of its quad, not judged" in said


# ── The frame server, and the page ────────────────────────────────────────


def test_the_frame_server_keeps_what_the_scene_draws_over_an_insert_on_a_presence_frame(tmp_path):
    insert = _inserts("image")[KEY]
    server = compositor.FrameServer(
        compositor.CompositorAssets(inserts={KEY: insert}), "<html></html>", presence={10: [KEY]}, guard={11: [KEY]}
    )
    stage = (1920.0, 1080.0)
    text = [[480.0, 324.0, 960.0, 120.0], [600.0, 480.0, 720.0, 120.0]]
    server.place(10, {KEY: [576.0, 324.0, 768.0, 518.4, 1.0, text]}, stage)
    server.place(11, {KEY: [576.0, 324.0, 768.0, 518.4, 1.0, text]}, stage)
    server.place(12, {KEY: [576.0, 324.0, 768.0, 518.4, 1.0]}, stage)
    taken = server.take_audits(0, 20)[KEY]
    assert taken["over"] == {"10": [[0.25, 0.3, 0.75, 0.41111], [0.3125, 0.44444, 0.6875, 0.55556]]}
    assert set(taken["placed"]) == {"10", "11"}, "the guard's frame placed, never covered"
    # Kept with the segment's audits and merged back, by a range worker too.
    segments = tmp_path / "segments"
    segments.mkdir()
    (segments / (Path(compositor.segment_name(0, 20)).stem + compositor.AUDIT_SUFFIX)).write_text(json.dumps({KEY: taken}), encoding="utf-8")
    merged = compositor.segment_audits(segments, [(0, 20)])
    assert merged[KEY]["over"] == {10: taken["over"]["10"]}
    into: dict = {}
    render_ranges.merge_audits(into, merged)
    assert into[KEY]["over"] == merged[KEY]["over"]


def _statement_spec() -> tuple[dict, str]:
    from app.services.video_studio import themes
    from app.services.video_studio.spec import parse_video_spec, validate_video_spec

    raw = {
        "title": "Covered",
        "freestyle": True,
        "format": {"aspect_ratio": "16:9"},
        "captions": {"enabled": False},
        "theme": {"watermark": "none"},
        "scenes": [
            {
                "id": "stat",
                "type": "statement",
                "lines": ["Every alert", "*triaged* first", "in one place"],
                "chips": ["OpenCTI", "OpenAEV"],
                "duration": 6.0,
                "backdrop": {"file_id": "00000000-0000-0000-0000-000000006596"},
                "overlays": [
                    {
                        "type": "screen_insert",
                        "source": {"file_id": "00000000-0000-0000-0000-000000006597"},
                        "quad": [[0.25, 0.3], [0.75, 0.3], [0.75, 0.75], [0.25, 0.75]],
                        "duration": 6.0,
                    }
                ],
            }
        ],
    }
    spec = parse_video_spec(validate_video_spec(raw))
    media = timeline.PlanMedia()
    media.backdrops["stat"] = timeline.ImageMedia(1600, 900)
    media.inserts[timeline.insert_key("stat", 0)] = timeline.ImageMedia(1280, 720)
    media.inserts_gathered = True
    theme = themes.build_theme(mode=spec.theme.mode, background_style=spec.theme.background)
    return timeline.plan(spec, media, fps=FPS, theme=theme).document, timeline.insert_key("stat", 0)


@pytest.mark.chromium
async def test_in_a_real_chromium_insert_rects_names_the_statement_s_lines_and_chips_over_its_insert():
    from playwright.async_api import async_playwright

    from app.services.builtin_tools.html_render import font_face_css
    from app.services.headless_render import RENDER_CHROMIUM_ARGS
    from app.services.video_studio.compositor_page import ORIGIN, page_html

    document, key = _statement_spec()
    html = page_html(font_face_css()).encode("utf-8")
    index = f"{ORIGIN}/index.html"

    async def handle(route) -> None:
        if route.request.url == index:
            await route.fulfill(status=200, body=html, content_type="text/html; charset=utf-8")
        else:
            await route.abort("blockedbyclient")

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=list(RENDER_CHROMIUM_ARGS))
            try:
                context = await browser.new_context(viewport={"width": int(document["width"]), "height": int(document["height"])})
                await context.route("**/*", handle)
                page = await context.new_page()
                await page.goto(index, wait_until="load")
                # As a render's driver loads it (``compositor.render_frames``): the frame server's media, never a preview's.
                await page.evaluate("doc => window.__xtm.load(doc)", document)
                await page.evaluate("i => window.__xtm.render(i)", 0)
                before = await page.evaluate("() => window.__xtm.insertRects()")
                await page.evaluate("i => window.__xtm.render(i)", round(5.0 * FPS))
                after = await page.evaluate("() => window.__xtm.insertRects()")
            finally:
                await browser.close()
    except Exception as exc:
        if "Executable doesn't exist" not in str(exc) and "playwright install" not in str(exc).lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
    # Nothing of the statement in yet on its first frame: the insert's rect alone.
    assert len(before[key]) == 5
    x, y, w, h, shown, over = after[key]
    assert shown == 1.0 and over
    # Each box crosses the insert's rect: the lines' glyphs, each chip whole.
    for bx, by, bw, bh in over:
        assert bw > 0 and bh > 0 and bx < x + w and bx + bw > x and by < y + h and by + bh > y
    covered = sum(bw * bh for _bx, _by, bw, bh in over)
    assert covered > 0.05 * w * h, "a statement's lines across its quad"
    # Kept as the frame server keeps them, 0-1 of the frame, by the presence gate's frames alone.
    server = compositor.FrameServer(compositor.CompositorAssets(), "<html></html>", presence={0: [key]})
    server.place(0, {key: after[key]}, (float(document["width"]), float(document["height"])))
    assert len(server.covers[key][0]) == len(over)
