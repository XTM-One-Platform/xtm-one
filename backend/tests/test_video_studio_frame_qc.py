"""The frames of a made AI shot, kept and measured. Synthetic frames and pictures only.

* SSIM on downscaled grey copies: identical pictures measure 1, a moved
  frame less, another picture far less; a start picture of another shape is
  cut to the frame's shape around its centre, as a provider fits it;
* the OCR match is the share of the picture's words the frame still reads,
  from the text check's own transcription;
* ``run_clip`` stores the first, middle and last frames as files of the shot
  and measures them; under ``min_reference_similarity`` the take fails as
  "drifted from reference (ssim 0.41)" with its video kept, and generating it
  again in place replaces its frames with its take;
* the shot serves its frames and measures, and its frames go with it.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from app.llm.video import VideoResult
from app.models.video_studio import JOB_FAILED, JOB_PENDING, JOB_READY, VideoClip
from app.services.video_studio import assets, clips, content_drift, encoder, frame_qc, jobs, runners, text_drift

pytestmark = pytest.mark.usefixtures("no_default_video_template")

USER = SimpleNamespace(id=uuid.UUID("00000000-0000-0000-0000-00000000b002"), role="user")
START = "00000000-0000-0000-0000-0000000000f2"


def _scene(shift: int = 0, size: tuple[int, int] = (640, 360), seed: int = 0) -> Image.Image:
    """A synthetic frame with structure: a gradient sky, a horizon, a few shapes - moved by *shift* pixels."""
    width, height = size
    picture = Image.new("RGB", size)
    draw = ImageDraw.Draw(picture)
    for y in range(height):
        draw.line([(0, y), (width, y)], fill=(40 + y * 100 // height, 80, 160 - y * 80 // height))
    draw.rectangle([0, height * 2 // 3, width, height], fill=(60, 90, 40))
    for index in range(6):
        x = (index * 97 + seed * 53) % (width - 60) + shift
        y = (index * 61 + seed * 31) % (height // 2) + 20
        draw.ellipse([x, y, x + 50, y + 50], fill=(230, 200 - index * 20, 60 + index * 30))
        draw.rectangle([x + 10, height * 2 // 3 - 40, x + 30, height * 2 // 3], fill=(20, 20, 20))
    return picture


def _bytes(picture: Image.Image, kind: str = "JPEG") -> bytes:
    out = io.BytesIO()
    picture.save(out, format=kind)
    return out.getvalue()


# ── The measures ──────────────────────────────────────────────────────────


def test_ssim_tells_the_same_picture_a_moved_one_and_another():
    still = _scene()
    assert frame_qc.ssim(still, still) == 1.0
    moved = frame_qc.ssim(still, _scene(shift=40))
    other = frame_qc.ssim(still, _scene(seed=7).transpose(Image.Transpose.FLIP_TOP_BOTTOM))
    assert 0.3 < moved < 0.95 and other < moved


def test_a_start_picture_of_another_shape_is_cut_to_the_frames_shape():
    wide = _scene(size=(1280, 720))
    # The same picture with bands added above and below (4:3): cut back to 16:9, it is the frame again.
    square = Image.new("RGB", (1280, 960), (255, 255, 255))
    square.paste(wide, (0, 120))
    assert frame_qc.ssim(wide, square) > 0.98
    # Compared squashed rather than cut, it would not be.
    assert frame_qc.ssim(wide, square.resize((1280, 720))) < 0.9


def test_the_text_match_is_the_share_of_the_pictures_words_still_read():
    assert frame_qc.text_match("Threat Intelligence Platform", "THREAT intelligence plattform") == 0.667
    assert frame_qc.text_match("", "anything") is None
    assert frame_qc.text_match("Orchestration", "") == 0.0


def test_a_drift_says_its_similarity_first():
    reason = frame_qc.drift_reason(0.41, 0.6)
    assert reason.startswith("drifted from reference (ssim 0.41)") and "min_reference_similarity 0.60" in reason


def test_the_threshold_needs_a_start_frame_and_a_share():
    assert clips.reference_similarity_threshold(None, start_frame=False) is None
    assert clips.reference_similarity_threshold(0, start_frame=False) is None
    assert clips.reference_similarity_threshold("0.65", start_frame=True) == 0.65
    with pytest.raises(clips.ClipError, match="from 0 to 1"):
        clips.reference_similarity_threshold(1.2, start_frame=True)
    with pytest.raises(clips.ClipError, match="give the shot one"):
        clips.reference_similarity_threshold(0.5, start_frame=False)


# ── run_clip ──────────────────────────────────────────────────────────────


@pytest.fixture
def shot(monkeypatch):
    """``run_clip`` on a shot started from a picture, its video's frames synthetic."""
    row = VideoClip(
        id=uuid.uuid4(),
        user_id=USER.id,
        provider="gemini",
        model="veo-3.1-generate-preview",
        prompt="A slow push in on the booth",
        params={"provider_id": str(uuid.uuid4()), "duration_seconds": 8, "reference_file_id": START, "content_check": False},
        status=JOB_PENDING,
    )
    world = SimpleNamespace(
        row=row,
        start=_scene(),
        frames={0.0: _scene(), 4.0: _scene(shift=12), None: _scene(shift=30)},
        stored=[],
        deleted=[],
        texts=None,
    )

    class _Db:
        async def get(self, _model, _key):
            return row

        async def commit(self):
            return None

    @asynccontextmanager
    async def session():
        yield _Db()

    class _Provider:
        async def generate_video(self, request, **_kwargs):
            return VideoResult(data=b"MP4", job_id="op/1")

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        return _Provider(), model, SimpleNamespace(id=provider_id, provider="gemini")

    async def mark(_kind, _job_id, **changes):
        for key, value in changes.items():
            setattr(row, key, value)

    async def store_path(path, **kwargs):
        stored = SimpleNamespace(id=uuid.uuid4(), path=path, **kwargs)
        world.stored.append(stored)
        return stored

    async def delete_files(ids):
        world.deleted.extend(str(i) for i in ids or [] if i)

    async def probe(_path):
        return SimpleNamespace(duration=8.0, width=640, height=360, has_audio=False)

    async def poster(_path, *, at, out_path, width):
        Path(out_path).write_bytes(_bytes(world.frames[0.0]))

    async def extract_still(_video, *, at, out_path, max_side, crop_to=None):
        Path(out_path).write_bytes(_bytes(world.frames[at]))

    async def start_frame(params, _workdir, **_kwargs):
        if not params.get("reference_file_id"):
            return None
        return clips.StartFrame(_bytes(world.start, "PNG"), "image/png", file_id=START)

    async def pictures(_params, _workdir):
        return []

    async def text_check(*_args, **_kwargs):
        return text_drift.TextCheck(True, texts=world.texts)

    monkeypatch.setattr(runners, "async_session", session)
    monkeypatch.setattr(runners, "bind_llm_caller", lambda *_a, **_k: asyncio.sleep(0))
    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(clips, "start_frame", start_frame)
    monkeypatch.setattr(clips, "reference_pictures", pictures)
    monkeypatch.setattr(jobs, "mark", mark)
    monkeypatch.setattr(assets, "store_path", store_path)
    monkeypatch.setattr(assets, "delete_files", delete_files)
    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(encoder, "extract_frame", poster)
    monkeypatch.setattr(encoder, "extract_still", extract_still)
    monkeypatch.setattr(text_drift, "check", text_check)
    monkeypatch.setattr(content_drift, "check", lambda *_a, **_k: asyncio.sleep(0, content_drift.ContentCheck(True)))
    return world


