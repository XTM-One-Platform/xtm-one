"""A crossfade's faint frames are a fact of the presence gate, and two drawings of one join agree.

A crossfade's frames are read unblended, and the gate judged every frame showing the insert above alpha
0.5. On the s01 to s02 join of a sweep, one frame at alpha 0.58 read SSIM 0.29 against 0.3 and warned "insert
missing", its fix line promising the same frames would block next time; the s02 preview of the same join, drawn
minutes later, passed. Unblending divides the encode's error by the alpha: the fainter the frame, the more the reading
moves from one encode to the next. The floor is 0.65; this module holds the issue's own fixture to
it, and two drawings of one join to each other.

Synthetic fixtures only - no provider, no browser, no database. A bright title card crossfades over 0.7 s into a clip
whose two screens are keyed inserts of a dim product UI, each frame composed as the render composes it
(``test_video_studio_insert_tail._Render``) and put through a lossy encode harsh on the blend, each preview its own:

* judged as before, from alpha 0.5, the faint frames of the join miss the inserts on one preview and not the next;
* from 0.65 (``PRESENCE_MIN_SHOWN``) every preview of either scene reads both inserts present, the faint frames said;
* the inserts really black on the join still fail;
* a later drawing of the same join that reads them the other way says so, never warns;
* a finding nothing in the spec causes on more than one frame blocks every render of the same spec, never the
  harness's said once (one frame alone).
"""

from __future__ import annotations

import functools
import io
import math

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from app.services.video_studio import insert_checks, render, render_checks, screen_insert, timeline
from app.services.video_studio.render_checks import FAIL, INSERT_MISSING, Checks, Finding
from app.services.video_studio.screen_insert import Footage, InsertSource, KeySpec
from tests.test_video_studio_insert_tail import _Render
from tests.video_studio_insert_fixture import BEZEL, FPS, GREEN, H, W, dim_ui

#: The clip's two screens, in pixels of its footage.
SCREENS = {
    "insert/clip.0": np.array([(40, 70), (210, 70), (210, 196), (40, 196)], dtype=float),
    "insert/clip.1": np.array([(270, 60), (440, 60), (440, 186), (270, 186)], dtype=float),
}
QUADS = {key: [[round(x / W, 6), round(y / H, 6)] for x, y in screen] for key, screen in SCREENS.items()}
#: The title card to 1.6 s, the clip coming in over it from 0.9 s.
CROSSFADE = 0.7
TITLE_END, CLIP_END = 1.6, 2.6
#: Each preview's encode: its quality and its noise - as harsh on the blend as an H.264 preview is on a fade.
ENCODES = ((24, 1), (27, 2), (30, 3), (25, 4), (28, 5))


def _clip_footage() -> np.ndarray:
    """A noisy wall with two dark bezels around flat green panels, through a lens and an encoder."""
    rng = np.random.default_rng(7)
    picture = Image.fromarray((rng.random((H, W, 3)) * 20 + 95).astype(np.uint8))
    draw = ImageDraw.Draw(picture)
    for screen in SCREENS.values():
        draw.polygon([tuple(p) for p in screen_insert._grown_px(screen, 6.0)], fill=BEZEL)
        draw.polygon([tuple(p) for p in screen], fill=GREEN)
    frame = np.asarray(picture.filter(ImageFilter.GaussianBlur(0.8))).astype(np.float32)
    frame += np.random.default_rng(1007).normal(0.0, 2.0, frame.shape).astype(np.float32)
    return np.clip(frame, 0, 255).astype(np.uint8)


def _title_card() -> np.ndarray:
    """A bright title card: rows of heavy dark words over a light ground, across both screens."""
    picture = Image.new("RGB", (W, H), (238, 240, 244))
    draw = ImageDraw.Draw(picture)
    for k, y in enumerate(range(40, H - 30, 34)):
        for x in range(30 + (k % 2) * 20, W - 40, 58):
            draw.rectangle((x, y, x + 44, y + 16), fill=(18, 22, 34))
    return np.asarray(picture)


def _source(key: str) -> InsertSource:
    quad = tuple(tuple(p) for p in QUADS[key])
    return InsertSource(
        f"{key}.png",
        "image",
        quad,
        key=KeySpec("green", 0.4, 3.0),
        footage=Footage("f", "video", float(FPS), "clip/clip"),
        box=tuple(screen_insert.grown_box(quad, W / H)),
        label=f"Scene clip: its screen insert {key[-1]}",
    )


