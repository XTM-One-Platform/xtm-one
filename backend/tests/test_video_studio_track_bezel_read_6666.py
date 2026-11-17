"""A tracked screen insert on a static-looking surface is followed from its first frame. Synthetic footage.

On footage whose bezel drifts 0.5% of the frame's width over 8 s - a wall shot, the static alignment gate
blocking its locked quad - ``track: true`` failed on footage frame 1: "the track's confidence fell to 20%". The
tracker read its confidence against the points it had after replacing the ones it lost, not against the points it
followed into the frame; and a frame it was genuinely unsure of failed although the bezel's edges were clean there.

* the tracker's confidence is the share of the points followed into the frame (``TrackStep.followed``): a wall
  shot whose screen draws on its own is followed by the tracker on every frame, its quad on the bezel;
* a frame the tracker is not sure of - a wall whose texture shimmers frame to frame, a flat screen, little to
  follow - keeps the bezel read there (``surface_refine.read_near``, chained on its own reading), the tracker
  started again from it: the quad follows the bezel on every frame, alignment under 0.1%, ``N + M`` the frames;
* the bezel read losing the surface too - the monitor leaves the frame at 3 s - is ``surface lost at t=3.00 s``;
* the save seeds the track on the bezel measured on the insert's first footage frame and says "track: N frames by
  the tracker, M by the bezel read", or the loss a preview fails on.
"""

from __future__ import annotations

import asyncio
import dataclasses
import shutil
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.services.video_studio import (
    compositor,
    encoder,
    insert_bezels,
    insert_checks,
    insert_tracking,
    render,
    screen_insert,
    timeline,
    tracking,
)
from app.services.video_studio.render_checks import INSERT_ALIGNMENT
from app.services.video_studio.spec import parse_video_spec, validate_video_spec
from app.services.video_studio.surface_refine import ALIGNMENT_MAX_SHARE

#: One worker encodes the footage once for every test of the module.
pytestmark = [
    pytest.mark.skipif(not encoder.encoder_available(), reason="ffmpeg is not installed"),
    pytest.mark.xdist_group("track_bezel_read_6666"),
]

#: The tracker's own size (``insert_tracking.TRACK_SIDE``): 0.1% of the frame's width is about a pixel.
W, H, FPS, SECONDS = 960, 540, 12, 8.0
#: The frames the shot plays (its encoder holds one more, at its 8 s).
COUNT = int(FPS * SECONDS)
#: The camera drifts this share of the frame's width over the shot.
DRIFT = 0.005
#: The monitor's screen on the first frame (pixels), its bezel around it.
SCREEN = ((315.0, 150.0), (645.0, 153.75), (642.75, 341.25), (317.25, 337.5))
#: The world the camera films is this much larger than the frame, the frame's origin at it on the first frame.
MARGIN = (150, 75)
#: The monitor leaves the frame on this footage frame (3 s): the camera whips to bare wall.
GONE = int(3.0 * FPS)
KEY = "insert/s1.0"
FILE = "00000000-0000-0000-0000-000000006666"
SOURCE = "00000000-0000-0000-0000-000000006667"


def _shift(frame: int) -> float:
    return DRIFT * W * frame / (COUNT - 1)


def _truth(frame: int) -> np.ndarray:
    """The screen's corners on footage frame *frame*, 0-1."""
    return (np.asarray(SCREEN) - [_shift(frame), 0.0]) / [W, H]


