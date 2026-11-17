"""Rescuing AI shots in code: stabilisation, tracked screen inserts, logos drawn back. Synthetic footage only.

Every video here is drawn by the test - a textured "world" seen through a
camera whose motion is known exactly - and encoded with ffmpeg, so each
measure is checked against the truth:

* the tracking core recovers a known homography through outliers, and
  follows a plane through a drifting camera;
* a take whose camera drifts is locked to its first frame (its estimated
  homographies match the camera's), kept only when it moves less, the raw
  take kept otherwise and the reason said: no drift, a camera that cannot be
  followed (with the frame), a drift too large to crop;
* through ``run_clip``: a drifting take asked still is rescued instead of
  failed as "camera moved", its un-stabilised take kept as a file of the
  take; ``stabilise: false`` fails it as before;
* a tracked screen insert's quad follows a monitor through a moving camera,
  the render hands the page its frames and box, the driver draws each footage
  frame into its own quad, and a surface lost fails loudly with its frame;
* a logo is found in the start frame, drawn back with its own pixels over
  the model's garbled one in every frame of a drifting shot, and fails the
  take as "logo lost" when the shot cuts away; a model without a native path
  composites it (``ingredients_path: composited``).
"""

from __future__ import annotations

import asyncio
import io
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter, ImageOps

from app.llm.video import VideoResult
from app.models.video_studio import JOB_FAILED, JOB_PENDING, JOB_READY, VideoClip
from app.services.video_studio import (
    assets,
    clips,
    compositor,
    content_drift,
    encoder,
    frame_qc,
    insert_tracking,
    jobs,
    logos,
    render,
    runners,
    screen_insert,
    screens,
    stabilise,
    text_drift,
    timeline,
    tracking,
)

FFMPEG = pytest.mark.skipif(not encoder.encoder_available(), reason="ffmpeg is not installed")
USER = SimpleNamespace(id=uuid.UUID("00000000-0000-0000-0000-00000000c064"), role="user")
LOGO_FILE = "00000000-0000-0000-0000-00000000c065"
START = "00000000-0000-0000-0000-00000000c066"
SIZE = (640, 360)
FPS = 24


# ── Synthetic footage ─────────────────────────────────────────────────────