def test_a_made_shot_keeps_its_three_frames_and_measures_them(shot):
    shot.texts = {"start_picture": "Live threat intelligence", "first_frame": "Live threat intellgence", "last_frame": ""}
    asyncio.run(runners.run_clip(str(shot.row.id)))
    row = shot.row
    assert row.status == JOB_READY
    kept = row.params[frame_qc.QC_KEY]
    frames = [s for s in shot.stored if s.filename.endswith(("-first.jpg", "-mid.jpg", "-last.jpg"))]
    assert [kept[name] for name, _at in frame_qc.FRAMES] == [str(s.id) for s in frames]
    assert all(s.source == assets.SOURCE_CLIP and s.user_id == USER.id for s in frames)
    # The first frame is the start picture redrawn: alike. The camera moved by the last.
    assert kept["reference_ssim"] > 0.95
    assert kept["motion_ssim"] < kept["reference_ssim"]
    assert kept["reference_text_match"] == 0.667
    served = frame_qc.summary(row.params)
    assert served["first_frame_file_id"] == kept["first_frame_file_id"]
    # The whole frame's change is served beside motion_ssim; no camera was asked, so none was measured.
    assert set(served["frame_qc"]) == {"reference_ssim", "reference_text_match", "motion_ssim", frame_qc.FRAME_CHANGE}
    assert served["frame_qc"][frame_qc.FRAME_CHANGE] == frame_qc.frame_change(kept)