def _wall(rng: np.random.Generator, *, monitor: bool, green: bool) -> Image.Image:
    size = (W + 2 * MARGIN[0], H + 2 * MARGIN[1])
    light = np.linspace(150, 190, size[0], dtype=np.float32)[None, :] * np.ones((size[1], 1), np.float32)
    plaster = rng.normal(0, 6, (size[1] // 8, size[0] // 8)).astype(np.float32)
    grey = np.clip(light + np.asarray(Image.fromarray(plaster).resize(size, Image.Resampling.BICUBIC)), 0, 255)
    room = Image.fromarray(np.stack([grey, grey * 0.97, grey * 0.92], -1).astype(np.uint8), "RGB")
    if monitor:
        screen = [(x + MARGIN[0], y + MARGIN[1]) for x, y in SCREEN]
        draw = ImageDraw.Draw(room)
        draw.polygon(tracking.grown(screen, 0.05), fill=(24, 24, 28))
        draw.polygon(screen, fill=(0, 177, 64) if green else (214, 220, 228))
    return room


def _shot(tmp: Path, name: str, *, green: bool, jitter: float = 0.0, shimmer: float = 0.0, gone: int | None = None) -> str:
    """An 8 s wall shot drifting :data:`DRIFT`: a light screen whose panels *jitter* on their own, or a flat *green*
    one; the wall's fine texture drawn again on every frame by *shimmer* grey levels; bare wall from frame *gone*."""
    rng = np.random.default_rng(6666)
    room, bare = _wall(rng, monitor=True, green=green), _wall(rng, monitor=False, green=green)
    (x0, y0), (x1, y1) = (SCREEN[0][0] + MARGIN[0], SCREEN[0][1] + MARGIN[1]), (SCREEN[2][0] + MARGIN[0], SCREEN[2][1] + MARGIN[1])
    panels = [
        (rng.uniform(x0 + 45, x1 - 80), rng.uniform(y0 + 42, y1 - 56), rng.uniform(8, 38), rng.uniform(3, 15), tuple(int(v) for v in rng.integers(20, 160, 3)))
        for _ in range(60 if jitter else 0)
    ]
    outside = Image.new("L", room.size, 255)
    ImageDraw.Draw(outside).polygon(tracking.grown([(x + MARGIN[0], y + MARGIN[1]) for x, y in SCREEN], 0.05), fill=0)
    wall_only = np.asarray(outside, np.float32)[..., None] / 255.0
    folder = tmp / f"{name}-frames"
    folder.mkdir()
    names = []
    for index in range(COUNT):
        frame = (bare if gone is not None and index >= gone else room).copy()
        draw = ImageDraw.Draw(frame)
        for x, y, w, h, colour in panels:
            dx, dy = rng.uniform(-jitter, jitter, 2)
            draw.rectangle([x + dx, y + dy, x + dx + w, y + dy + h], fill=colour)
        pixels = np.asarray(frame, np.float32)
        if shimmer:
            fine = rng.normal(0, shimmer, (frame.size[1] // 3, frame.size[0] // 3)).astype(np.float32)
            pixels = pixels + np.asarray(Image.fromarray(fine).resize(frame.size, Image.Resampling.BICUBIC))[..., None] * wall_only
        left = MARGIN[0] + _shift(index)
        shot = Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8)).transform(
            (W, H), Image.Transform.EXTENT, (left, MARGIN[1], left + W, MARGIN[1] + H), resample=Image.Resampling.BICUBIC
        )
        grained = np.asarray(shot, np.float32) + rng.normal(0, 2.5, (H, W, 1))
        Image.fromarray(np.clip(grained, 0, 255).astype(np.uint8)).save(folder / f"f{index:03d}.png")
        names.append((index / FPS, f"f{index:03d}.png"))
    out = str(tmp / f"{name}.mp4")
    asyncio.run(encoder.frames_to_mezzanine(names, end_time=SECONDS, out_path=out, fps=FPS, crf=12, preset="ultrafast", workdir=str(folder)))
    return out


@pytest.fixture(scope="module")
def footage(tmp_path_factory) -> dict[str, str]:
    tmp = tmp_path_factory.mktemp("wall-6666")
    return {
        # The issue's wall shot: the screen's drawing moves on its own, the wall carries little detail.
        "drawn": _shot(tmp, "drawn", green=False, jitter=3.0),
        # A flat green screen on a wall whose fine texture shimmers frame to frame: little the tracker can hold on to.
        "shimmer": _shot(tmp, "shimmer", green=True, shimmer=20.0),
        "gone": _shot(tmp, "gone", green=True, shimmer=20.0, gone=GONE),
    }


def _decoded(path: str) -> list[np.ndarray]:
    async def read() -> list[np.ndarray]:
        return [np.frombuffer(raw, dtype=np.uint8).reshape(H, W, 3) async for raw in encoder.iter_rgb_frames(path, width=W, height=H)]

    return asyncio.run(read())[:COUNT]


def _seed(path: str, key: screen_insert.KeySpec | None) -> np.ndarray:
    """The bezel the save measures near the screen on the first frame (``surface_refine.measure_bezel``), 0-1: where a
    track starts, and the bezel every later frame shows moved by the camera's drift."""
    from app.services.video_studio import surface_refine

    bezel = surface_refine.measure_bezel(
        _decoded(path)[0], key.key() if key else None, key.matte_tolerance() if key else None, given=_truth(0).tolist()
    )
    assert bezel is not None and bezel.found_by == "seed"
    return np.asarray(bezel.quad, dtype=float)


def _followed(path: str, *, key: screen_insert.KeySpec | None = None) -> tuple[insert_tracking.InsertTrack, np.ndarray]:
    seed = _seed(path, key)
    asked = insert_tracking.Asked(seed.tolist(), 0, COUNT - 1, "Scene s1: its screen insert 0", key=key)
    [track] = asyncio.run(insert_tracking.track_all(path, [asked]))
    return track, seed


def _worst(track: insert_tracking.InsertTrack, seed: np.ndarray) -> float:
    """The farthest any corner of *track* stands from the bezel the frame shows - the *seed* moved by the camera's
    drift -, over its frames, a share of the frame's width."""
    return max(
        float(np.linalg.norm((np.asarray(track.quad_at(n)) - seed + [_shift(n) / W, 0.0]) * [W, H], axis=1).max()) / W
        for n in range(COUNT)
    )


# ── The tracker ───────────────────────────────────────────────────────────


def test_the_tracker_s_confidence_is_the_share_of_the_points_followed_into_the_frame(footage):
    """Frame 1 of the wall shot: most points followed agree, but against the points kept once the lost ones are
    replaced the share falls under the floor - what failed the track on its first frame."""
    frames = _decoded(footage["drawn"])
    given = np.asarray(SCREEN)
    tracker = tracking.PlanarTracker(
        tracking.luma(frames[0]), tracking.Region(outer=tracking.grown(given, insert_tracking.BEZEL_OUTSIDE)), max_points=240, min_distance=5, min_inliers=10
    )
    step = tracker.step(tracking.luma(frames[1]))
    assert step.found and step.followed >= step.inliers
    assert step.inliers / step.followed >= insert_tracking.MIN_CONFIDENCE > step.inliers / step.points


def test_a_wall_shot_whose_screen_draws_on_its_own_is_followed_from_its_first_frame(footage):
    track, seed = _followed(footage["drawn"])
    assert (track.first, track.last) == (0, COUNT - 1)
    # On the screen the camera carries, every frame: well inside the alignment gate (the preview's own reading of the
    # bezel, under 0.1%, is the render test's below).
    assert _worst(track, seed) < ALIGNMENT_MAX_SHARE / 2, _worst(track, seed)
    assert track.said() == f"{COUNT - len(track.bezel_read)} frames by the tracker, {len(track.bezel_read)} by the bezel read"


def test_the_frames_the_tracker_is_not_sure_of_keep_the_bezel_they_show(footage):
    track, seed = _followed(footage["shimmer"], key=screen_insert.KeySpec("green"))
    # Where the source switches, the quad is blended: the jitter gate never fails on a switch (issue #10377).
    switches = {first for source, first, _last in track.spans()[1:]}
    audits = {n: {"refined": [list(p) for p in track.quad_at(n)], "size": [W, H]} for n in range(COUNT)}
    jitter = insert_checks.quad_jitter(audits)
    assert jitter is not None and not {n for n in jitter.past if any(abs(n - s) <= 1 for s in switches)}, jitter
    # The bezel read placed frames, the tracker the others: every frame placed, every one on the screen.
    assert track.bezel_read and len(track.quads) == COUNT
    assert set(track.bezel_read) <= set(range(1, COUNT))
    assert _worst(track, seed) < ALIGNMENT_MAX_SHARE / 2, _worst(track, seed)
    by_tracker = COUNT - len(track.bezel_read)
    assert track.said() == f"{by_tracker} frames by the tracker, {len(track.bezel_read)} by the bezel read"


def test_a_monitor_that_leaves_the_frame_at_3_s_is_lost_there(footage):
    with pytest.raises(insert_tracking.InsertTrackLostError) as lost:
        _followed(footage["gone"], key=screen_insert.KeySpec("green"))
    assert lost.value.frame == GONE and lost.value.seconds == pytest.approx(3.0)
    message = str(lost.value)
    assert message.startswith(f"Scene s1: its screen insert 0: surface lost at t=3.00 s of its scene (footage frame {GONE}, 3.00 s): ")
    # Why both let it go: the tracker, and the bezel read near where it read the bezel last.
    assert ", and the bezel read finds " in message
    assert lost.value.frame_png


def test_a_bezel_read_too_far_from_where_it_read_the_bezel_before_is_no_bezel():
    room = np.asarray(_wall(np.random.default_rng(1), monitor=True, green=True).crop((MARGIN[0], MARGIN[1], MARGIN[0] + W, MARGIN[1] + H)))
    here = np.asarray(SCREEN)
    read, why = insert_tracking._bezel_read(room, here, None)
    assert read is not None and why == ""
    assert np.abs(read - here).max() < 1.5
    # Seeded 2% of the frame's width off, the read finds the bezel back - too far a step for one frame.
    read, why = insert_tracking._bezel_read(room, here + np.array([0.02 * W, 0.0]), None)
    assert read is None and "of the frame's width from where it read one before (over 0.5%)" in why


# ── The render ────────────────────────────────────────────────────────────


def _spec(quad, *, key: bool):
    overlay = {"type": "screen_insert", "source": {"file_id": SOURCE}, "quad": quad, "track": True, "duration": SECONDS}
    if key:
        overlay["key"] = {"color": "green"}
    return {"title": "Wall", "scenes": [{"id": "s1", "type": "clip", "file_id": FILE, "overlays": [overlay]}]}


@pytest.mark.parametrize("shot", ["drawn", "shimmer"])
def test_the_render_follows_it_and_draws_it_on_the_bezel_on_every_frame(footage, tmp_path, shot):
    """``track: true`` on either wall shot, its quad the bezel the save measures: followed by the render as a preview
    follows it, drawn on every frame with its alignment under 0.1%, and no insert gate fails."""
    keyed = shot == "shimmer"
    key = screen_insert.KeySpec("green") if keyed else None
    quad = [[round(float(x), 5), round(float(y), 5)] for x, y in _seed(footage[shot], key)]
    spec = parse_video_spec(_spec(quad, key=keyed))
    payload = {"src": KEY, "quad": quad, "t0": 0.0, "t1": SECONDS}
    document = {"scenes": [{"id": "s1", "type": "clip", "src": "clip/s1", "time_map": [[0.0, 0.0], [SECONDS, SECONDS]], "inserts": [payload]}]}
    assert timeline.insert_key("s1", 0) == KEY
    media = render._Media()
    media.assets.sources["clip/s1"] = compositor.FrameSource(footage[shot], float(FPS))
    # A recording-sized UI of flat panels, larger than the quad it is drawn on: never a soft insert.
    picture_path = tmp_path / "ui.png"
    blocks = np.random.default_rng(3).integers(0, 255, (45, 80, 3), dtype=np.uint8)
    Image.fromarray(blocks, "RGB").resize((1280, 720), Image.Resampling.NEAREST).save(picture_path)
    media.assets.inserts[KEY] = screen_insert.InsertSource(str(picture_path), "image", tuple(map(tuple, quad)))
    asyncio.run(render._track_inserts(spec, document, media))
    track = media.assets.inserts[KEY].track
    # Its last frame is the one at its 8 s, which the encoder holds.
    assert payload["track"] == {"from": 0, "to": COUNT, "fps": float(FPS)}
    assert bool(track.bezel_read) or not keyed

    frames = _decoded(footage[shot])
    insert = screen_insert.InsertSource(
        str(picture_path), "image", tuple(map(tuple, quad)), fit="stretch", track=track, key=key, label="Scene s1: its screen insert 0"
    )
    insert = dataclasses.replace(insert, box=tuple(screen_insert.grown_box(track.box_quad(), W / H)))
    if keyed:
        insert = screen_insert.keyed_on(insert, frames[0])
    state = screen_insert.Matting()
    picture = Image.open(picture_path).convert("RGB")
    audits = {}
    for n, under in enumerate(frames):
        layer = screen_insert.compose(insert, picture, (W, H), n, footage=under, state=state, window_frame=n)
        audits[str(n)] = layer.audit.as_dict()
    alignments = [audits[str(n)]["alignment"] for n in range(COUNT)]
    assert all(value is not None and value < 0.001 for value in alignments), max(value or 1.0 for value in alignments)
    placed = {
        KEY: insert_checks.Placed(
            KEY,
            "s1",
            insert.label,
            0.0,
            SECONDS,
            keyed=keyed,
            fit="stretch",
            key_settings=insert_checks.key_settings(insert.key) if keyed else None,
        )
    }
    found = [finding for finding, _proof in insert_checks.findings({KEY: {"frames": audits, "evidence": {}}}, placed, fps=float(FPS))]
    assert INSERT_ALIGNMENT not in {finding.kind for finding in found}
    assert [finding.kind for finding in found if finding.severity == insert_checks.FAIL] == []


def test_a_track_kept_by_a_render_keeps_the_frames_its_bezel_read_placed(monkeypatch):
    from app.services.video_studio import render_cache

    kept: dict = {}

    async def put_json(kind, digest, value):
        kept[(kind, digest)] = value

    async def get_json(kind, digest):
        return kept.get((kind, digest))

    monkeypatch.setattr(render_cache, "put_json", put_json)
    monkeypatch.setattr(render_cache, "get_json", get_json)
    quads = tuple(((0.3 + 0.001 * n, 0.3), (0.6, 0.3), (0.6, 0.6), (0.3, 0.6)) for n in range(6))
    track = insert_tracking.InsertTrack(4, quads, (0.3, 0.3, 0.605, 0.6), (6, 7))
    asyncio.run(render._keep_tracks("d", [track]))
    [again] = asyncio.run(render._kept_tracks("d", 1))
    assert again == track and again.said() == "4 frames by the tracker, 2 by the bezel read"
    # A wall's pane keeps them too.
    assert insert_tracking.pane_track(track, 1, 2, 0.02, 1).bezel_read == (6, 7)


# ── The save ──────────────────────────────────────────────────────────────


def _saved(monkeypatch, path: str, quad, kept: dict | None = None) -> tuple[list[insert_bezels.Measured], insert_bezels.Resolution]:
    """What the save measures of a tracked keyed insert on the footage at *path*, given *quad*, and what it makes of it;
    *kept* is what the render cache holds."""
    from app.services.video_studio import assets, render_cache

    kept = {} if kept is None else kept

    async def footage_of(_db, _scene):
        return (("file", FILE), FILE, False)

    async def download(_file_id, out):
        shutil.copyfile(path, out)
        return SimpleNamespace(filename="wall.mp4")

    async def get_json(kind, digest):
        return kept.get((kind, digest))

    async def put_json(kind, digest, value):
        kept[(kind, digest)] = value

    async def put_file(kind, digest, file_path, *, suffix=""):
        kept[(kind, digest, suffix)] = file_path

    monkeypatch.setattr(insert_bezels, "_footage_of", footage_of)
    monkeypatch.setattr(assets, "download_file", download)
    monkeypatch.setattr(render_cache, "get_json", get_json)
    monkeypatch.setattr(render_cache, "put_json", put_json)
    monkeypatch.setattr(render_cache, "put_file", put_file)
    # The tracks of a save have their own budget: a slow CI runner is given room.
    monkeypatch.setattr(insert_bezels, "TRACK_SECONDS", 300.0)
    authored = validate_video_spec(_spec(quad, key=True))
    measured = asyncio.run(insert_bezels.measure(None, parse_video_spec(authored), tracks=True))
    return measured, insert_bezels.resolve(authored, measured)


def test_the_save_seeds_the_track_on_the_bezel_and_says_who_placed_its_frames(footage, monkeypatch):
    # Given a little off the screen, as an author reads four corners off a frame.
    off = [[round(float(x) + 0.006, 5), round(float(y) - 0.004, 5)] for x, y in _truth(0)]
    cache: dict = {}
    [item], resolution = _saved(monkeypatch, footage["shimmer"], off, cache)
    assert item.tracked and item.bezel is not None and item.bezel.found_by == "seed"
    # Its quad snapped to the bezel measured on its first frame: where its track starts.
    [(written, error)] = resolution.written.values()
    assert np.abs(np.asarray(written) - _truth(0)).max() < 0.004 and error > 0.004
    assert resolution.authored["scenes"][0]["overlays"][0]["quad"] == [list(p) for p in written]
    assert not resolution.problems and not resolution.refused
    track = item.track
    # Every frame it shows, from its first to the one at its 8 s.
    assert track["tracker"] + track["bezel_read"] == COUNT + 1 and track["bezel_read"] > 0
    said = f"Scene s1: its screen insert 0, track: {track['tracker']} frames by the tracker, {track['bezel_read']} by the bezel read"
    # ... and the runs of frames each source placed, the switches between them blended.
    assert track["spans"].startswith("frames 0") and f"{said} ({track['spans']})." in resolution.notes
    # Saved again on the same footage, its measure and its track are read back, never followed again.
    monkeypatch.setattr(insert_tracking, "track_all", _never)
    [again], _resolution = _saved(monkeypatch, footage["shimmer"], off, cache)
    assert again.track == track


async def _never(*_args, **_kwargs):
    pytest.fail("followed again")


def test_the_save_says_where_a_preview_would_lose_the_surface(footage, monkeypatch):
    [item], resolution = _saved(monkeypatch, footage["gone"], [[round(float(x), 5), round(float(y), 5)] for x, y in _truth(0)])
    assert item.track["frame"] == GONE and item.track["seconds"] == pytest.approx(3.0)
    [note] = [note for note in resolution.notes if "surface lost" in note]
    assert note.startswith("Scene s1: its screen insert 0: surface lost at t=3.00 s of its scene")
    assert note.endswith("(followed at save: a preview of scene s1 fails there)")
    assert not resolution.problems


def test_a_tracked_insert_is_measured_only_by_the_save():
    """The render's and the quality check's measures read the static gates' inserts alone: a tracked one is the save's."""
    spec = parse_video_spec(validate_video_spec(_spec([[0.33, 0.28], [0.67, 0.28], [0.67, 0.63], [0.33, 0.63]], key=True)))
    assert asyncio.run(insert_bezels.measure(None, spec)) == []