def _world(size: tuple[int, int] = (1600, 1000), seed: int = 3) -> Image.Image:
    """A textured picture to film: a gradient, and shapes of every size and colour."""
    rng = np.random.default_rng(seed)
    width, height = size
    picture = Image.new("RGB", size)
    draw = ImageDraw.Draw(picture)
    for y in range(height):
        draw.line([(0, y), (width, y)], fill=(40 + y * 120 // height, 70, 150 - y * 90 // height))
    for _ in range(320):
        x, y = int(rng.integers(0, width)), int(rng.integers(0, height))
        r = int(rng.integers(6, 42))
        colour = tuple(int(v) for v in rng.integers(0, 255, 3))
        if rng.random() < 0.5:
            draw.ellipse([x, y, x + r, y + r], fill=colour)
        else:
            draw.rectangle([x, y, x + r, y + r // 2], fill=colour)
    return picture


def _box(index: int, *, pan: float, zoom: float, centre=(800.0, 500.0), span: float = 1100.0) -> tuple[float, ...]:
    """The part of the world frame *index* shows: a camera panning *pan* px and pushing in *zoom* a frame."""
    scale = 1.0 + zoom * index
    width, height = span / scale, span * SIZE[1] / SIZE[0] / scale
    cx, cy = centre[0] + pan * index, centre[1] + 0.5 * pan * index
    return (cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2)


def _shoot(world: Image.Image, box: tuple[float, ...]) -> Image.Image:
    return world.transform(SIZE, Image.Transform.EXTENT, box, resample=Image.Resampling.BICUBIC)


def _to_frame(box: tuple[float, ...]) -> np.ndarray:
    """The map from a frame's pixels to the world's, for the part of the world *box* it shows."""
    x0, y0, x1, y1 = box
    return np.array([[(x1 - x0) / SIZE[0], 0.0, x0], [0.0, (y1 - y0) / SIZE[1], y0], [0.0, 0.0, 1.0]])


def _truth(first: tuple[float, ...], box: tuple[float, ...]) -> np.ndarray:
    """The homography from frame 0's pixels to the pixels of the frame showing *box*: the camera's own motion."""
    return np.linalg.inv(_to_frame(box)) @ _to_frame(first)


def _encode(tmp_path: Path, frames: list[Image.Image], name: str = "shot.mp4") -> str:
    folder = tmp_path / name.replace(".", "-")
    folder.mkdir()
    names = []
    for index, frame in enumerate(frames):
        frame.save(folder / f"f{index:03d}.png")
        names.append((index / FPS, f"f{index:03d}.png"))
    out = str(tmp_path / name)
    asyncio.run(
        encoder.frames_to_mezzanine(
            names, end_time=len(frames) / FPS, out_path=out, fps=FPS, crf=12, preset="ultrafast", workdir=str(folder)
        )
    )
    return out


def _decoded(path: str, size: tuple[int, int] = SIZE) -> list[np.ndarray]:
    async def read() -> list[np.ndarray]:
        return [
            np.frombuffer(raw, dtype=np.uint8).reshape(size[1], size[0], 3)
            async for raw in encoder.iter_rgb_frames(path, width=size[0], height=size[1])
        ]

    return asyncio.run(read())


def _drifting(
    world: Image.Image, count: int = 36, *, pan: float = 2.0, zoom: float = 0.003, centre=(800.0, 500.0)
) -> tuple[list[Image.Image], list]:
    boxes = [_box(i, pan=pan, zoom=zoom, centre=centre) for i in range(count)]
    return [_shoot(world, box) for box in boxes], boxes


# ── The tracking core ─────────────────────────────────────────────────────


def test_ransac_recovers_a_known_homography_through_outliers():
    rng = np.random.default_rng(1)
    truth = np.array([[1.02, 0.03, 12.0], [-0.02, 0.98, -7.0], [0.0001, -0.00005, 1.0]])
    source = rng.uniform(0, 600, (200, 2))
    target = tracking.project(truth, source) + rng.normal(0, 0.2, (200, 2))
    # A third of the points moved with something else in the scene.
    target[:70] += rng.uniform(-60, 60, (70, 2))
    found, inliers = tracking.ransac_homography(source, target)
    assert found is not None and inliers[70:].mean() > 0.95 and inliers[:70].mean() < 0.1
    corners = np.array([[0, 0], [600, 0], [600, 600], [0, 600]], float)
    assert np.abs(tracking.project(found, corners) - tracking.project(truth, corners)).max() < 1.0
    assert tracking.ransac_homography(source[:3], target[:3])[0] is None


def test_the_tracker_follows_a_plane_and_says_when_it_cannot():
    world = _world()
    first = _box(0, pan=1.5, zoom=0.002)
    reference = tracking.luma(np.asarray(_shoot(world, first)))
    tracker = tracking.PlanarTracker(reference)
    for index in range(1, 20):
        box = _box(index, pan=1.5, zoom=0.002)
        step = tracker.step(tracking.luma(np.asarray(_shoot(world, box))))
        assert step.found, index
        corners = np.array([[40, 40], [600, 40], [600, 320], [40, 320]], float)
        error = np.abs(tracking.project(step.homography, corners) - tracking.project(_truth(first, box), corners)).max()
        assert error < 1.0, (index, error)
    # Another place entirely: nothing it follows is there.
    elsewhere = tracking.luma(np.asarray(_shoot(_world(seed=11), first)))
    assert not tracker.step(elsewhere).found


# ── 1. Stabilisation ──────────────────────────────────────────────────────


@FFMPEG
def test_a_drifting_take_is_locked_to_its_first_frame(tmp_path):
    frames, boxes = _drifting(_world())
    raw = _encode(tmp_path, frames)
    homographies, _first, _last, lost = asyncio.run(stabilise.estimate(raw, size=SIZE, fps=FPS))
    assert lost is None and len(homographies) >= len(frames)
    # Each frame's homography against frame 0 is the camera's own motion.
    corners = np.array([[0, 0], [SIZE[0], 0], [SIZE[0], SIZE[1]], [0, SIZE[1]]], float)
    for index in (5, 20, len(frames) - 1):
        error = np.abs(tracking.project(homographies[index], corners) - tracking.project(_truth(boxes[0], boxes[index]), corners))
        assert error.max() < 1.5, (index, error.max())
    out = str(tmp_path / "steady.mp4")
    result = asyncio.run(stabilise.stabilise(raw, out))
    assert result.applied and result.raw_motion > 0.15 and result.residual_motion < 0.05
    assert stabilise.MIN_KEPT_SHARE <= result.kept_share < 1.0 and result.max_drift > stabilise.MIN_DRIFT
    steady = _decoded(out)
    assert len(steady) == len(_decoded(raw))
    assert frame_qc.ssim(Image.fromarray(steady[0]), Image.fromarray(steady[-1])) > 0.9
    record = result.as_params()
    assert record["applied"] is True and len(record["crop"]) == 4
    assert stabilise.note(record).startswith("Stabilised: the camera drifted up to ")


@FFMPEG
def test_a_still_take_is_left_as_it_is(tmp_path):
    still = _shoot(_world(), _box(0, pan=0, zoom=0))
    raw = _encode(tmp_path, [still] * 12)
    out = str(tmp_path / "steady.mp4")
    result = asyncio.run(stabilise.stabilise(raw, out))
    assert not result.applied and result.reason == stabilise.NO_DRIFT and not Path(out).exists()
    # Nothing to correct is nothing to say: the record keeps it, the shot's notes do not.
    assert stabilise.note(result.as_params()) is None


@FFMPEG
def test_a_camera_that_cannot_be_followed_keeps_the_raw_take_and_says_from_which_frame(tmp_path):
    frames, _boxes = _drifting(_world(), 20, pan=1.0)
    cut = frames[:10] + [_shoot(_world(seed=29), _box(i, pan=1.0, zoom=0.003)) for i in range(10, 20)]
    raw = _encode(tmp_path, cut)
    out = str(tmp_path / "steady.mp4")
    result = asyncio.run(stabilise.stabilise(raw, out))
    assert not result.applied and result.lost_at == 10 and not Path(out).exists()
    assert result.reason == "its camera could not be followed past frame 9 (0.4 s)"
    assert stabilise.note(result.as_params()).startswith("Not stabilised: its camera could not be followed")


@FFMPEG
def test_a_drift_too_large_to_crop_keeps_the_raw_take(tmp_path):
    # A third of the frame's width in a second and a quarter: locking it off would zoom in too far.
    frames, _boxes = _drifting(_world(), 30, pan=12.0, zoom=0.0, centre=(600.0, 450.0))
    raw = _encode(tmp_path, frames)
    result = asyncio.run(stabilise.stabilise(raw, str(tmp_path / "steady.mp4")))
    assert not result.applied and result.kept_share < stabilise.MIN_KEPT_SHARE
    assert "locking it would keep only" in result.reason


def test_a_take_is_stabilised_when_asked_or_when_its_camera_is_asked_still():
    assert clips.wants_stabilising({clips.STABILISE: True}, content_drift.CAMERA_MOVING)
    assert not clips.wants_stabilising({clips.STABILISE: False, frame_qc.MAX_CAMERA_MOTION: 0.2}, None)
    assert clips.wants_stabilising({frame_qc.MAX_CAMERA_MOTION: 0.2}, None)
    assert clips.wants_stabilising({}, content_drift.CAMERA_LOCKED)
    assert not clips.wants_stabilising({}, content_drift.CAMERA_MOVING)
    assert not clips.wants_stabilising({}, None)
    assert clips.stabilise_choice(None) is None and clips.stabilise_choice(False) is False
    with pytest.raises(clips.ClipError, match="stabilise is true"):
        clips.stabilise_choice("yes")


# ── run_clip on real footage ──────────────────────────────────────────────


def _png(picture: Image.Image) -> bytes:
    out = io.BytesIO()
    picture.save(out, format="PNG")
    return out.getvalue()


@pytest.fixture
def take(monkeypatch, tmp_path):
    """``run_clip`` on one shot whose provider answers *world.video*; the store keeps real files."""
    row = VideoClip(
        id=uuid.uuid4(),
        user_id=USER.id,
        provider="gemini",
        model="veo-3.1-generate-preview",
        prompt="The booth at dusk",
        params={"provider_id": str(uuid.uuid4()), "duration_seconds": 8, "reference_file_id": START, "content_check": False},
        status=JOB_PENDING,
    )
    world = SimpleNamespace(row=row, video=b"", start=b"", files={}, deleted=[], logo=b"")

    class _Db:
        async def get(self, _model, _key, **_kwargs):
            return row

        async def commit(self):
            return None

    @asynccontextmanager
    async def session():
        yield _Db()

    class _Provider:
        async def generate_video(self, request, **_kwargs):
            return VideoResult(data=world.video, job_id="op/1")

        def max_reference_images(self, _model):
            return 0

        def reference_images_with_start_frame(self, _model):
            return False

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        return _Provider(), model, SimpleNamespace(id=provider_id, provider="gemini")

    async def mark(_kind, _job_id, **changes):
        for key, value in changes.items():
            setattr(row, key, value)

    async def store_path(path, **kwargs):
        stored = SimpleNamespace(id=uuid.uuid4(), size=Path(path).stat().st_size, **kwargs)
        world.files[str(stored.id)] = Path(path).read_bytes()
        return stored

    async def store_bytes(data, **kwargs):
        stored = SimpleNamespace(id=uuid.uuid4(), size=len(data), **kwargs)
        world.files[str(stored.id)] = data
        return stored

    async def download_file(file_id, target):
        data = world.logo if str(file_id) == LOGO_FILE else world.files.get(str(file_id))
        if data is None:
            return None
        Path(target).write_bytes(data)
        return SimpleNamespace(content_type="image/png" if str(file_id) == LOGO_FILE else "video/mp4")

    async def delete_files(ids):
        world.deleted.extend(str(i) for i in ids or [] if i)

    async def start_frame(params, _workdir, **_kwargs):
        return clips.StartFrame(world.start, "image/png", file_id=START) if params.get("reference_file_id") else None

    async def nothing(*_args, **_kwargs):
        return []

    async def text_check(*_args, **_kwargs):
        return text_drift.TextCheck(True)

    async def content_check(*_args, **_kwargs):
        return content_drift.ContentCheck(True)

    monkeypatch.setattr(runners, "async_session", session)
    monkeypatch.setattr(runners, "bind_llm_caller", lambda *_a, **_k: asyncio.sleep(0))
    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(clips, "start_frame", start_frame)
    monkeypatch.setattr(clips, "reference_pictures", nothing)
    monkeypatch.setattr(jobs, "mark", mark)
    monkeypatch.setattr(assets, "store_path", store_path)
    monkeypatch.setattr(assets, "store_bytes", store_bytes)
    monkeypatch.setattr(assets, "download_file", download_file)
    monkeypatch.setattr(assets, "delete_files", delete_files)
    monkeypatch.setattr(text_drift, "check", text_check)
    monkeypatch.setattr(content_drift, "check", content_check)
    world.tmp = tmp_path
    return world


def _drifting_take(take, tmp_path, **motion) -> list:
    frames, boxes = _drifting(_world(), 30, **motion)
    take.video = Path(_encode(tmp_path, frames)).read_bytes()
    take.start = _png(frames[0])
    return boxes


@FFMPEG
def test_a_drifting_take_asked_still_is_rescued_and_keeps_its_raw_take(take, tmp_path):
    _drifting_take(take, tmp_path)
    take.row.params[frame_qc.MAX_CAMERA_MOTION] = 0.1
    asyncio.run(runners.run_clip(str(take.row.id)))
    row = take.row
    assert row.status == JOB_READY, row.error
    qc = row.params[frame_qc.QC_KEY]
    record = qc[frame_qc.STABILISATION]
    assert record["applied"] and record["residual_motion"] < 0.1 < record["raw_motion"]
    assert frame_qc.camera_motion(qc) < 0.1
    # The un-stabilised take is the take's own file, kept beside it; the stabilised one plays.
    unstabilised = record[frame_qc.UNSTABILISED]
    assert unstabilised in take.files and unstabilised not in take.deleted and unstabilised != str(row.file_id)
    assert unstabilised in frame_qc.frame_file_ids(row.params)
    assert unstabilised in clips.side_files(row.params, videos_only=True)
    # Measured on the part of its start picture it kept: still the start picture.
    assert qc["reference_ssim"] > 0.8
    served = frame_qc.summary(row.params)["stabilisation"]
    assert served["unstabilised_kept"] is True and frame_qc.UNSTABILISED not in served
    # Each frame's map is kept for the render, one per frame of the take that plays, never served.
    played = tmp_path / "played.mp4"
    played.write_bytes(take.files[str(row.file_id)])
    assert len(record[stabilise.MAPS]) == len(_decoded(str(played))) and stabilise.MAPS not in served
    assert stabilise.raw_take(row.params) == (unstabilised, record[stabilise.MAPS], record["kept_share"])
    assert any(note.startswith("Stabilised: the camera drifted") for note in row.params["notes"])


@FFMPEG
def test_a_drifting_take_not_to_be_stabilised_fails_as_before(take, tmp_path):
    _drifting_take(take, tmp_path)
    # Its camera moves about 0.09 of the frame (measured on the background).
    take.row.params.update({frame_qc.MAX_CAMERA_MOTION: 0.05, clips.STABILISE: False})
    asyncio.run(runners.run_clip(str(take.row.id)))
    row = take.row
    assert row.status == JOB_FAILED and row.error.startswith("camera moved: its camera moved ")
    assert frame_qc.STABILISATION not in row.params[frame_qc.QC_KEY]


@FFMPEG
def test_a_take_whose_prompt_asks_a_locked_camera_is_stabilised_and_a_moving_one_is_not(take, tmp_path):
    _drifting_take(take, tmp_path)
    # The prompt read for its camera (the content check's own reading, kept with the shot).
    take.row.params.pop("content_check")
    take.row.params[content_drift.SUBJECTS_KEY] = {"camera": content_drift.CAMERA_LOCKED}
    asyncio.run(runners.run_clip(str(take.row.id)))
    assert take.row.status == JOB_READY and take.row.params[frame_qc.QC_KEY][frame_qc.STABILISATION]["applied"]

    take.row.status, take.row.file_id, take.row.provider_job_id = JOB_PENDING, None, None
    take.row.params = {
        "provider_id": str(uuid.uuid4()),
        "reference_file_id": START,
        content_drift.SUBJECTS_KEY: {"camera": content_drift.CAMERA_MOVING},
    }
    asyncio.run(runners.run_clip(str(take.row.id)))
    assert take.row.status == JOB_READY and frame_qc.STABILISATION not in take.row.params[frame_qc.QC_KEY]


# ── 1b. The camera measured on the background ─────────────────────

#: A subject as large as most of the frame, as textured as the room behind it.
_SUBJECT = (400, 360)


def _crossing(count: int = 36, *, pan: float = 0.0, step: float = 18.0, start: float = 60.0) -> list[Image.Image]:
    """A camera panning *pan* of the frame's width over the take, a subject crossing it *step* px a frame and out.

    *pan* 0 is a locked-off camera. The subject is another textured picture, its colours inverted, so it carries
    as many feature points as the background (it outvotes it on the frames it fills) and shares none of its look.
    """
    world = _world((2400, 1000))
    subject = ImageOps.invert(
        _shoot(_world(seed=17), (300.0, 200.0, 300.0 + _SUBJECT[0] * 1.6, 200.0 + _SUBJECT[1] * 1.6)).crop((0, 0, *_SUBJECT))
    )
    # A level pan: frame 0 shows 1100 x 619 px of the world, each next one moves right by a share of that width.
    span, per_frame = 1100.0, pan * 1100.0 / max(count - 1, 1)
    height = span * SIZE[1] / SIZE[0]
    frames = []
    for index in range(count):
        left = 50.0 + per_frame * index
        frame = _shoot(world, (left, 500.0 - height / 2, left + span, 500.0 + height / 2))
        frame.paste(subject, (round(start + step * index), 0))
        frames.append(frame)
    return frames


@FFMPEG
def test_a_subject_leaving_a_locked_off_frame_is_no_camera_move(tmp_path):
    frames = _crossing()
    raw = _encode(tmp_path, frames)
    # The whole frame changed: the subject that filled most of it is gone.
    decoded = _decoded(raw)
    assert 1.0 - frame_qc.ssim(Image.fromarray(decoded[0]), Image.fromarray(decoded[-1])) > 0.5
    camera, why = asyncio.run(stabilise.measure_camera(raw))
    assert why is None and camera < 0.05
    out = str(tmp_path / "steady.mp4")
    result = asyncio.run(stabilise.stabilise(raw, out))
    # Nothing to lock: the take is not warped, and no "could not be stabilised" is said of it.
    assert not result.applied and result.reason == stabilise.NO_DRIFT and not Path(out).exists()
    assert result.raw_motion > 0.5 and result.camera_motion < 0.05
    record = result.as_params()
    assert record[stabilise.CAMERA_MOTION] < 0.05 and stabilise.recorded_camera(record) < 0.05


@FFMPEG
def test_a_pan_alone_measures_its_share_of_the_frame(tmp_path):
    raw = _encode(tmp_path, _crossing(pan=0.2, start=SIZE[0] + 10.0))
    camera, why = asyncio.run(stabilise.measure_camera(raw))
    assert why is None and 0.17 < camera < 0.23, camera


@FFMPEG
def test_a_locked_off_take_whose_subject_leaves_passes_its_camera_tolerance(take, tmp_path):
    frames = _crossing()
    take.video = Path(_encode(tmp_path, frames)).read_bytes()
    take.start = _png(frames[0])
    take.row.params[frame_qc.MAX_CAMERA_MOTION] = 0.5
    asyncio.run(runners.run_clip(str(take.row.id)))
    row = take.row
    assert row.status == JOB_READY, row.error
    qc = row.params[frame_qc.QC_KEY]
    assert qc[frame_qc.CAMERA_MOTION] < 0.05 and qc[frame_qc.FRAME_CHANGE] > 0.5
    assert frame_qc.camera_motion(qc) < 0.05 and not qc[frame_qc.STABILISATION].get("applied")
    served = frame_qc.summary(row.params)["frame_qc"]
    assert {"motion_ssim", frame_qc.CAMERA_MOTION, frame_qc.FRAME_CHANGE} <= set(served)


@FFMPEG
def test_a_pan_with_a_crossing_subject_fails_on_its_camera_not_on_its_frame(take, tmp_path):
    frames = _crossing(pan=0.6, step=-18.0, start=200.0)
    take.video = Path(_encode(tmp_path, frames)).read_bytes()
    take.start = _png(frames[0])
    take.row.params[frame_qc.MAX_CAMERA_MOTION] = 0.5
    asyncio.run(runners.run_clip(str(take.row.id)))
    row = take.row
    qc = row.params[frame_qc.QC_KEY]
    camera = qc[frame_qc.CAMERA_MOTION]
    assert 0.5 < camera < 0.7, camera
    assert row.status == JOB_FAILED and row.error.startswith(f"camera moved: its camera moved {camera:.2f} of the frame")
    assert f"the whole frame changed {qc[frame_qc.FRAME_CHANGE]:.2f}" in row.error


def test_the_camera_gate_reads_the_background_first_and_the_frame_when_the_camera_was_not_followed():
    assert frame_qc.camera_motion({"motion_ssim": 0.09, frame_qc.CAMERA_MOTION: 0.012}) == 0.012
    assert frame_qc.camera_motion({"motion_ssim": 0.09}) == 0.91
    assert frame_qc.motion_reason(0.91, 0.5).startswith("camera moved: its last frame changed 0.91 from its first")
    reason = frame_qc.motion_reason(0.62, 0.5, change=0.94)
    assert reason.startswith("camera moved: its camera moved 0.62 of the frame") and "whole frame changed 0.94" in reason
    assert stabilise.recorded_camera({"applied": True, "camera_motion": 0.2, "residual_camera_motion": 0.01}) == 0.01
    assert stabilise.recorded_camera({"applied": False, "camera_motion": 0.2}) == 0.2
    assert stabilise.recorded_camera({"applied": False, "reason": "lost"}) is None
    assert stabilise.camera_share(np.array([[1.0, 0.0, 128.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]), (640, 360)) == 0.2


def test_the_quality_check_holds_a_locked_off_shot_to_its_camera():
    from app.services.video_studio import quality

    crossing = quality.ShotFacts(name="s", status=JOB_READY, camera="locked", motion_ssim=0.09, camera_motion=0.01)
    assert not quality.camera_moved(crossing)
    assert quality.camera_moved(quality.ShotFacts(name="s", status=JOB_READY, camera="locked", motion_ssim=0.09))
    held = quality.ShotFacts(name="s", status=JOB_READY, motion_ssim=0.9, camera_motion=0.6, max_camera_motion=0.5)
    assert quality.camera_moved(held)


# ── 2. Tracked screen inserts ─────────────────────────────────────────────

#: The monitor drawn on the wall, in the world's pixels: its screen, and the bezel around it.
_SCREEN = ((620.0, 330.0), (980.0, 340.0), (975.0, 560.0), (625.0, 550.0))


def _room(seed: int = 5) -> Image.Image:
    room = _world(seed=seed)
    draw = ImageDraw.Draw(room)
    bezel = tracking.grown(_SCREEN, 0.12)
    draw.polygon(bezel, fill=(28, 28, 32))
    rng = np.random.default_rng(seed)
    for x, y in rng.uniform([600, 300], [1000, 590], (60, 2)):
        draw.ellipse([x, y, x + 5, y + 5], fill=tuple(int(v) for v in rng.integers(90, 220, 3)))
    draw.polygon(_SCREEN, fill=(210, 230, 240))
    return room


def _quad_in(box: tuple[float, ...]) -> np.ndarray:
    """The screen's corners on the frame showing *box*, 0-1."""
    x0, y0, x1, y1 = box
    return (np.asarray(_SCREEN) - [x0, y0]) / [x1 - x0, y1 - y0]


@FFMPEG
def test_a_tracked_quad_follows_the_surface_a_moving_camera_carries(tmp_path):
    room = _room()
    boxes = [_box(i, pan=1.5, zoom=0.002) for i in range(30)]
    footage = _encode(tmp_path, [_shoot(room, box) for box in boxes])
    first = 6
    followed = asyncio.run(
        insert_tracking.track(footage, _quad_in(boxes[first]).tolist(), first=first, last=27, what="Scene demo: its screen insert 0")
    )
    assert (followed.first, followed.last) == (first, 27)
    for frame in (first, 15, 27):
        error = np.abs(np.asarray(followed.quad_at(frame)) - _quad_in(boxes[frame])).max()
        assert error < 0.004, (frame, error)
    x0, y0, x1, y1 = followed.box
    assert all(x0 <= x <= x1 and y0 <= y <= y1 for quad in followed.quads for x, y in quad)
    # A frame past the track holds its last quad; one before it, its first.
    assert followed.quad_at(40) == followed.quads[-1] and followed.quad_at(0) == followed.quads[0]


@FFMPEG
def test_a_held_insert_is_followed_back_from_the_frame_its_quad_is_given_on(tmp_path):
    """A keyed insert coming in after its scene's start is held on its surface from that start - its quad, given
    on the frame it comes in on, followed back over the frames before it as well as forward."""
    room = _room()
    boxes = [_box(i, pan=1.5, zoom=0.002) for i in range(30)]
    footage = _encode(tmp_path, [_shoot(room, box) for box in boxes])
    first = 9
    [followed] = asyncio.run(
        insert_tracking.track_all(
            footage, [insert_tracking.Asked(_quad_in(boxes[first]).tolist(), first, 27, "Scene demo: its screen insert 0", since=0)]
        )
    )
    assert (followed.first, followed.last) == (0, 27)
    for frame in (0, 4, first, 20, 27):
        error = np.abs(np.asarray(followed.quad_at(frame)) - _quad_in(boxes[frame])).max()
        assert error < 0.004, (frame, error)
    # Lost before it comes in: held on the quad of the frame after back to its since, said, never a failure.
    frames = [_shoot(_world(seed=41), box) for box in boxes[:4]] + [_shoot(room, box) for box in boxes[4:]]
    [held] = asyncio.run(
        insert_tracking.track_all(
            _encode(tmp_path, frames, name="lost.mp4"),
            [insert_tracking.Asked(_quad_in(boxes[first]).tolist(), first, 27, "Scene demo: its screen insert 0", since=0)],
        )
    )
    assert (held.first, held.last, held.held_before) == (0, 27, 4)
    assert held.said().endswith(", 4 held before it comes in")
    assert np.abs(np.asarray(held.quad_at(0)) - np.asarray(held.quad_at(4))).max() < 0.004
    for frame in (5, first, 27):
        assert np.abs(np.asarray(held.quad_at(frame)) - _quad_in(boxes[frame])).max() < 0.004, frame
    # Lost once it is in: still a failure, with the remedy of cutting it short.
    gone = [_shoot(room, box) for box in boxes[:20]] + [_shoot(_world(seed=41), box) for box in boxes[20:]]
    with pytest.raises(insert_tracking.InsertTrackLostError) as lost:
        asyncio.run(
            insert_tracking.track_all(
                _encode(tmp_path, gone, name="gone.mp4"),
                [insert_tracking.Asked(_quad_in(boxes[first]).tolist(), first, 27, "Scene demo: its screen insert 0", since=0)],
            )
        )
    assert lost.value.frame == 20 and "cut the insert short (duration)" in str(lost.value)


def test_the_frames_followed_back_are_kept_in_a_file_their_slots_reused():
    """The frames before an insert comes in are kept on disk, never as bytes in memory, a frame given back
    freeing its slot for the next one."""
    behind = insert_tracking._Behind(6)
    try:
        behind.put(10, b"aaaaaa")
        behind.put(11, b"bbbbbb")
        assert (10 in behind, len(behind), behind.get(11)) == (True, 2, b"bbbbbb")
        behind.pop(10)
        behind.put(12, b"cccccc")
        assert sorted(behind) == [11, 12] and behind.get(12) == b"cccccc" and behind.get(11) == b"bbbbbb"
        assert behind._next == 2  # the slot frame 10 freed held frame 12
    finally:
        behind.close()


@FFMPEG
def test_a_surface_lost_fails_loudly_with_its_frame(tmp_path):
    room = _room()
    boxes = [_box(i, pan=1.5, zoom=0.002) for i in range(24)]
    frames = [_shoot(room, box) for box in boxes[:12]] + [_shoot(_world(seed=41), box) for box in boxes[12:]]
    footage = _encode(tmp_path, frames)
    with pytest.raises(insert_tracking.InsertTrackLostError) as lost:
        asyncio.run(insert_tracking.track(footage, _quad_in(boxes[0]).tolist(), first=0, last=23, what="Scene demo: its screen insert 0"))
    assert lost.value.frame == 12 and lost.value.seconds == pytest.approx(0.5)
    assert str(lost.value).startswith("Scene demo: its screen insert 0: surface lost at t=0.50 s of its scene (footage frame 12, 0.50 s)")
    # The frame it was lost on goes with the failure: the footage there, the last quad found drawn on it.
    shown = Image.open(io.BytesIO(lost.value.frame_png))
    assert shown.format == "JPEG" and shown.size == SIZE


def test_a_tracked_insert_is_drawn_into_its_quad_on_the_footage_frame_shown():
    quads = tuple(
        tuple((x + 0.01 * i, y + 0.005 * i) for x, y in ((0.3, 0.3), (0.6, 0.3), (0.6, 0.6), (0.3, 0.6))) for i in range(11)
    )
    xs, ys = [x for q in quads for x, _y in q], [y for q in quads for _x, y in q]
    followed = insert_tracking.InsertTrack(5, quads, (min(xs), min(ys), max(xs), max(ys)))
    insert = screen_insert.InsertSource("p.png", "image", quads[0], fit="stretch", track=followed)
    picture = Image.new("RGB", (200, 120), (250, 40, 40))

    def centre(footage_frame: int) -> tuple[float, float, tuple[int, int]]:
        drawn = screen_insert.decode(screen_insert.draw(insert, picture, (1000, 1000), footage_frame)[0])
        alpha = np.asarray(drawn)[..., 3]
        ys_, xs_ = np.nonzero(alpha > 200)
        return float(xs_.mean()), float(ys_.mean()), drawn.size

    x_a, y_a, size_a = centre(5)
    x_b, y_b, size_b = centre(15)
    # Every frame covers the box the page places it on - the one all its quads lie in; the picture moved with its quad.
    x0, y0, x1, y1 = screen_insert.bbox([tuple(p) for p in followed.box_quad()], 1000, 1000)
    assert size_a == size_b == (x1 - x0, y1 - y0)
    assert x_b - x_a == pytest.approx(100, abs=2) and y_b - y_a == pytest.approx(50, abs=2)
    # The page names the footage frame it shows and places the frame on that box.
    from app.services.video_studio.compositor_page import PAGE_JS

    assert "const f = tr ? clamp(Math.round((at || 0) * tr.fps), tr.from, tr.to)" in PAGE_JS
    assert "(f !== null ? `&f=${f}` : '')" in PAGE_JS
    assert "insertBox(ins.box || ins.quad, W, H)" in PAGE_JS


@FFMPEG
def test_the_render_follows_a_tracked_insert_and_tells_the_page_its_frames(tmp_path):
    from app.services.video_studio.spec import parse_video_spec

    room = _room()
    boxes = [_box(i, pan=1.5, zoom=0.002) for i in range(30)]
    footage = _encode(tmp_path, [_shoot(room, box) for box in boxes])
    # Given on the frame the insert comes in on: the scene plays its shot from 0.25 s, the insert starts at 0.25 s,
    # so it comes in on footage frame 12 (0.5 s) and goes out on frame 24 (1.0 s).
    quad = [[round(x, 4), round(y, 4)] for x, y in _quad_in(boxes[12])]
    spec = parse_video_spec(
        {
            "title": "T",
            "scenes": [
                {
                    "id": "demo",
                    "type": "clip",
                    "clip_id": "00000000-0000-0000-0000-00000000c0aa",
                    "overlays": [
                        {"type": "screen_insert", "source": {"file_id": LOGO_FILE}, "quad": quad, "track": True, "start": 0.25, "duration": 0.5}
                    ],
                }
            ],
        }
    )
    key = timeline.insert_key("demo", 0)
    payload = {"src": key, "quad": quad, "t0": 0.25, "t1": 0.75}
    document = {"scenes": [{"id": "demo", "type": "clip", "src": "clip/demo", "time_map": [[0.0, 0.25], [2.0, 2.25]], "inserts": [payload]}]}
    media = render._Media()
    media.assets.sources["clip/demo"] = compositor.FrameSource(footage, float(FPS))
    media.assets.inserts[key] = screen_insert.InsertSource("p.png", "image", tuple(map(tuple, quad)))
    heard: list[str] = []

    async def progress(_value, stage):
        heard.append(stage)

    followed: list[int] = []
    asyncio.run(render._track_inserts(spec, document, media, progress, on_frame=lambda: followed.append(1)))
    assert payload["track"] == {"from": 12, "to": 24, "fps": float(FPS)}
    assert len(payload["box"]) == 4 and media.assets.inserts[key].track.first == 12
    assert heard and heard[0].startswith("Scene demo: its screen insert 0: following its surface")
    # Each footage frame decoded renews the render's lease: frames 0 to 25, the one past the insert's last.
    assert len(followed) == 26
    # A shot that cuts away from the monitor while the insert shows: the render fails, naming the footage frame.
    payload.pop("track")
    cut = [_shoot(room, box) for box in boxes[:16]] + [_shoot(_world(seed=41), box) for box in boxes[16:]]
    media.assets.sources["clip/demo"] = compositor.FrameSource(_encode(tmp_path, cut, "cut.mp4"), float(FPS))
    with pytest.raises(insert_tracking.InsertTrackLostError) as lost:
        asyncio.run(render._track_inserts(spec, document, media))
    # Said in the scene's time (its footage plays from 0.25 s): footage frame 16 (0.67 s) is 0.42 s into the scene.
    assert str(lost.value).startswith("Scene demo: its screen insert 0: surface lost at t=0.42 s of its scene (footage frame 16, 0.67 s)")
    assert lost.value.scene_id == "demo" and lost.value.scene_seconds == pytest.approx(0.4167, abs=1e-3)


@FFMPEG
def test_the_inserts_on_one_footage_are_followed_in_one_decode(tmp_path, monkeypatch):
    from app.services.video_studio.spec import parse_video_spec

    room = _room()
    boxes = [_box(i, pan=1.5, zoom=0.002) for i in range(30)]
    footage = _encode(tmp_path, [_shoot(room, box) for box in boxes])
    # The scene plays its shot from 0.25 s: one insert comes in on footage frame 12 (0.5 s), the next on frame 19 (0.8 s).
    quads = [[[round(x, 4), round(y, 4)] for x, y in _quad_in(boxes[frame])] for frame in (12, 19)]
    overlays = [
        {"type": "screen_insert", "source": {"file_id": LOGO_FILE}, "quad": quads[0], "track": True, "start": 0.25, "duration": 0.3},
        {"type": "screen_insert", "source": {"file_id": LOGO_FILE}, "quad": quads[1], "track": True, "start": 0.55, "duration": 0.3},
    ]
    spec = parse_video_spec(
        {"title": "T", "scenes": [{"id": "demo", "type": "clip", "clip_id": "00000000-0000-0000-0000-00000000c0aa", "overlays": overlays}]}
    )
    keys = [timeline.insert_key("demo", n) for n in range(2)]
    payloads = [
        {"src": keys[0], "quad": quads[0], "t0": 0.25, "t1": 0.55},
        {"src": keys[1], "quad": quads[1], "t0": 0.55, "t1": 0.85},
    ]
    document = {"scenes": [{"id": "demo", "type": "clip", "src": "clip/demo", "time_map": [[0.0, 0.25], [2.0, 2.25]], "inserts": payloads}]}
    media = render._Media()
    media.assets.sources["clip/demo"] = compositor.FrameSource(footage, float(FPS))
    for key, quad in zip(keys, quads, strict=True):
        media.assets.inserts[key] = screen_insert.InsertSource("p.png", "image", tuple(map(tuple, quad)))
    decodes: list[str] = []
    decode = encoder.iter_rgb_frames

    def counted(path, **kwargs):
        decodes.append(path)
        return decode(path, **kwargs)

    monkeypatch.setattr(encoder, "iter_rgb_frames", counted)
    asyncio.run(render._track_inserts(spec, document, media))
    assert decodes == [footage]
    assert [payload["track"] for payload in payloads] == [
        {"from": 12, "to": 19, "fps": float(FPS)},
        {"from": 19, "to": 26, "fps": float(FPS)},
    ]
    for key, frames in zip(keys, ((12, 15, 19), (19, 22, 26)), strict=True):
        followed = media.assets.inserts[key].track
        for frame in frames:
            assert np.abs(np.asarray(followed.quad_at(frame)) - _quad_in(boxes[frame])).max() < 0.004, (key, frame)


def test_a_tracked_insert_over_a_picture_stays_where_it_is_given():
    from app.services.video_studio.spec import parse_video_spec

    spec = parse_video_spec(
        {
            "title": "T",
            "scenes": [
                {
                    "id": "s",
                    "type": "statement",
                    "lines": ["Hi"],
                    "backdrop": {"file_id": LOGO_FILE},
                    "overlays": [
                        {
                            "type": "screen_insert",
                            "source": {"file_id": LOGO_FILE},
                            "quad": [[0.2, 0.2], [0.5, 0.2], [0.5, 0.5], [0.2, 0.5]],
                            "track": True,
                        }
                    ],
                }
            ],
        }
    )
    key = timeline.insert_key("s", 0)
    payload = {"src": key, "quad": [], "t0": 0.0, "t1": 1.0}
    document = {"scenes": [{"id": "s", "type": "statement", "backdrop": {"src": "backdrop/s", "kind": "image"}, "inserts": [payload]}]}
    media = render._Media()
    media.assets.inserts[key] = screen_insert.InsertSource("p.png", "image", ((0.2, 0.2), (0.5, 0.2), (0.5, 0.5), (0.2, 0.5)))
    asyncio.run(render._track_inserts(spec, document, media))
    assert "track" not in payload and media.assets.inserts[key].track is None
    assert media.notes == ["Scene s: its screen insert 0 stays where its quad is: a picture behind it has no footage to follow."]


# ── 2b. Inserts on a stabilised shot, and walls of panes ──────────

#: A second monitor on the wall, lower right of the first.
_SCREEN_B = ((1060.0, 600.0), (1240.0, 606.0), (1236.0, 730.0), (1062.0, 724.0))


def _wall_room(seed: int = 5) -> Image.Image:
    """The room of :func:`_room` with a second monitor: its bezel dark and flecked, its screen pale."""
    room = _room(seed)
    draw = ImageDraw.Draw(room)
    draw.polygon(tracking.grown(_SCREEN_B, 0.14), fill=(30, 28, 34))
    rng = np.random.default_rng(seed + 1)
    for x, y in rng.uniform([1035, 580], [1265, 750], (50, 2)):
        draw.ellipse([x, y, x + 5, y + 5], fill=tuple(int(v) for v in rng.integers(90, 220, 3)))
    draw.polygon(_SCREEN_B, fill=(220, 225, 245))
    return room


def _steadied(tmp_path) -> tuple[str, str, dict, list]:
    """A drifting take of the room with two monitors, stabilised: the raw take, the stabilised one, its record, the boxes."""
    frames, boxes = _drifting(_wall_room(), 30)
    raw = _encode(tmp_path, frames, "raw.mp4")
    steady = str(tmp_path / "steady.mp4")
    result = asyncio.run(stabilise.stabilise(raw, steady))
    assert result.applied, result.reason
    return raw, steady, result.as_params(), boxes


def _on_stabilised(screen, box: tuple[float, ...], crop) -> np.ndarray:
    """*screen*'s corners on the stabilised take (0-1): where frame 0 shows them, in the window the crop kept."""
    x0, y0, x1, y1 = box
    on_first = (np.asarray(screen) - [x0, y0]) / [x1 - x0, y1 - y0]
    cx0, cy0, cx1, cy1 = crop
    return (on_first - [cx0, cy0]) / [cx1 - cx0, cy1 - cy0]


@FFMPEG
def test_each_frame_s_map_takes_a_point_of_the_stabilised_take_to_the_raw_take(tmp_path):
    raw, _steady, record, boxes = _steadied(tmp_path)
    maps = record[stabilise.MAPS]
    # One map per frame of the take, as decoded.
    assert len(maps) == len(_decoded(raw)) and all(len(coefficients) == 8 for coefficients in maps)
    for frame in (0, 14, 29):
        # The monitor's corners on the stabilised take land where the raw frame shows them.
        landed = tracking.project(stabilise.map_matrix(maps[frame]), _on_stabilised(_SCREEN, boxes[0], record["crop"]))
        assert np.abs(landed - _quad_in(boxes[frame])).max() < 0.004, frame


@FFMPEG
def test_two_screen_inserts_on_a_stabilised_shot_stay_inside_the_frame_on_every_frame(tmp_path):
    """Acceptance: followed through the raw take, placed back where the stabilised take draws each screen."""
    from app.services.video_studio.spec import parse_video_spec

    raw, steady, record, boxes = _steadied(tmp_path)
    quads = [[[round(x, 4), round(y, 4)] for x, y in _on_stabilised(s, boxes[0], record["crop"])] for s in (_SCREEN, _SCREEN_B)]
    overlays = [
        {"type": "screen_insert", "source": {"file_id": LOGO_FILE}, "quad": quad, "track": True, "start": 0.0, "duration": 1.0}
        for quad in quads
    ]
    spec = parse_video_spec(
        {"title": "T", "scenes": [{"id": "demo", "type": "clip", "clip_id": "00000000-0000-0000-0000-00000000c0aa", "overlays": overlays}]}
    )
    keys = [timeline.insert_key("demo", n) for n in range(2)]
    payloads = [{"src": key, "quad": quad, "t0": 0.0, "t1": 1.0} for key, quad in zip(keys, quads, strict=True)]
    document = {"scenes": [{"id": "demo", "type": "clip", "src": "clip/demo", "time_map": [[0.0, 0.0], [2.0, 2.0]], "inserts": payloads}]}
    media = render._Media()
    media.assets.sources["clip/demo"] = compositor.FrameSource(steady, float(FPS))
    media.steadied[steady] = (raw, record[stabilise.MAPS], record["kept_share"])
    for key, quad in zip(keys, quads, strict=True):
        media.assets.inserts[key] = screen_insert.InsertSource("p.png", "image", tuple(map(tuple, quad)))
    decodes: list[str] = []
    decode = encoder.iter_rgb_frames

    def counted(path, **kwargs):
        decodes.append(path)
        return decode(path, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(encoder, "iter_rgb_frames", counted)
        asyncio.run(render._track_inserts(spec, document, media))
    # Followed through the raw take, once for both.
    assert decodes == [raw]
    for key, quad in zip(keys, quads, strict=True):
        followed = media.assets.inserts[key].track
        assert (followed.first, followed.last) == (0, 24)
        for frame, tracked in enumerate(followed.quads):
            assert all(0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 for x, y in tracked), (key, frame, tracked)
            # On the stabilised take the screen holds still: so does its insert, within the warp's own residue.
            assert np.abs(np.asarray(tracked) - quad).max() < 0.004, (key, frame)


@FFMPEG
def test_an_insert_the_stabilised_crop_cuts_fails_as_screen_cropped(tmp_path):
    raw, _steady, record, _boxes = _steadied(tmp_path)
    # A quad past the left edge of the stabilised frame: the crop cut what it points at, the raw take shows it.
    asked = insert_tracking.Asked([[-0.03, 0.3], [0.25, 0.3], [0.25, 0.55], [-0.03, 0.55]], 12, 20, "Scene demo: its screen insert 0")
    with pytest.raises(insert_tracking.ScreenCroppedError) as cropped:
        asyncio.run(
            insert_tracking.track_stabilised(raw, record[stabilise.MAPS], [asked], fps=FPS, kept_share=record["kept_share"])
        )
    assert cropped.value.frame == 12 and isinstance(cropped.value, insert_tracking.InsertTrackLostError)
    message = str(cropped.value)
    assert message.startswith("Scene demo: its screen insert 0: screen cropped at frame 12 of its footage (0.50 s): its top left")
    assert "the stabilised shot's crop cuts into it" in message and "stabilise: false" in message


@FFMPEG
def test_the_crop_never_cuts_into_the_screen_a_shot_shows(tmp_path):
    frames, _boxes = _drifting(_world())
    raw = _encode(tmp_path, frames)
    # The camera drifts right: frame 0's left edge leaves the frame; a screen there cannot be kept by any crop.
    edge = [(0.01, 0.1), (0.2, 0.1), (0.2, 0.4), (0.01, 0.4)]
    out = str(tmp_path / "steady.mp4")
    kept = asyncio.run(stabilise.stabilise(raw, out, keep=[edge]))
    assert not kept.applied and not Path(out).exists()
    assert "locking it would crop into its screen" in kept.reason
    # A screen in the middle is kept whole: the take is stabilised, the window around it.
    middle = [(0.4, 0.35), (0.6, 0.35), (0.6, 0.6), (0.4, 0.6)]
    steadied = asyncio.run(stabilise.stabilise(raw, out, keep=[middle]))
    x0, y0, x1, y1 = steadied.crop
    assert steadied.applied and all(x0 <= x <= x1 and y0 <= y <= y1 for x, y in middle)
    # The shot's own screen, as its params keep it (start picture pixels), on its first frame.
    stored = {"quad": [[64, 36], [128, 36], [128, 90], [64, 90]], "start_size": [640, 360]}
    assert screens.start_quad(stored) == [(0.1, 0.1), (0.2, 0.1), (0.2, 0.25), (0.1, 0.25)]
    assert screens.start_quad(None) is None


@FFMPEG
def test_a_wall_is_followed_once_and_its_panes_move_with_it(tmp_path):
    from app.services.video_studio.spec import pane_quads, parse_video_spec

    room = _room()
    boxes = [_box(i, pan=1.5, zoom=0.002) for i in range(30)]
    footage = _encode(tmp_path, [_shoot(room, box) for box in boxes])
    quad = [[round(x, 4), round(y, 4)] for x, y in _quad_in(boxes[12])]
    wall = {
        "type": "screen_insert",
        "panes": 2,
        "sources": [{"file_id": LOGO_FILE}, {"file_id": LOGO_FILE}],
        "quad": quad,
        "track": True,
        "start": 0.25,
        "duration": 0.5,
    }
    spec = parse_video_spec(
        {"title": "T", "scenes": [{"id": "demo", "type": "clip", "clip_id": "00000000-0000-0000-0000-00000000c0aa", "overlays": [wall]}]}
    )
    parts = timeline.insert_sources(spec)
    payloads = [{"src": key, "quad": [list(p) for p in part.quad], "t0": 0.25, "t1": 0.75} for key, part in parts.items()]
    document = {"scenes": [{"id": "demo", "type": "clip", "src": "clip/demo", "time_map": [[0.0, 0.25], [2.0, 2.25]], "inserts": payloads}]}
    media = render._Media()
    media.assets.sources["clip/demo"] = compositor.FrameSource(footage, float(FPS))
    for key, part in parts.items():
        media.assets.inserts[key] = screen_insert.InsertSource("p.png", "image", part.quad)
    asked: list[int] = []
    track_all = insert_tracking.track_all

    async def counted(path, wanted, **kwargs):
        asked.append(len(wanted))
        return await track_all(path, wanted, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(insert_tracking, "track_all", counted)
        asyncio.run(render._track_inserts(spec, document, media))
    # One surface followed for the two panes.
    assert asked == [1]
    for pane, key in enumerate(parts):
        followed = media.assets.inserts[key].track
        assert payloads[pane]["track"] == {"from": 12, "to": 24, "fps": float(FPS)}
        for frame in (12, 18, 24):
            truth = pane_quads([tuple(p) for p in _quad_in(boxes[frame])], 1, 2, 0.015)[pane]
            assert np.abs(np.asarray(followed.quad_at(frame)) - truth).max() < 0.004, (pane, frame)


def test_track_is_a_field_of_a_screen_insert_only():
    from app.services.video_studio.spec import Overlay

    insert = Overlay.model_validate(
        {"type": "screen_insert", "source": {"file_id": LOGO_FILE}, "quad": [[0.2, 0.2], [0.5, 0.2], [0.5, 0.5], [0.2, 0.5]], "track": True}
    )
    assert insert.track is True
    assert Overlay.model_validate({"type": "callout", "text": "Hi", "track": True}).track is None


# ── 6. Logos drawn back ───────────────────────────────────────────────────


def _logo() -> Image.Image:
    """A synthetic mark on a transparent ground: a ring, a bar and a square of three colours."""
    logo = Image.new("RGBA", (160, 80), (0, 0, 0, 0))
    draw = ImageDraw.Draw(logo)
    draw.ellipse([4, 4, 72, 72], outline=(255, 210, 0, 255), width=10)
    draw.rectangle([84, 10, 152, 30], fill=(0, 200, 255, 255))
    draw.rectangle([84, 44, 116, 76], fill=(255, 60, 120, 255))
    return logo


#: Where the logo is on the wall, in the world's pixels.
_LOGO_AT = (700, 420, 860, 500)


def _wall(logo: Image.Image | None) -> Image.Image:
    wall = _world(seed=8)
    if logo is not None:
        wall.paste(logo, _LOGO_AT[:2], logo)
    return wall


def _garbled(logo: Image.Image) -> Image.Image:
    """What a model makes of a logo it redraws: blurred, its colours off."""
    rgb = np.asarray(logo, dtype=np.float32)
    shifted = rgb.copy()
    shifted[..., :3] = rgb[..., [2, 0, 1]] * 0.8
    return Image.fromarray(shifted.astype(np.uint8), "RGBA").filter(ImageFilter.GaussianBlur(4))


def _logo_frames(count: int, *, cut_at: int | None = None) -> tuple[list[Image.Image], list[Image.Image], list]:
    """The model's frames (its logo garbled), the frames with the real logo, and what each shows."""
    boxes = [_box(i, pan=1.2, zoom=0.002) for i in range(count)]
    garbled, clean = _wall(_garbled(_logo())), _wall(_logo())
    made = [
        _shoot(garbled if cut_at is None or i < cut_at else _world(seed=50), box) for i, box in enumerate(boxes)
    ]
    return made, [_shoot(clean, box) for box in boxes], boxes


def _logo_box(box: tuple[float, ...]) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = box
    sx, sy = SIZE[0] / (x1 - x0), SIZE[1] / (y1 - y0)
    lx0, ly0, lx1, ly1 = _LOGO_AT
    return (round((lx0 - x0) * sx) + 2, round((ly0 - y0) * sy) + 2, round((lx1 - x0) * sx) - 2, round((ly1 - y0) * sy) - 2)


def test_a_logo_is_found_where_the_start_frame_shows_it_and_nowhere_else():
    frame = _shoot(_wall(_logo()), _box(0, pan=0, zoom=0))
    found = logos.locate(_logo(), frame)
    assert found is not None and found[1] > 0.95
    expected = _logo_box(_box(0, pan=0, zoom=0))
    (x0, y0), _tr, (x1, y1), _bl = found[0]
    measured = (x0 * SIZE[0], y0 * SIZE[1], x1 * SIZE[0], y1 * SIZE[1])
    # The box the test reads is 2 px inside the logo's own edges.
    assert np.abs(np.array(measured) - np.add(expected, [-2, -2, 2, 2])).max() < 3.0
    # Frames of the same textures without it, and its garbled redraw, are no logo: never drawn in the wrong place.
    for seed in (8, 2, 4, 13):
        assert logos.locate(_logo(), _shoot(_world(seed=seed), _box(0, pan=0, zoom=0))) is None, seed
    assert logos.locate(_logo(), _shoot(_wall(_garbled(_logo())), _box(0, pan=0, zoom=0))) is None


def test_a_logo_of_one_flat_colour_is_found_by_its_shape():
    mark = Image.new("RGBA", (160, 60), (0, 0, 0, 0))
    draw = ImageDraw.Draw(mark)
    for x0, y0, x1, y1 in ((0, 0, 30, 60), (40, 0, 70, 20), (40, 40, 160, 60), (90, 0, 120, 30)):
        draw.rectangle([x0, y0, x1, y1], fill=(255, 255, 255, 255))
    wall = Image.new("RGB", (1600, 1000), (30, 40, 60))
    wall.paste(mark, _LOGO_AT[:2], mark)
    found = logos.locate(mark, _shoot(wall, _box(0, pan=0, zoom=0)))
    assert found is not None and found[1] > logos.MIN_SHAPE_MATCH
    (x0, y0), _tr, _br, _bl = found[0]
    expected = _logo_box(_box(0, pan=0, zoom=0))
    assert abs(x0 * SIZE[0] - (expected[0] - 2)) < 3.0 and abs(y0 * SIZE[1] - (expected[1] - 2)) < 3.0


@FFMPEG
def test_a_garbled_logo_is_drawn_back_with_its_own_pixels_through_a_drifting_shot(tmp_path):
    made, clean, boxes = _logo_frames(24)
    video = _encode(tmp_path, made)
    out = str(tmp_path / "logo.mp4")
    start = clean[0]
    done = asyncio.run(logos.composite(video, out, [logos.Logo(_logo(), "the brand logo")], start))
    assert done.drawn == ["the brand logo"] and done.notes[0].startswith("The brand logo drawn with its own pixels")
    drawn, raw = _decoded(out), _decoded(video)
    for index in (0, 12, 23):
        x0, y0, x1, y1 = _logo_box(boxes[index])
        truth = np.asarray(clean[index], dtype=np.float32)[y0:y1, x0:x1]
        assert np.abs(drawn[index][y0:y1, x0:x1] - truth).mean() < 18, index
        assert np.abs(raw[index][y0:y1, x0:x1] - truth).mean() > 30, index


@FFMPEG
def test_a_logo_the_shot_cuts_away_from_is_lost(tmp_path):
    made, clean, _boxes = _logo_frames(20, cut_at=9)
    video = _encode(tmp_path, made)
    with pytest.raises(logos.LogoLostError, match=r"^logo lost: the brand logo at t=0\.38 s \(frame 9\) of the shot: only \d+ points") as lost:
        asyncio.run(logos.composite(video, str(tmp_path / "logo.mp4"), [logos.Logo(_logo(), "the brand logo")], clean[0]))
    # The frame it was lost in goes with the failure.
    assert lost.value.frame == 9 and Image.open(io.BytesIO(lost.value.frame_jpeg)).size == SIZE


def _covered(made: list[Image.Image], boxes: list, frames: range) -> list[Image.Image]:
    """*made* with a flat hand over the logo and all around it in *frames*: nothing there for a track to hold on to."""
    covered = list(made)
    for index in frames:
        x0, y0, x1, y1 = _logo_box(boxes[index])
        width, height = x1 - x0, y1 - y0
        picture = covered[index].copy()
        ImageDraw.Draw(picture).rectangle([x0 - width, y0 - height, x1 + width, y1 + height], fill=(196, 150, 120))
        covered[index] = picture
    return covered


def _logo_quad(box: tuple[float, ...]) -> np.ndarray:
    """The logo's corners on the frame showing *box*, 0-1."""
    x0, y0, x1, y1 = box
    lx0, ly0, lx1, ly1 = _LOGO_AT
    corners = np.array([[lx0, ly0], [lx1, ly0], [lx1, ly1], [lx0, ly1]], dtype=float)
    return (corners - [x0, y0]) / [x1 - x0, y1 - y0]


@FFMPEG
def test_a_logo_covered_even_for_a_moment_is_lost_never_bridged(tmp_path):
    made, clean, boxes = _logo_frames(24)
    brand = logos.Logo(_logo(), "the brand logo")
    quad = _logo_quad(boxes[0])
    # Followed, its place is the surface's in every frame.
    video = _encode(tmp_path, made, "clear.mp4")
    [track] = asyncio.run(logos._track(video, clean[0], [quad], logos._working_size(SIZE, [quad]), FPS, [brand]))
    assert len(track) >= 24
    for index in (5, 11, 12, 20):
        error = (np.abs(track[index].quad - _logo_quad(boxes[index])) * SIZE).max()
        assert error < 2.0, (index, error)
    # A hand in front of it for 4 frames (0.17 s): the take fails at the first, with that frame - a logo is never
    # drawn over the hand where the take does not show it.
    video = _encode(tmp_path, _covered(made, boxes, range(10, 14)), "brief.mp4")
    with pytest.raises(logos.LogoLostError, match=r"^logo lost: the brand logo at t=0\.42 s \(frame 10\) of the shot") as lost:
        asyncio.run(logos.composite(video, str(tmp_path / "brief-logo.mp4"), [brand], clean[0]))
    assert lost.value.frame == 10 and lost.value.frame_jpeg.startswith(b"\xff\xd8")


def test_a_logo_not_in_the_start_frame_stays_named_in_the_prompt(tmp_path):
    empty = _shoot(_world(seed=8), _box(0, pan=0, zoom=0))

    async def probe(_path):
        return SimpleNamespace(width=SIZE[0], height=SIZE[1], fps=FPS)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(encoder, "probe", probe)
        done = asyncio.run(logos.composite("v.mp4", str(tmp_path / "o.mp4"), [logos.Logo(_logo(), "the brand logo")], empty))
    assert done.drawn == [] and done.notes[0].startswith("The brand logo was not found in the start frame")


def test_a_logo_on_a_model_without_references_is_composited():
    class _None:
        def max_reference_images(self, _model):
            return 0

        def reference_images_with_start_frame(self, _model):
            return False

    class _Three:
        def max_reference_images(self, _model):
            return 3

        def reference_images_with_start_frame(self, _model):
            return True

    assert clips.ingredient_path(_None(), "m", 1, start_frame=True, logos=True) == clips.PATH_COMPOSITED
    assert clips.ingredient_path(_None(), "m", 1, start_frame=True, logos=False) == clips.PATH_PROMPT
    assert clips.ingredient_path(_None(), "m", 1, start_frame=False, logos=True) == clips.PATH_PROMPT
    assert clips.ingredient_path(_Three(), "m", 1, start_frame=True, logos=True) == clips.PATH_NATIVE
    params = {
        clips.INGREDIENTS: [{"file_id": LOGO_FILE, "role": "logo", "label": "the brand logo"}, {"file_id": START, "role": "product"}],
        clips.INGREDIENTS_PATH: clips.PATH_COMPOSITED,
    }
    assert [item.name for item in clips.composited_logos(params)] == ["the brand logo"]
    assert clips.with_ingredients("A booth.", params).endswith("Keep the product exactly as in the reference image.")
    assert clips.composited_logos({**params, clips.INGREDIENTS_PATH: clips.PATH_PROMPT}) == []


def test_a_logo_may_say_where_it_is_and_nothing_else_may():
    square = [[0.4, 0.4], [0.6, 0.4], [0.6, 0.6], [0.4, 0.6]]
    [logo] = clips.parse_ingredients([{"file_id": LOGO_FILE, "role": "logo", "quad": square}])
    assert logo.quad == ((0.4, 0.4), (0.6, 0.4), (0.6, 0.6), (0.4, 0.6)) and logo.as_dict()["quad"] == square
    with pytest.raises(clips.ClipError, match="quad places a logo"):
        clips.parse_ingredients([{"file_id": LOGO_FILE, "role": "product", "quad": square}])
    with pytest.raises(clips.ClipError, match=r"ingredients\.0\.quad is the logo's four corners"):
        clips.parse_ingredients([{"file_id": LOGO_FILE, "role": "logo", "quad": [[0, 0], [1, 1]]}])
    with pytest.raises(clips.ClipError, match=r"ingredients\.0\.quad: the quad's corners must go"):
        clips.parse_ingredients([{"file_id": LOGO_FILE, "role": "logo", "quad": [[0.6, 0.4], [0.4, 0.4], [0.6, 0.6], [0.4, 0.6]]}])
    # The editor's body says the shape itself: four corners of two numbers each.
    from pydantic import ValidationError

    from app.api.v1.schemas.video_studio import ClipIngredient

    assert ClipIngredient(file_id=LOGO_FILE, role="logo", quad=square).quad == square
    for wrong in ([[1, 2, 3]] * 4, [[0.4], [0.6], [0.6], [0.4]], [[0.4, 0.4]] * 3):
        with pytest.raises(ValidationError):
            ClipIngredient(file_id=LOGO_FILE, role="logo", quad=wrong)


@FFMPEG
def test_run_clip_composites_the_logo_and_reports_its_path(take, tmp_path):
    made, clean, boxes = _logo_frames(20)
    take.video = Path(_encode(tmp_path, made)).read_bytes()
    take.start = _png(clean[0])
    take.logo = _png(_logo())
    take.row.params.update(
        {
            clips.INGREDIENTS: [{"file_id": LOGO_FILE, "role": "logo", "label": "the brand logo"}],
            clips.INGREDIENTS_PATH: clips.PATH_COMPOSITED,
        }
    )
    asyncio.run(runners.run_clip(str(take.row.id)))
    row = take.row
    assert row.status == JOB_READY, row.error
    assert [step["step"] for step in row.params[runners._TAKE_STEPS]] == ["logos"]
    kept = take.tmp / "kept.mp4"
    kept.write_bytes(take.files[str(row.file_id)])
    drawn = _decoded(str(kept))
    x0, y0, x1, y1 = _logo_box(boxes[10])
    assert np.abs(drawn[10][y0:y1, x0:x1] - np.asarray(clean[10], dtype=np.float32)[y0:y1, x0:x1]).mean() < 18
    assert any(note.startswith("The brand logo drawn with its own pixels") for note in row.params["notes"])
    assert clips.reference_note(row.params).startswith("Ingredients (the brand logo) named in the prompt, the logo drawn back")


@FFMPEG
def test_run_clip_fails_a_take_that_loses_its_logo_and_keeps_it(take, tmp_path):
    made, clean, _boxes = _logo_frames(20, cut_at=9)
    take.video = Path(_encode(tmp_path, made)).read_bytes()
    take.start = _png(clean[0])
    take.logo = _png(_logo())
    take.row.params.update(
        {clips.INGREDIENTS: [{"file_id": LOGO_FILE, "role": "logo"}], clips.INGREDIENTS_PATH: clips.PATH_COMPOSITED}
    )
    asyncio.run(runners.run_clip(str(take.row.id)))
    row = take.row
    assert row.status == JOB_FAILED and row.error.startswith("logo lost: the logo at t=0.38 s (frame 9) of the shot")
    assert row.file_id is not None and str(row.file_id) not in take.deleted
    # The frame it was lost in is a file of the take, named by the failure, deleted with the take's frames.
    lost = next(qc for qc in [row.params.get(frame_qc.QC_KEY), *(a.get("frame_qc") for a in row.params.get(clips.ATTEMPTS) or [])] if qc)
    frame_id = lost[frame_qc.LOST_FRAME]
    assert row.error.endswith(f"(frame: file `{frame_id}`)") and take.files[frame_id].startswith(b"\xff\xd8")
    assert frame_id in frame_qc.frame_file_ids({frame_qc.QC_KEY: lost})


def test_a_logo_that_could_not_be_drawn_is_retried_never_kept_garbled(take, monkeypatch):
    take.video, take.start, take.logo = b"MP4", _png(_logo()), _png(_logo())
    take.row.params.update(
        {clips.INGREDIENTS: [{"file_id": LOGO_FILE, "role": "logo"}], clips.INGREDIENTS_PATH: clips.PATH_COMPOSITED}
    )

    async def probe(_path):
        return SimpleNamespace(duration=2.0, width=SIZE[0], height=SIZE[1], fps=FPS, has_audio=False)

    async def poster(_path, *, at, out_path, width):
        Path(out_path).write_bytes(_png(_logo()))

    async def broken(*_args, **_kwargs):
        raise MemoryError("Unable to allocate 2.20 MiB")

    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(encoder, "extract_frame", poster)
    monkeypatch.setattr(logos, "composite", broken)
    outcome = asyncio.run(runners.run_clip(str(take.row.id)))
    # The engine asks again; the take is stored, so the next run draws the logo without paying for a new one.
    assert outcome.kind == "retry" and "Unable to allocate" in outcome.error
    assert take.row.status != JOB_READY and str(take.row.file_id) in take.files


def test_a_step_logged_on_the_stored_take_is_not_drawn_again():
    params = {runners._TAKE_STEPS: [{"step": "screen", "file_id": "a"}, {"step": "logos", "file_id": "b"}]}
    assert runners._steps_done(params, "b") == {"screen", "logos"}
    assert runners._steps_done(params, "a") == {"screen"}
    assert runners._steps_done(params, "raw") == set() and runners._steps_done({}, "b") == set()