def test_a_take_under_its_threshold_fails_as_drifted_and_keeps_its_video(shot):
    shot.row.params[clips.MIN_REFERENCE_SIMILARITY] = 0.8
    shot.frames[0.0] = _scene(seed=5).transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    asyncio.run(runners.run_clip(str(shot.row.id)))
    row = shot.row
    similarity = row.params[frame_qc.QC_KEY]["reference_ssim"]
    assert row.status == JOB_FAILED and row.error.startswith(f"drifted from reference (ssim {similarity:.2f})")
    # Its video and its frames stay on the row to look at.
    assert row.file_id is not None and row.poster_file_id is not None
    assert len(frame_qc.frame_file_ids(row.params)) == 3


def test_a_take_over_its_threshold_is_ready(shot):
    shot.row.params[clips.MIN_REFERENCE_SIMILARITY] = 0.8
    asyncio.run(runners.run_clip(str(shot.row.id)))
    assert shot.row.status == JOB_READY


def test_a_take_whose_camera_moves_past_its_tolerance_fails_and_keeps_its_video(shot):
    """max_camera_motion measures the camera on the frames, never on a still."""
    shot.row.params[frame_qc.MAX_CAMERA_MOTION] = 0.01
    asyncio.run(runners.run_clip(str(shot.row.id)))
    row = shot.row
    motion = frame_qc.camera_motion(row.params[frame_qc.QC_KEY])
    assert motion is not None and motion > 0.01
    assert row.status == JOB_FAILED and row.error.startswith(f"camera moved: its last frame changed {motion:.2f}")
    assert "max_camera_motion 0.01" in row.error and row.file_id is not None


def test_a_take_within_its_camera_tolerance_is_ready(shot):
    shot.row.params[frame_qc.MAX_CAMERA_MOTION] = 0.99
    asyncio.run(runners.run_clip(str(shot.row.id)))
    assert shot.row.status == JOB_READY


def test_each_kept_frame_says_what_it_is(shot):
    """A frame read once in a turn answers the next read with its caption, never a refusal."""
    asyncio.run(runners.run_clip(str(shot.row.id)))
    kept = shot.row.params[frame_qc.QC_KEY]
    by_name = {s.filename.rsplit("-", 1)[-1]: s.description for s in shot.stored if s.filename.endswith(".jpg")}
    assert by_name["first.jpg"].startswith('The first frame (0.0 s) of the AI shot "A slow push in on the booth", ')
    assert f"keeps {kept['reference_ssim']:.2f} of its start frame's structure" in by_name["first.jpg"]
    assert by_name["mid.jpg"].startswith("The middle frame (4.0 s) of the AI shot")
    assert by_name["last.jpg"].startswith("The last frame (8.0 s)") and f"{kept['motion_ssim']:.2f} of the first" in by_name["last.jpg"]
    assert by_name["poster.jpg"].startswith('The poster of the AI shot "A slow push in on the booth"')