def _document() -> dict:
    """The title card, then the clip crossfading in over it, its two inserts from its start to its end."""
    start = TITLE_END - CROSSFADE
    payloads = []
    for key in SCREENS:
        payloads.append(
            {
                "src": key,
                "quad": QUADS[key],
                "box": [list(p) for p in _source(key).box],
                "t0": 0.0,
                "t1": round(CLIP_END - start, 4),
                "keyed": True,
                "matte": {"fps": FPS, "frames": 400},
                "to_end": True,
            }
        )
    timeline.hold_surfaces(payloads, round(CLIP_END - start, 4))
    scenes = [
        {"id": "title", "type": "title", "start": 0.0, "end": TITLE_END, "transition_in": {"type": "cut", "duration": 0.0}, "inserts": []},
        {"id": "clip", "type": "clip", "start": start, "end": CLIP_END, "transition_in": {"type": "crossfade", "duration": CROSSFADE}, "inserts": payloads},
    ]
    return {"fps": FPS, "frames": math.ceil(CLIP_END * FPS), "duration": CLIP_END, "fade_in": 0.0, "fade_out": 0.0, "scenes": scenes}


class _Join(_Render):
    """The render of the join: the title card's frame, the clip's footage with its two inserts drawn on it, and each
    preview's own encode (:attr:`encode`)."""

    encode = (90, 0)
    black = False

    def __init__(self, document: dict) -> None:
        self.document = document
        self.footage = {"title": _title_card(), "clip": _clip_footage()}
        self.pictures = {"insert/clip.0": dim_ui((40, 46, 60)), "insert/clip.1": dim_ui((60, 44, 48))}
        self.sources = dict(self.pictures)
        if self.black:
            self.pictures = {key: Image.new("RGB", (600, 450), (0, 0, 0)) for key in self.pictures}
        self.inserts, self.fills, self.surfaces, self.states = {}, {}, {}, {}
        for key, screen in SCREENS.items():
            first = self.footage["clip"]
            self.inserts[key] = screen_insert.keyed_on(_source(key), first)
            self.fills[key] = screen_insert.static_fill(self.inserts[key], first, (W, H))
            self.surfaces[key] = screen_insert.Surface.of(first, list(map(tuple, screen)))
            self.states[key] = screen_insert.Matting()
        self.audits = {key: {"frames": {}, "evidence": {}, "placed": {}} for key in self.inserts}
        self._drawn = {}

    def frame(self, frame: int) -> np.ndarray:
        quality, seed = self.encode
        out = self._stack(frame, self.layers(frame))
        if seed:
            out = out + np.random.default_rng(seed * 1000 + frame).normal(0.0, 1.5, out.shape)
        buffer = io.BytesIO()
        Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)).save(buffer, format="JPEG", quality=quality)
        return np.asarray(Image.open(io.BytesIO(buffer.getvalue())).convert("RGB"))


@functools.cache
def _render(black: bool = False) -> _Join:
    """One drawing of the join, its layers drawn once: each preview encodes it its own way."""
    _Join.black = black
    try:
        return _Join(_document())
    finally:
        _Join.black = False


def _window(index: int) -> tuple[int, int]:
    """The output frames a preview of the scene at *index* draws (``render.window_times``)."""
    document = _document()
    begin, end, _from, _to = render.window_times(document, index)
    return math.floor(begin * FPS), min(int(document["frames"]), math.ceil(end * FPS))


def _preview(n: int, *, black: bool = False) -> insert_checks.Presence:
    """The *n*-th consecutive preview: the title card's and the clip's in turn, each with its own encode."""
    render_ = _render(black)
    render_.encode = ENCODES[n % len(ENCODES)]
    window = _window(n % 2)
    return render_.gate(range(*window), frame_range=window).result()


def _alpha(frame: int) -> float:
    return (frame / FPS - (TITLE_END - CROSSFADE)) / CROSSFADE


def test_judged_from_alpha_0_5_the_faint_frames_of_the_join_flip_with_the_encode(monkeypatch):
    monkeypatch.setattr(insert_checks, "PRESENCE_MIN_SHOWN", 0.5)
    results = [_preview(n) for n in range(len(ENCODES))]
    missed = [n for n, result in enumerate(results) if any(summary["missing"] for summary in result.by_key.values())]
    # The bug: one preview of the join misses the inserts, another of the same join reads them present.
    assert missed and len(missed) < len(results)
    for n in missed:
        for finding, _proof in results[n].findings:
            assert finding.kind == INSERT_MISSING and finding.data["transition"]
            assert 0.5 <= finding.data["alpha"] < 0.65 and 0.5 <= _alpha(finding.data["frame"]) < 0.65


