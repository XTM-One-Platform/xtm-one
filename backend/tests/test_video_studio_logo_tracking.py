"""A logo ingredient drawn once on an object that moves, rolls and rests. Synthetic footage only.

* the tracker proposes one candidate per plane its region holds, and
  ``step`` stays the adoption of the first;
* of several candidates the most confident is drawn, continuity telling
  apart the ones alike - a card carrying the logo across a textured table is
  followed, never the table left behind;
* a cylinder rolling 180 degrees shows the logo once while it faces the
  camera, faded out over 3 frames as it turns away, and nowhere once it faces
  away;
* an object at rest keeps the quad of its first stable frame, a folded mesh
  is never drawn;
* the duplicate gate: a logo shown twice in a frame is found at its tracked
  size, recorded on the take with that frame, and fails every render's scene
  playing it - in its scene's time - like an audio failure.
"""

from __future__ import annotations

import asyncio
import io
import math
import uuid
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.services.video_studio import frame_qc, logos, render_checks, runners, tracking
from tests.test_video_studio_shot_rescue import FFMPEG, FPS, SIZE, _box, _decoded, _encode, _logo, _shoot, _world

W, H = SIZE


# ── The tracker's candidates ──────────────────────────────────────────────


def _two_planes(seed: int = 4) -> tuple[np.ndarray, np.ndarray, Image.Image]:
    """A frame whose left half is one textured plane and right half another, and the next frame: each moved its own way."""
    rng = np.random.default_rng(seed)
    first = Image.new("L", (W, H), 90)
    draw = ImageDraw.Draw(first)
    for _ in range(900):
        x, y = int(rng.integers(0, W)), int(rng.integers(0, H))
        draw.ellipse([x, y, x + 6, y + 6], fill=int(rng.integers(0, 255)))
    left, right = first.crop((0, 0, W // 2, H)), first.crop((W // 2, 0, W, H))
    second = Image.new("L", (W, H), 90)
    second.paste(left.transform(left.size, Image.Transform.AFFINE, (1, 0, -3, 0, 1, 0)), (0, 0))
    second.paste(right.transform(right.size, Image.Transform.AFFINE, (1, 0, 4, 0, 1, -2)), (W // 2, 0))
    return np.asarray(first, dtype=np.float32), np.asarray(second, dtype=np.float32), first


def test_a_region_holding_two_planes_proposes_both_and_step_adopts_the_first():
    first, second, _ = _two_planes()
    proposing = tracking.PlanarTracker(first, max_points=300, min_distance=5, min_inliers=10, threshold=1.5)
    stepping = tracking.PlanarTracker(first, max_points=300, min_distance=5, min_inliers=10, threshold=1.5)
    proposal = proposing.propose(second, planes=3)
    assert len(proposal.planes) >= 2
    # Each candidate moves its own points as its half moved: the left one 3 px right, the right one 4 left, 2 down.
    moves = []
    for plane in proposal.planes[:2]:
        centre = proposal.reference[plane.inliers].mean(axis=0)
        moves.append(tuple(np.round(tracking.project(plane.homography, centre)[0] - centre).astype(int)))
    assert sorted(moves) == [(-4, 2), (3, 0)], moves
    # The candidates share no point, and every point they hold was found in the reference frame.
    assert not (proposal.planes[0].inliers & proposal.planes[1].inliers).any() and proposal.original.all()
    step = stepping.step(second)
    adopted = proposing.adopt(proposal, 0)
    assert np.allclose(step.homography, adopted.homography) and step.inliers == adopted.inliers
    # A point refilled later is never taken for one found in the reference frame.
    assert len(proposing.original) == len(proposing.points) and proposing.original[: adopted.inliers].all()


def test_the_most_confident_candidate_is_drawn_and_continuity_tells_alike_ones_apart():
    last = np.array([[100.0, 100.0], [200.0, 100.0], [200.0, 150.0], [100.0, 150.0]])
    still, moved = last.copy(), last + np.array([12.0, 0.0])
    # The more confident wins, however far it moved.
    assert logos._choose([(0, still, 0.55), (1, moved, 0.95)], last)[0] == 1
    # Within the tie, the one whose corners moved least.
    assert logos._choose([(0, moved, 0.93), (1, still, 0.9)], last)[0] == 1
    # Nothing drawn before: the most confident.
    assert logos._choose([(0, moved, 0.9), (1, still, 0.8)], None)[0] == 0


def _card(logo: Image.Image) -> Image.Image:
    rng = np.random.default_rng(5)
    picture = Image.new("RGB", (150, 90), (230, 225, 215))
    draw = ImageDraw.Draw(picture)
    for _ in range(40):
        x, y = int(rng.integers(0, 150)), int(rng.integers(0, 90))
        r = int(rng.integers(3, 8))
        draw.ellipse([x, y, x + r, y + r], fill=tuple(int(v) for v in rng.integers(40, 200, 3)))
    mark = logo.resize((110, round(110 * logo.height / logo.width)), Image.Resampling.LANCZOS)
    picture.paste(mark, ((150 - mark.width) // 2, (90 - mark.height) // 2), mark)
    return picture


@FFMPEG
def test_a_card_carrying_the_logo_across_a_textured_table_is_followed_never_the_table(tmp_path):
    logo, table = _logo(), _shoot(_world(seed=9), _box(0, pan=0, zoom=0))
    card, mark_height = _card(logo), round(110 * logo.height / logo.width)
    frames, truth = [], []
    for index in range(36):
        # The table holds most of the points the logo's region follows; the card turns a corner half way.
        x, y = 200 + 4 * index, 130 + (2 * index if index > 18 else 0)
        frame = table.copy()
        frame.paste(card, (x, y))
        frames.append(frame)
        truth.append((x + 20, y + (90 - mark_height) // 2))
    video = _encode(tmp_path, frames)
    found = logos.locate(logo, frames[0])
    [track] = asyncio.run(logos._track(video, frames[0], [found[0]], logos._working_size(SIZE, [found[0]]), FPS, [logos.Logo(logo, "x")]))
    for index in range(36):
        corner = track[index].quad[0] * [W, H]
        assert np.abs(corner - truth[index]).max() < 3.0, (index, corner, truth[index])
    out = str(tmp_path / "logo.mp4")
    done = asyncio.run(logos.composite(video, out, [logos.Logo(logo, "the brand logo")], frames[0]))
    assert done.check["status"] == "passed" and done.check["frames_sampled"] >= 12 and done.duplicate is None


# ── A cylinder rolling away ───────────────────────────────────────────────

_CX, _TOP, _BOTTOM, _R, _ROLL_FRAMES = 320, 50, 310, 130, 60


def _surface(logo: Image.Image) -> np.ndarray:
    """The cylinder's surface unrolled: flecks to follow, the logo printed where it faces the camera at first."""
    circumference = round(2 * math.pi * _R)
    rng = np.random.default_rng(11)
    surface = Image.new("RGB", (circumference, _BOTTOM - _TOP), (225, 220, 210))
    draw = ImageDraw.Draw(surface)
    for _ in range(420):
        x, y = int(rng.integers(0, circumference)), int(rng.integers(0, _BOTTOM - _TOP))
        r = int(rng.integers(3, 14))
        draw.ellipse([x, y, x + r, y + r], fill=tuple(int(v) for v in rng.integers(40, 200, 3)))
    mark = logo.resize((110, round(110 * logo.height / logo.width)), Image.Resampling.LANCZOS)
    surface.paste(mark, (circumference // 2 - mark.width // 2, (_BOTTOM - _TOP) // 2 - mark.height // 2), mark)
    return np.asarray(surface, dtype=np.float32)


def _rolled(background: np.ndarray, surface: np.ndarray, angle: float) -> Image.Image:
    """The cylinder (axis upright, seen from afar) turned by *angle*: the logo moves to its left edge, then behind."""
    frame = background.copy()
    circumference = surface.shape[1]
    xs = np.arange(_CX - _R + 1, _CX + _R)
    facing = np.arcsin((xs - _CX) / _R)
    u = ((facing + angle) * _R + circumference / 2) % circumference
    u0 = np.floor(u).astype(int) % circumference
    weight = (u - np.floor(u))[None, :, None]
    strip = surface[:, u0] * (1 - weight) + surface[:, (u0 + 1) % circumference] * weight
    frame[_TOP:_BOTTOM, xs[0] : xs[-1] + 1] = strip * (0.75 + 0.25 * np.cos(facing))[None, :, None]
    return Image.fromarray(np.clip(frame, 0, 255).astype(np.uint8), "RGB")


@FFMPEG
def test_a_logo_on_a_cylinder_rolling_half_a_turn_shows_once_a_frame_and_nowhere_once_it_faces_away(tmp_path):
    logo = _logo()
    background = np.asarray(_shoot(_world(seed=21), _box(0, pan=0, zoom=0)), dtype=np.float32)
    surface = _surface(logo)
    angles = [math.pi * index / (_ROLL_FRAMES - 1) for index in range(_ROLL_FRAMES)]
    frames = [_rolled(background, surface, angle) for angle in angles]
    video, out = _encode(tmp_path, frames), str(tmp_path / "logo.mp4")
    found = logos.locate(logo, frames[0])
    assert found is not None
    done = asyncio.run(logos.composite(video, out, [logos.Logo(logo, "the brand logo")], frames[0]))
    assert done.duplicate is None and done.check["status"] == "passed"
    assert any(note.startswith("The brand logo turns away from the camera: faded out over 3 frames") for note in done.notes)
    [track] = asyncio.run(logos._track(video, frames[0], [found[0]], logos._working_size(SIZE, [found[0]]), FPS, [logos.Logo(logo, "x")]))
    drawn = _decoded(out)
    start_quad = np.asarray(found[0]) * [W, H]
    for index, frame in enumerate(drawn[:_ROLL_FRAMES]):
        roll = math.degrees(angles[index])
        place = track[index]
        quad = place.quad * [W, H] if place is not None else start_quad
        shown = logos.matches(frame, logo, quad)
        # Never more than one logo in a frame.
        assert len(shown) <= 1, (index, roll, shown)
        if roll <= 45:
            assert place is not None and place.opacity == 1.0 and len(shown) == 1, (index, roll)
        if roll >= 90:
            # Facing away: drawn nowhere, and nothing of it shows.
            assert place is None and not shown, (index, roll)
    # It fades out over 3 frames - 2/3, 1/3, then nothing - instead of being squeezed into a smear.
    first_gone = next(index for index, place in enumerate(track) if place is None)
    assert [round(track[first_gone - 2].opacity, 2), round(track[first_gone - 1].opacity, 2)] == [0.67, 0.33]
    assert all(place.opacity == 1.0 for place in track[: first_gone - 2])
    assert all(place is None for place in track[first_gone:])


def test_the_turn_of_a_surface_is_read_off_its_foreshortening_whichever_way_it_turned():
    square = np.array([[0.0, 0.0], [100.0, 0.0], [100.0, 50.0], [0.0, 50.0]])
    assert logos._turned(square, 2.0) == pytest.approx(0.0, abs=0.01)
    narrower = square * [math.cos(math.radians(60)), 1.0]
    lower = square * [1.0, math.cos(math.radians(70))]
    assert logos._turned(narrower, 2.0) == pytest.approx(60.0, abs=0.1)
    assert logos._turned(lower, 2.0) == pytest.approx(70.0, abs=0.1)
    assert logos._turned(square * [0.0, 1.0], 2.0) == 90.0


# ── At rest, and a folded mesh ────────────────────────────────────────────


def _place(x: float, y: float, opacity: float = 1.0) -> logos.Placement:
    quad = np.array([[x, y], [x + 100, y], [x + 100, y + 50], [x, y + 50]]) / [W, H]
    return logos.Placement(quad, None, opacity)


def test_an_object_at_rest_keeps_the_quad_of_its_first_stable_frame_until_it_moves_again():
    rng = np.random.default_rng(2)
    moving = [_place(100 + 5 * index, 100) for index in range(5)]
    resting = [_place(130 + rng.uniform(-0.3, 0.3), 100 + rng.uniform(-0.3, 0.3)) for _ in range(10)]
    leaving = [_place(140 + 5 * index, 100) for index in range(4)]
    track = [*moving, *resting, None, *leaving]
    settled, kept = logos._settled(track, SIZE)
    # Moving: as tracked. Resting: the first still frame's quad in every frame after it, no shimmer.
    assert all(np.array_equal(settled[n].quad, track[n].quad) for n in range(5))
    first = resting[0].quad
    assert all(np.array_equal(settled[5 + n].quad, first) for n in range(10)) and kept == 9
    # Gone, then moving again: as tracked.
    assert settled[15] is None and all(np.array_equal(settled[16 + n].quad, leaving[n].quad) for n in range(4))


def test_a_mesh_whose_cells_fold_is_never_drawn():
    flat = logos._mesh_vertices([(0.2, 0.2), (0.6, 0.2), (0.6, 0.5), (0.2, 0.5)], logos.MESH_CELLS)
    assert not logos._folds(flat)
    folded = flat.copy()
    # One column of vertices pushed past its neighbour: its cells turn over, the logo drawn twice there.
    folded[:, 3, 0] = folded[:, 5, 0] + 0.01
    assert logos._folds(folded)


# ── The duplicate gate ────────────────────────────────────────────────────


def _twice(tmp_path: Path) -> tuple[str, list[Image.Image]]:
    logo = _logo()
    frames = []
    for index in range(12):
        frame = _shoot(_world(seed=3), _box(index, pan=1.0, zoom=0.0))
        frame.paste(logo, (120, 140), logo)
        if index >= 6:
            # The same mark a second time: what drawing every candidate does.
            frame.paste(logo, (380, 160), logo)
        frames.append(frame)
    return _encode(tmp_path, frames, "twice.mp4"), frames


@FFMPEG
def test_a_logo_shown_twice_in_a_frame_fails_the_gate_with_that_frame(tmp_path):
    video, _frames = _twice(tmp_path)
    logo = logos.Logo(_logo(), "the brand logo")
    quad = np.array([[120, 140], [280, 140], [280, 220], [120, 220]], dtype=float) / [W, H]
    track = [logos.Placement(quad) for _ in range(12)]
    duplicate, sampled = asyncio.run(logos.find_duplicates(video, [logo], [track], width=W, height=H, fps=FPS))
    assert duplicate is not None and sampled == 3
    # Sampled every 3 frames: frame 6 is the first sampled that shows it twice.
    assert (duplicate.frame, duplicate.count) == (6, 2) and duplicate.seconds == pytest.approx(6 / FPS)
    assert duplicate.reason().startswith("logo duplicated at t=0.25 s (frame 6) of the shot: the brand logo shows 2 times")
    assert Image.open(io.BytesIO(duplicate.frame_jpeg)).size == SIZE
    # A frame that shows it once is no duplicate.
    clean = np.asarray(_frames[0])
    assert len(logos.matches(clean, logo.picture, quad * [W, H])) == 1


def test_the_take_keeps_its_check_and_the_frame_with_its_frames(monkeypatch):
    stored: list[dict] = []

    async def store_bytes(data, **kwargs):
        stored.append({"data": data, **kwargs})
        return SimpleNamespace(id=uuid.UUID(int=6191))

    monkeypatch.setattr(runners.assets, "store_bytes", store_bytes)
    duplicate = logos.Duplicate("the brand logo", 6, 0.25, ((0.1, 0.2, 0.3, 0.4), (0.5, 0.6, 0.7, 0.8)), b"\xff\xd8jpeg")
    check = {"status": "duplicated", "frames_sampled": 3, "label": "the brand logo", "frame": 6, "seconds": 0.25, "count": 2}
    params: dict = {frame_qc.QC_KEY: {frame_qc.STABILISATION: {"applied": False}}}
    composited = logos.Composited(12, ["the brand logo"], [], check, duplicate)
    asyncio.run(runners._record_logo_check(composited, params, base="shot", user_id=uuid.uuid4()))
    record = frame_qc.logo_check(params[frame_qc.QC_KEY])
    assert record == {**check, "frame_file_id": str(uuid.UUID(int=6191))} and stored[0]["data"] == b"\xff\xd8jpeg"
    # The stabilisation stays; the frame is a file of the take, deleted with it.
    assert params[frame_qc.QC_KEY][frame_qc.STABILISATION] == {"applied": False}
    assert str(uuid.UUID(int=6191)) in frame_qc.frame_file_ids(params)
    assert frame_qc.summary(params)["logo_check"]["status"] == "duplicated"


def _document() -> dict:
    return {
        "scenes": [
            {"id": "intro", "type": "title", "start": 0.0, "end": 3.0},
            {"id": "cat-cube", "type": "clip", "start": 3.0, "end": 9.0, "time_map": [[0.0, 0.5], [6.0, 6.5]]},
            {"id": "mug", "type": "statement", "start": 9.0, "end": 12.0, "backdrop": {"time_map": [[0.0, 0.0], [3.0, 3.0]]}},
        ]
    }


def test_a_duplicated_logo_fails_the_scene_of_every_render_playing_it_in_the_scenes_time():
    check = {"status": "duplicated", "label": "the brand logo", "frame": 48, "seconds": 2.0, "count": 2, "frame_file_id": "f1"}
    shots = {"cat-cube": "clip-a", "mug": "clip-b"}
    found = logos.duplicate_findings(_document(), shots, {"clip-a": check, "clip-b": {"status": "passed"}})
    assert len(found) == 1
    finding, frame_file = found[0]
    # Take second 2.0 is scene second 1.5 (the scene starts half a second into the take), 4.5 s into the video.
    assert finding.kind == render_checks.LOGO_DUPLICATED and finding.severity == render_checks.FAIL
    assert finding.scene_id == "cat-cube" and finding.at == pytest.approx(4.5) and frame_file == "f1"
    assert finding.message.startswith("logo duplicated at t=0:04.50: Scene cat-cube: its AI shot shows the brand logo 2 times")
    # A scene preview: in the MP4's own time, and nothing outside its window.
    preview = logos.duplicate_findings(_document(), shots, {"clip-a": check}, offset=3.0, span=(3.0, 6.0))
    assert preview[0][0].at == pytest.approx(1.5)
    assert logos.duplicate_findings(_document(), shots, {"clip-a": check}, offset=9.0, span=(9.0, 3.0)) == []
    # A frame the scene does not play (past its time map) is not this render's.
    late = {**check, "seconds": 7.5}
    assert logos.duplicate_findings(_document(), shots, {"clip-a": late}) == []
    # Like an audio failure: the next render of the revision is refused until it is fixed.
    checks = render_checks.Checks(findings=[finding]).as_dict()
    assert render_checks.LOGO_DUPLICATED in render_checks.PICTURE_KINDS
    assert render_checks.open_findings(checks, same_quality=False) and not render_checks.scene_passed(checks, "cat-cube")


def test_two_copies_of_one_ingredient_at_the_same_place_are_drawn_once():
    logo = _logo()
    quad = ((0.2, 0.2), (0.45, 0.2), (0.45, 0.42), (0.2, 0.42))
    placed = [(logos.Logo(logo, "the brand logo"), quad)]
    assert logos._drawn_already(logos.Logo(logo.copy(), "its mark"), quad, placed) == "the brand logo"
    # Elsewhere, or another picture: drawn too.
    elsewhere = tuple((x + 0.4, y) for x, y in quad)
    assert logos._drawn_already(logos.Logo(logo.copy(), "its mark"), elsewhere, placed) is None
    assert logos._drawn_already(logos.Logo(logo.rotate(90, expand=True), "another"), quad, placed) is None