def test_the_camera_tolerance_is_a_share_and_1_drops_it():
    assert clips.camera_motion_tolerance(None) is None and clips.camera_motion_tolerance("") is None
    assert clips.camera_motion_tolerance("0.3") == 0.3 and clips.camera_motion_tolerance(1) is None
    with pytest.raises(clips.ClipError, match="from 0 to 1"):
        clips.camera_motion_tolerance(1.4)
    assert frame_qc.camera_motion({"motion_ssim": 0.62}) == 0.38 and frame_qc.camera_motion({}) is None


def test_a_drifted_take_generated_again_replaces_its_frames_with_its_take(shot, monkeypatch):
    shot.row.params[clips.MIN_REFERENCE_SIMILARITY] = 0.8
    shot.frames[0.0] = _scene(seed=5).transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    asyncio.run(runners.run_clip(str(shot.row.id)))
    old = frame_qc.frame_file_ids(shot.row.params)

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        return SimpleNamespace(max_reference_images=lambda m: 0, reference_images_with_start_frame=lambda m: True), model, SimpleNamespace(
            id=uuid.uuid4(), provider="gemini"
        )

    async def enqueue(*_args, **_kwargs):
        return None

    class _Db:
        async def refresh(self, _row, with_for_update=False):
            return None

        async def flush(self):
            return None

    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    asyncio.run(clips.restart_clip(_Db(), USER, shot.row, prompt="A slower push in on the booth"))
    params = shot.row.params
    assert shot.row.status == JOB_PENDING and frame_qc.QC_KEY not in params
    assert set(old) <= set(params[clips.REPLACED_MEDIA])
    assert params[clips.MIN_REFERENCE_SIMILARITY] == 0.8


def test_frames_that_cannot_be_cut_leave_the_shot_ready_and_say_the_threshold_was_not_applied(shot, monkeypatch):
    async def broken(*_args, **_kwargs):
        raise encoder.EncoderError("no frame")

    monkeypatch.setattr(encoder, "extract_still", broken)
    shot.row.params[clips.MIN_REFERENCE_SIMILARITY] = 0.8
    asyncio.run(runners.run_clip(str(shot.row.id)))
    assert shot.row.status == JOB_READY and frame_qc.cut_file_ids(shot.row.params) == []
    # No frame, no measure: only the text check's record, which reads its own frames, stays with the take.
    summary = frame_qc.summary(shot.row.params)
    assert summary["frame_qc"] is None and summary["first_frame_file_id"] is None and summary["text_check"]["status"] == "passed"
    assert any("min_reference_similarity was not applied" in note for note in shot.row.params["notes"])


def test_a_shot_without_a_start_frame_measures_only_its_motion(shot):
    del shot.row.params["reference_file_id"]
    asyncio.run(runners.run_clip(str(shot.row.id)))
    kept = shot.row.params[frame_qc.QC_KEY]
    assert "reference_ssim" not in kept and "motion_ssim" in kept


def test_the_frames_of_a_shot_go_with_it():
    params = {frame_qc.QC_KEY: {"first_frame_file_id": "a", "mid_frame_file_id": "b", "last_frame_file_id": "c", "motion_ssim": 0.9}}
    assert frame_qc.frame_file_ids(params) == ["a", "b", "c"]
    assert frame_qc.frame_file_ids({}) == [] and frame_qc.summary({})["frame_qc"] is None
    from app.services.video_studio.jobs import serialize_clip

    served = serialize_clip(
        VideoClip(id=uuid.uuid4(), provider="gemini", model="veo", prompt="x", params=params, status=JOB_READY, keep=False)
    )
    assert (served["first_frame_file_id"], served["last_frame_file_id"], served["frame_qc"]) == ("a", "c", {"motion_ssim": 0.9})