def test_from_alpha_0_65_both_inserts_are_present_on_both_sides_of_the_join_across_5_consecutive_previews():
    assert insert_checks.PRESENCE_MIN_SHOWN == 0.65
    for n in range(len(ENCODES)):
        result = _preview(n)
        assert [finding.message for finding, _proof in result.findings] == [], f"preview {n}"
        for key in SCREENS:
            summary = result.by_key[key]
            assert summary["missing"] == 0 and summary["joins_missing"] == 0 and summary["joins"] > 0, (n, key, summary)
            # The frames under 0.65 are faint: counted, never judged.
            assert summary["faint"] >= 3 and summary["least_alpha"] >= insert_checks.PRESENCE_MIN_SHOWN
            assert summary["join_spans"]["missing"] == [] and summary["join_spans"]["present"]


def test_the_report_says_the_faint_frames_as_a_fact():
    result = _preview(1)
    render_ = _render()
    placed = insert_checks.placed_inserts(render_.document, render_.inserts)
    report = next(r for r in insert_checks.reports(render_.audits, placed, fps=FPS, presence=result.by_key) if r["key"] == "insert/clip.0")
    said = next(m for m in insert_checks.classify(report) if m.text.startswith("its source present on all"))
    assert said.cls == insert_checks.FACT
    assert f"{result.by_key['insert/clip.0']['faint']} frame(s) of a transition show under 65% of it, not judged" in said.text


def test_the_inserts_really_black_on_the_join_still_fail():
    for n in (0, 1):
        result = _preview(n, black=True)
        hit = {finding.data["insert"]: finding for finding, _proof in result.findings}
        assert set(hit) == set(SCREENS), f"preview {n}"
        for finding in hit.values():
            assert finding.kind == INSERT_MISSING and finding.severity == FAIL and finding.data["why"] in ("flat", "absent")
            assert finding.data.get("alpha", 1.0) >= insert_checks.PRESENCE_MIN_SHOWN
        assert all(summary["missing"] > 0 for summary in result.by_key.values())


# ── Two drawings of one join ──────────────────────────────────────────────


def _presence(*, missing: list[list[float]], present: list[list[float]]) -> insert_checks.Presence:
    """A presence read of ``insert/clip.0`` whose join frames read missing on *missing* and present on *present*."""
    found = insert_checks.Presence()
    frames = sum(round((b - a) * FPS) + 1 for a, b in missing)
    found.by_key["insert/clip.0"] = {
        "frames": 40,
        "missing": frames,
        "unjudged": 0,
        "joins": 12,
        "joins_missing": frames,
        "join_spans": {"present": present, "missing": missing},
    }
    if missing:
        fix = "nothing in the spec causes it and rendering again draws the same frame: report the render with this frame"
        found.findings.append(
            (
                Finding(
                    INSERT_MISSING,
                    missing[0][0],
                    missing[-1][1],
                    "clip",
                    f"insert missing at t=0:01.29: Scene clip: its screen insert 0 - ... - {fix}",
                    FAIL,
                    {"insert": "insert/clip.0", "gate": "presence", "transition": True, "span": list(missing[0]),
                     "action": insert_checks.action(insert_checks.NOTHING, fix)},
                    fix=fix,
                ),
                None,
            )
        )
    return found


def _placed() -> dict[str, insert_checks.Placed]:
    render_ = _render()
    return insert_checks.placed_inserts(render_.document, render_.inserts)


def test_a_later_drawing_that_reads_missing_where_the_other_side_s_preview_read_present_says_so_and_never_warns():
    earlier = {"insert/clip.0": [{"stage": "preview", "id": "render-s02", "revision": 113, "present": [[1.333, 1.583]], "missing": []}]}
    found = _presence(missing=[[1.333, 1.333]], present=[[1.375, 1.583]])
    notes = insert_checks.agree_on_joins(found, earlier, _placed(), offset=0.0)
    assert found.findings == []
    assert notes == [
        "Scene clip: its screen insert 0: read missing on its scene's join at t=0:01.33, where preview render-s02 of the "
        "same join read it present, not a finding - the two drawings disagree on a measure at its threshold there."
    ]
    assert found.by_key["insert/clip.0"]["disagrees"] == {"stage": "preview", "id": "render-s02", "at": 1.333, "here": "missing"}


