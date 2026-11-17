"""A static keyed screen insert draws its key, a resumed render never keeps another key's frames, and the residual
key is measured only where a setting can change it.

On synthetic footage, no provider, no browser:

* the segments' digest reads every field a screen insert is drawn by - its key's colour, tolerance and feather
  among them - and each file by what it holds; a key change changes it and the old key's segments are dropped;
* a resumed attempt says which segments it kept, the revision and the attempt that drew them and the digest parts
  they matched on; a render that kept none says so;
* the author's tolerance is never lowered by the cap on the surface's spread;
* a key change draws different frames on a static keyed insert: its tolerance in the margin, its feather on the
  edge of what stands in front;
* the footage's own key colour past the bezel is filled with the bezel's colour on its side and fails nothing,
  while the surface running past a quad too small still fails the gate - said by region and side, the side
  holding most of it kept as a 100 % crop and named in the answer.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from app.services.video_studio import (
    compositor,
    encoder,
    insert_checks,
    render_checks,
    render_preflight,
    screen_insert,
    screens,
)
from app.services.video_studio.screen_insert import Footage, InsertSource, KeySpec
from app.services.video_studio.spec import QUALITY_PRESETS

W, H = 960, 540
GREEN = (32, 230, 40)
BEZEL = (28, 28, 32)
#: The monitor's screen in the footage, pixels: the quad the insert is given.
SCREEN = (300, 130, 660, 400)
KEY = "insert/s1.0"
FRAMES = 6


def _quad(x0: float = SCREEN[0], y0: float = SCREEN[1], x1: float = SCREEN[2], y1: float = SCREEN[3]) -> tuple:
    return tuple((x / W, y / H) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)))


QUAD = _quad()


def _room(
    *, panel: int = 0, shade: tuple = GREEN, bezel: int = 8, right_bezel: tuple | None = None, person: int | None = None, seed: int = 1
) -> np.ndarray:
    """A noisy wall, a keyed *panel* px larger than the monitor in *shade*, the monitor's bezel across it, its screen
    green; *person* rises that many px into the screen through its bottom edge."""
    rng = np.random.default_rng(seed)
    picture = Image.fromarray((rng.random((H, W, 3)) * 20 + 95).astype(np.uint8))
    draw = ImageDraw.Draw(picture)
    x0, y0, x1, y1 = SCREEN
    if panel:
        draw.rectangle((x0 - panel, y0 - panel, x1 + panel, y1 + panel), fill=shade)
    draw.rectangle((x0 - bezel, y0 - bezel, x1 + bezel, y1 + bezel), fill=BEZEL)
    if right_bezel is not None:
        draw.rectangle((x1, y0 - bezel, x1 + bezel, y1 + bezel), fill=right_bezel)
    draw.rectangle((x0, y0, x1, y1), fill=GREEN)
    if person:
        draw.ellipse((440, y1 - person, 540, y1 - person + 160), fill=(70, 50, 40))
    return np.asarray(picture.filter(ImageFilter.GaussianBlur(1.0))).copy()


def _insert(tolerance: float = 0.3, feather: float = 1.5, quad: tuple = QUAD) -> InsertSource:
    return InsertSource(
        "p.png",
        "image",
        quad,
        fit="contain",
        key=KeySpec("green", tolerance, feather),
        footage=Footage("f", "video", 24.0, "clip/s1"),
        box=tuple(screen_insert.grown_box(quad, W / H)),
        label="Scene s1: its screen insert 0",
    )


def _picture() -> Image.Image:
    """A picture of its own proportions, on transparency: a contain fit leaves the surface showing around it."""
    picture = Image.new("RGBA", (400, 300), (0, 0, 0, 0))
    ImageDraw.Draw(picture).rectangle((40, 30, 360, 270), fill=(20, 30, 90, 255))
    return picture


def _static(insert: InsertSource, frames: list[np.ndarray], *, evidence: bool = False) -> list:
    """Each frame of a static keyed insert as the frame server draws it: its fill and its surface read once."""
    insert = screen_insert.keyed_on(insert, frames[0])
    fill = screen_insert.static_fill(insert, frames[0], (W, H))
    assert fill is not None
    surface = screen_insert.Surface.of(frames[0], [(x * W, y * H) for x, y in insert.quad])
    state = screen_insert.Matting()
    picture = _picture()
    return [
        screen_insert.compose(
            insert, picture, (W, H), n, footage=frame, surface=surface, fill=fill, state=state, window_frame=n, evidence=evidence
        )
        for n, frame in enumerate(frames)
    ]


def _keyed(insert: InsertSource, frames: list[np.ndarray], *, evidence: bool = False) -> list:
    """Each frame keyed frame by frame (a tracked insert's path), its matte pooled over the frames around."""
    insert = screen_insert.keyed_on(insert, frames[0])
    state = screen_insert.Matting()
    picture = _picture()
    return [
        screen_insert.compose(insert, picture, (W, H), n, footage=frame, state=state, window_frame=n, evidence=evidence)
        for n, frame in enumerate(frames)
    ]


def _at(layer, x: int, y: int) -> np.ndarray:
    x0, y0, _x1, _y1 = layer.warped.box
    return np.asarray(layer.warped.image, dtype=int)[y - y0, x - x0, :3]


# ── The segments' digest ─────────────────────────────────────────────────


def _assets(tmp_path: Path, insert: InsertSource | None = None, *, footage_file: str = "clip-file") -> compositor.CompositorAssets:
    footage = tmp_path / "clip-s1.mp4"
    footage.write_bytes(b"clip")
    picture = tmp_path / "insert-0.png"
    if not picture.exists():
        picture.write_bytes(b"the screenshot")
    shown = dataclasses.replace(insert or _insert(0.6, 5.0), path=str(picture), footage=Footage(str(footage), "video", 24.0, "clip/s1"))
    return compositor.CompositorAssets(
        sources={"clip/s1": compositor.FrameSource(str(footage), 24.0)},
        inserts={KEY: shown},
        origins={str(footage): f"file:{footage_file}"},
    )


def _document(trim: float = 0.0) -> dict:
    payload = {"src": KEY, "quad": [list(p) for p in QUAD], "fit": "contain", "glow": 0.0, "t0": 0.0, "t1": 3.0, "kind": "image"}
    if trim:
        payload["time_map"] = [[0.0, trim], [3.0, trim + 3.0]]
    scene = {"id": "s1", "type": "clip", "start": 0.0, "end": 3.0, "src": "clip/s1", "inserts": [payload]}
    return {"width": 1280, "height": 720, "fps": 30, "frames": 90, "duration": 3.0, "scenes": [scene]}


def test_the_segments_digest_reads_every_field_a_screen_insert_is_drawn_by(tmp_path):
    preset, size, frames = QUALITY_PRESETS["draft"], (1280, 720), (0, 90)

    def parts(assets, document=None):
        return compositor.drawing_inputs(document or _document(), preset, size, frames, assets)

    before = parts(_assets(tmp_path))
    assert parts(_assets(tmp_path)) == before, "the same drawing, the same parts"
    assert set(before) == {"document", "preset", "size", "range", "source clip/s1", f"insert {KEY}"}
    # The revision of the report: only its key changed (tolerance 0.6 -> 0.88, feather 5 -> 9). The insert's part
    # moves, nothing else - and so does the digest the segments are kept under.
    rekeyed = parts(_assets(tmp_path, _insert(0.88, 9.0)))
    assert {name for name in before if rekeyed[name] != before[name]} == {f"insert {KEY}"}
    assert compositor.digest_of(rekeyed) != compositor.digest_of(before)
    # Every other field it is drawn by moves it too.
    base = _insert(0.6, 5.0)
    keyed = base.key
    for changed in (
        dataclasses.replace(base, key=dataclasses.replace(keyed, tolerance=0.61)),
        dataclasses.replace(base, key=dataclasses.replace(keyed, feather=5.5)),
        dataclasses.replace(base, key=dataclasses.replace(keyed, color="blue")),
        dataclasses.replace(base, key=dataclasses.replace(keyed, rgb=(30, 220, 40))),
        dataclasses.replace(base, key=dataclasses.replace(keyed, spread=0.2)),
        dataclasses.replace(base, key=None),
        dataclasses.replace(base, fit="cover"),
        dataclasses.replace(base, glow=0.3),
        dataclasses.replace(base, light=0.5),
        dataclasses.replace(base, quad=_quad(x1=650)),
        dataclasses.replace(base, first_frame=3),
        dataclasses.replace(base, box=tuple(screen_insert.grown_box(QUAD, 1.5))),
    ):
        assert parts(_assets(tmp_path, changed))[f"insert {KEY}"] != before[f"insert {KEY}"], changed
    # Its source's trim is in the plan: the document and the insert's part both move.
    trimmed = parts(_assets(tmp_path), _document(trim=1.5))
    assert trimmed["document"] != before["document"] and trimmed[f"insert {KEY}"] != before[f"insert {KEY}"]
    # A footage downloaded from another file - a shot generated again, same length - moves its source and the insert
    # it mattes: the planned document alone never said it.
    regenerated = parts(_assets(tmp_path, footage_file="another-clip-file"))
    assert regenerated["document"] == before["document"]
    assert regenerated["source clip/s1"] != before["source clip/s1"] and regenerated[f"insert {KEY}"] != before[f"insert {KEY}"]
    # A file the render made is named by what it holds: the same picture elsewhere is the same, another is not.
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    copy = elsewhere / "insert-0.png"
    copy.write_bytes(b"the screenshot")
    moved = _assets(tmp_path)
    moved.inserts[KEY] = dataclasses.replace(moved.inserts[KEY], path=str(copy))
    assert parts(moved)[f"insert {KEY}"] == before[f"insert {KEY}"]
    copy.write_bytes(b"another screenshot")
    redrawn = _assets(tmp_path)
    redrawn.inserts[KEY] = dataclasses.replace(redrawn.inserts[KEY], path=str(copy))
    assert parts(redrawn)[f"insert {KEY}"] != before[f"insert {KEY}"]
    # No assets read as none: a render with nothing but its document keeps the digest it always had.
    plain = _document()
    assert compositor.drawing_digest(plain, preset, size, frames) == compositor.drawing_digest(plain, preset, size, frames, compositor.CompositorAssets())
    # The segments of the old key are never kept for the new one.
    directory = tmp_path / compositor.SEGMENTS_DIR
    compositor.kept_segments(directory, compositor.digest_of(before))
    (directory / compositor.segment_name(0, 90)).write_bytes(b"x" * 64)
    assert compositor.kept_segments(directory, compositor.digest_of(before)) != {}
    assert compositor.kept_segments(directory, compositor.digest_of(rekeyed)) == {}


async def test_a_resumed_attempt_says_what_it_kept_who_drew_it_and_what_it_matched_on(tmp_path, monkeypatch):
    preset, size = QUALITY_PRESETS["draft"], (1280, 720)
    document = {"width": 1280, "height": 720, "fps": 30, "frames": 600, "duration": 20.0, "scenes": []}
    assets = _assets(tmp_path)
    parts = compositor.drawing_inputs(document, preset, size, (0, 600), assets)
    workdir = tmp_path / "work"
    directory = workdir / compositor.SEGMENTS_DIR
    compositor.kept_segments(directory, compositor.digest_of(parts))
    pieces = compositor.segment_ranges(0, 600, compositor.segment_frames(600, 30))
    for index, (first, end) in enumerate(pieces):
        (directory / compositor.segment_name(first, end)).write_bytes(b"x" * 64)
        # The first half drawn for revision 7 by one attempt, the rest for revision 8 by the next.
        drawn = {"revision": 7, "attempt": "started 10:00"} if index < len(pieces) // 2 else {"revision": 8, "attempt": "started 10:20"}
        (directory / (Path(compositor.segment_name(first, end)).stem + compositor.DRAWN_SUFFIX)).write_text(json.dumps(drawn))

    async def concat(parts_, *, out_path, workdir):
        Path(out_path).write_bytes(b"video")

    monkeypatch.setattr(encoder, "concat", concat)
    tracker = compositor.DrawProgress()
    await compositor.render_frames(
        document, assets, preset=preset, output_size=size, workdir=str(workdir), font_css="", pages=1, tracker=tracker
    )
    assert tracker.kept == 600 and tracker.matched == parts
    assert tracker.reused == [
        {"frames": [0, 300], "segments": 5, "revision": 7, "attempt": "started 10:00"},
        {"frames": [300, 600], "segments": 5, "revision": 8, "attempt": "started 10:20"},
    ]
    drawn = render_preflight.layout(size, x264_preset="veryfast", ceiling=8 << 30, pages=1, cpus=2)
    measured = render_preflight.measurement(
        drawn, frames=0, frames_reused=tracker.kept, seconds=0, segments_reused=render_preflight.segments_reused(tracker)
    )
    said = render_preflight.said_of(SimpleNamespace(measured=measured))
    assert "600 more were kept from the segments an earlier attempt of this render finished (frames 0-300 (5 segments) drawn for revision 7 by the attempt started 10:00; frames 300-600 (5 segments) drawn for revision 8" in said
    assert f"its digest matched on document {parts['document'][:8]}" in said and f"insert {KEY} {parts[f'insert {KEY}'][:8]}" in said
    assert "its key included" in said
    # A segment drawn by a release before the records were kept says nothing it does not know.
    assert compositor.kept_from(tmp_path / "nowhere", [(0, 60)]) == [{"frames": [0, 60], "segments": 1, "revision": None, "attempt": None}]
    assert "drawn for revision" not in render_preflight.reused_line({"segments": [{"frames": [0, 60], "segments": 1}], "matched": {}})
    # Closed by this release, a segment records who drew it - and the store keeps it with the segment.
    from app.services.video_studio import render_segments

    uploaded: list[str] = []

    class _Store:
        async def upload_path(self, path, name, _content_type=None):
            uploaded.append(name)

    monkeypatch.setattr("app.services.file_storage.file_storage", _Store())
    await render_segments.keep("r1", "d1", directory / compositor.segment_name(*pieces[0]))
    assert [name.rsplit("/", 1)[-1] for name in uploaded] == [Path(compositor.segment_name(*pieces[0])).stem + compositor.DRAWN_SUFFIX, compositor.segment_name(*pieces[0])]


# ── The key, drawn ────────────────────────────────────────────────────────


def test_the_author_s_tolerance_is_never_lowered_by_the_surface_s_spread():
    assert screens.spread_tolerance(0.88, 0.05) == 0.88, "a tolerance past the cap stands as given"
    assert screens.spread_tolerance(0.3, 1.0) == screens.MAX_SPREAD_TOLERANCE, "the spread widens up to the cap"
    assert screens.spread_tolerance(0.3, 0.05) == 0.3
    assert KeySpec("green", 0.88, 9.0, spread=0.05).matte_tolerance() == 0.88


def test_a_key_change_draws_different_frames_on_a_static_keyed_insert():
    # The keyed panel the model drew larger than the monitor, past its bezel, in a shade of the key only a wide
    # tolerance takes.
    frames = [_room(panel=60, shade=(0, 228, 120), seed=10 + n) for n in range(FRAMES)]
    narrow = _static(_insert(0.3, 1.5), frames)
    wide = _static(_insert(0.88, 1.5), frames)
    past = (SCREEN[2] + 14, (SCREEN[1] + SCREEN[3]) // 2)  # past the right bezel, in the panel
    # The wide key takes the panel past the bezel and fills it with the bezel's colour; the narrow one leaves it,
    # despilled. Different frames, where they used to be the same pixels.
    assert np.abs(_at(wide[-1], *past) - np.array(BEZEL)).max() < 20, _at(wide[-1], *past)
    assert np.abs(_at(narrow[-1], *past) - _at(wide[-1], *past)).max() > 40
    assert all(layer.audit.residual_key == 0.0 for layer in narrow + wide)
    # The feather: the edge of what stands in front - a person rising into the screen - is the key's own.
    crossing = [_room(panel=60, person=60 if n >= 2 else 0, seed=20 + n) for n in range(FRAMES)]
    hard = _static(_insert(0.3, 0.5), crossing)
    soft = _static(_insert(0.3, 9.0), crossing)
    assert hard[-1].audit.covered is not None and not hard[-1].audit.covers_foreground
    assert np.abs(np.asarray(hard[-1].warped.image, dtype=int) - np.asarray(soft[-1].warped.image, dtype=int)).max() > 20


# ── The margin around the quad ────────────────────────────────────────────


def test_the_footage_s_key_past_the_bezel_is_filled_with_the_bezel_s_colour_and_fails_nothing(monkeypatch):
    # The report's take: the keyed panel larger than the bezel the quad is snapped to; its right bezel lit warmer.
    frames = [_room(panel=60, right_bezel=(120, 80, 50), seed=30 + n) for n in range(FRAMES)]
    left, right = (SCREEN[0] - 14, 260), (SCREEN[2] + 14, 260)
    for layers in (_static(_insert(), frames), _keyed(_insert(), frames)):
        assert all(layer.audit.residual_key == 0.0 and layer.audit.residual_where is None for layer in layers)
        assert all(not layer.audit.misaligned for layer in layers), [layer.audit.alignment for layer in layers]
        # Each side takes its own bezel's colour, never the key and never another side's.
        assert np.abs(_at(layers[-1], *right) - np.array((120, 80, 50))).max() < 25, _at(layers[-1], *right)
        assert np.abs(_at(layers[-1], *left) - np.array(BEZEL)).max() < 20, _at(layers[-1], *left)
    # What the fill takes out is said, on every side.
    assert set(layers[-1].audit.despilled_where) == set(screen_insert.SIDES) and layers[-1].audit.despilled > 0.05
    # Left as it is - as it was -, that panel lies past the residual margin: the footage's own, never a residual key,
    # and despilled toward the bezel's luminance on every side instead.
    monkeypatch.setattr(
        screen_insert, "band_of", lambda region, matte, inside, *_rest: screen_insert.Band(np.zeros(inside.shape, np.float32), np.zeros(inside.shape, bool))
    )
    [before] = _keyed(_insert(), frames[:1])
    assert before.audit.residual_key == 0.0 and before.audit.despilled > 0.05
    assert set(before.audit.despilled_where) == set(screen_insert.SIDES)
    shown = _at(before, *right)
    assert shown[1] - max(shown[0], shown[2]) < 30, f"the panel past the bezel still reads as the key: {shown}"


def test_a_quad_too_small_still_fails_said_by_region_and_side_with_a_crop_of_that_side(tmp_path):
    # The quad stops 50 px short of the screen's right edge, farther than the bezel lock looks: the surface runs past it.
    short = _quad(x1=SCREEN[2] - 50)
    frames = [_room(seed=40 + n) for n in range(FRAMES)]
    for layers in (_static(_insert(quad=short), frames, evidence=True), _keyed(_insert(quad=short), frames, evidence=True)):
        audit = layers[-1].audit
        assert audit.residual_key > screen_insert.RESIDUAL_FAIL_SHARE and set(audit.residual_where) == {"edge"}
        assert screen_insert.worst_side(audit.residual_where) == "right"
        assert audit.residual_where["edge"]["right"] > 0.9 * audit.residual_key
        # The crop: that side's margin past the edge and as deep inside it, unmarked, at the frame's own size.
        strip = layers[-1].residual_crop
        margin = screen_insert.KEY_MARGIN * W
        assert strip is not None and abs(strip.width - 2 * (margin + 2)) <= 3 and strip.height >= SCREEN[3] - SCREEN[1]
        pixels = np.asarray(strip, dtype=int)
        assert (pixels[..., 1] - np.maximum(pixels[..., 0], pixels[..., 2])).max() > 120, "the green left past the quad is in the crop"
    # Kept by the frame server as the residual's own evidence, lossless, beside the frame it shows.
    server = compositor.FrameServer(compositor.CompositorAssets(), "<html></html>", evidence_dir=tmp_path / compositor.SEGMENTS_DIR / compositor.EVIDENCE_DIR)
    for n, layer in enumerate(layers):
        asyncio.run(server._audited(KEY, n, layer.audit, layer.pictures()))
    taken = server.take_audits(0, FRAMES)
    assert taken[KEY]["evidence"]["residual_crop"]["file"].endswith(".png")
    directory = tmp_path / compositor.SEGMENTS_DIR
    (directory / (Path(compositor.segment_name(0, FRAMES)).stem + compositor.AUDIT_SUFFIX)).write_text(json.dumps(taken), encoding="utf-8")
    merged = compositor.segment_audits(directory, [(0, FRAMES)])
    placed = {KEY: insert_checks.Placed(KEY, "s1", "Scene s1: its screen insert 0", 0.0, FRAMES / 24.0)}
    found = {finding.kind: finding for finding, _proof in insert_checks.findings(merged, placed, fps=24)}
    residual = found[render_checks.INSERT_RESIDUAL_KEY]
    assert "% on the surface running past the quad on its right" in residual.message
    assert "leave its quad out (quad auto, no track) and save" in residual.message and residual.data["where"]["edge"]["right"] > 0
    [report] = insert_checks.reports(merged, placed, fps=24)
    assert report["residual_where"]["edge"]["right"] > 0 and report["residual_crop"]["side"] == "right"
    assert Path(report["residual_crop"]["path"]).is_file()
    # Stored, the crop is a file of the render: attached to the answer, named on its insert's line, deleted with it.
    report["residual_crop"].pop("path")
    report["residual_crop"]["file_id"] = "strip-1"
    checks = render_checks.Checks(inserts=[report]).as_dict()
    assert render_checks.insert_crop_ids(checks) == ["strip-1"]
    [line] = render_checks.insert_lines(checks)
    assert "on the surface running past the quad on its right" in line and "its right side at 100%: file `strip-1`" in line
    # The answer names it by the conversation's copy, the id that opens.
    [shown] = render_checks.insert_lines(render_checks.with_files(checks, {"strip-1": "copy-1"}))
    assert "its right side at 100%: file `copy-1`" in shown


def test_the_advice_follows_where_the_residual_sits():
    band = {"band": {"right": 0.012}, "inside": {"top": 0.0002}}
    said = insert_checks.residual_where_text(band)
    assert said == "1.20% in the margin past the bezel on its right, 0.02% inside the quad along its top"
    # One action, where most of it sits: the footage's own key colour past the bezel is the shot's to fix.
    chosen = insert_checks._residual_action(band)
    assert chosen["kind"] == insert_checks.REGENERATE_SHOT and chosen["text"].startswith("the footage's own key colour past the bezel")
    assert "tolerance" not in chosen["text"]
    nowhere = insert_checks._residual_action(None)
    assert nowhere["kind"] == insert_checks.GIVE_QUAD and "leave its quad out (quad auto, no track) and save" in nowhere["text"]
    assert insert_checks.residual_where_text(None) == "" and screen_insert.worst_side(None) is None