def test_a_drawing_that_reads_present_where_the_other_side_s_preview_warned_says_so():
    earlier = {"insert/clip.0": [{"stage": "preview", "id": "render-s01", "revision": 113, "present": [[1.375, 1.583]], "missing": [[1.333, 1.333]]}]}
    found = _presence(missing=[], present=[[1.333, 1.583]])
    notes = insert_checks.agree_on_joins(found, earlier, _placed())
    assert len(notes) == 1 and "read present on its scene's join at t=0:01.33, where preview render-s01 of the same join read it missing" in notes[0]
    summary = found.by_key["insert/clip.0"]
    assert summary["disagrees"]["here"] == "present"
    report = {"joins": {"frames": 12, "residual_key": 0.0, "missing": 0}, "presence": summary}
    said = next(m for m in insert_checks.classify(report) if m.text.startswith("its source present on all"))
    assert said.cls == insert_checks.FACT and "preview render-s01 of the same join read it missing at t=0:01.33" in said.text


def test_a_missing_frame_off_the_join_or_not_read_present_there_stays_a_finding():
    earlier = {"insert/clip.0": [{"stage": "preview", "id": "render-s02", "present": [[1.458, 1.583]], "missing": []}]}
    found = _presence(missing=[[1.333, 1.5]], present=[])
    notes = insert_checks.agree_on_joins(found, earlier, _placed())
    assert len(found.findings) == 1 and notes[0].endswith(
        "; its other missing frames stay a finding - the two drawings disagree on a measure at its threshold there."
    )
    # Agreeing drawings say nothing.
    agreeing = {"insert/clip.0": [{"stage": "preview", "id": "render-s02", "present": [[1.583, 1.583]], "missing": [[1.333, 1.5]]}]}
    found = _presence(missing=[[1.333, 1.5]], present=[])
    assert insert_checks.agree_on_joins(found, agreeing, _placed()) == [] and len(found.findings) == 1


def test_a_scene_preview_keeps_what_it_read_of_its_neighbour_s_inserts_on_the_join_with_their_scene_s_digest():
    checks = Checks()
    checks.inserts = [
        {"key": "insert/clip.0", "scene_id": "clip", "presence": {"frames": 9, "missing": 0, "join_spans": {"present": [[1.333, 1.583]], "missing": []}}},
        {"key": "insert/clip.1", "scene_id": "clip", "presence": {"frames": 9, "missing": 0}},
    ]
    joined = {"title": "digest-title", "clip": "digest-clip"}
    # The title card's preview: the clip's insert read through its transition out, kept on its own drawing.
    previews = render_checks.record_seen({}, {"title": "digest-title"}, "render-s01", checks=checks.as_dict(), stage="preview", revision=113, joined=joined)
    kept = previews["title"]["drawings"][0]["joins"]
    assert kept == {"insert/clip.0": {"digest": "digest-clip", "present": [[1.333, 1.583]], "missing": []}}
    readings = render_checks.join_readings(previews, joined)
    assert readings == {"insert/clip.0": [{"stage": "preview", "id": "render-s01", "revision": 113, "present": [[1.333, 1.583]], "missing": []}]}
    # The clip edited since: its join is drawn anew, nothing to hold it to.
    assert render_checks.join_readings(previews, {"title": "digest-title", "clip": "digest-clip-2"}) == {}
    # A whole render keeps each insert's reading on its own scene's drawing only.
    whole = render_checks.record_seen({}, joined, "render-1", checks=checks.as_dict(), stage="render", joined=joined)
    assert "joins" not in whole["title"]["drawings"][0] and set(whole["clip"]["drawings"][0]["joins"]) == {"insert/clip.0"}


def test_the_black_screen_s_finding_blocks_every_render_of_the_same_spec():
    """Its fix says nothing in the spec causes it, but on many frames it is the video's
    (``render_checks.HARNESS_MAX_FRAMES``): a block, on this render and the next of the same spec - never said once to
    the administrator and left."""
    findings = [finding for finding, _proof in _preview(1, black=True).findings]
    assert len(findings) == 2 and all(finding.data["frames"] > render_checks.HARNESS_MAX_FRAMES for finding in findings)
    kept, first = render_checks.harness_defects(findings, {})
    assert kept == findings and first == [] and Checks(findings=kept).failed
    previews = render_checks.record_seen({}, {"clip": "digest-clip"}, "render-1", checks=Checks(findings=kept).as_dict())
    again = [finding for finding, _proof in _preview(1, black=True).findings]
    kept, second = render_checks.harness_defects(again, render_checks.unexplained_before(previews, {"clip": "digest-clip"}))
    assert kept == again and second == [] and Checks(findings=kept).failed
