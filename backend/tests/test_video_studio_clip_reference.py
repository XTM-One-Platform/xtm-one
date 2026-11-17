"""An AI shot starts from a frame of a recording, of another shot or of a video file.

Every video model takes a start image, so footage is reworked from its own
frame instead of from a still generated to imitate it:

* ``clips.resolve_reference`` checks the first frame before anything is paid
  for - one source, readable by the person (a non-admin here), ready, and a
  step or a moment inside it - and records it in the shot's params;
* ``clips.start_frame`` takes that frame in the worker (the frame a
  recording's contact sheet shows after the step, a moment, else the last),
  and a source gone since fails the shot instead of a shot from the prompt alone;
  the frame is kept as a file of the shot's conversation and a shot generated
  again starts from that file without cutting it again;
* ``runners.run_clip`` hands it to the provider as the start image and says
  in the notes which frame it was; a generation resumed upstream needs none;
* the tool's answer and the editor's media token show what the shot opened on.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.llm.video import VideoResult
from app.models.file_attachment import FileAttachment
from app.models.video_studio import (
    JOB_FAILED,
    JOB_PENDING,
    JOB_READY,
    JOB_RUNNING,
    VideoClip,
    VideoTake,
)
from app.services.video_studio import (
    assets,
    capture,
    clips,
    content_drift,
    encoder,
    frame_qc,
    jobs,
    projects,
    runners,
    text_drift,
)

pytestmark = pytest.mark.usefixtures("no_default_video_template")

OWNER = uuid.UUID("00000000-0000-0000-0000-00000000a001")
TAKE_ID = "00000000-0000-0000-0000-0000000000b1"
TAKE_FILE = "00000000-0000-0000-0000-0000000000b2"
CLIP_ID = "00000000-0000-0000-0000-0000000000c1"
CLIP_FILE = "00000000-0000-0000-0000-0000000000c2"
VIDEO_FILE = "00000000-0000-0000-0000-0000000000d1"
IMAGE_FILE = "00000000-0000-0000-0000-0000000000d2"
PDF_FILE = "00000000-0000-0000-0000-0000000000d3"
FRAME_FILE = "00000000-0000-0000-0000-0000000000e1"
CONVERSATION = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
UNKNOWN = "00000000-0000-0000-0000-0000000000ff"

def _png() -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (64, 36), (12, 80, 200)).save(out, format="PNG")
    return out.getvalue()


#: A start picture every provider takes as it is.
PNG = _png()

EVENTS = [
    {"step": 0, "action": "goto", "t0": 0.0, "ta": 0.0, "t1": 1.2},
    {"step": 1, "action": "click", "label": "Risk", "t0": 1.4, "ta": 2.0, "t1": 2.4},
    {"step": 2, "action": "click", "label": "Dashboard", "t0": 3.0, "ta": 3.6, "t1": 4.0},
    {"step": 3, "action": "hover", "t0": 5.0, "ta": 5.0, "t1": 5.0, "ok": False},
]


def _take(**values) -> VideoTake:
    fields = {
        "id": uuid.UUID(TAKE_ID),
        "user_id": OWNER,
        "name": "OpenCRQ demo",
        "status": JOB_READY,
        "duration_ms": 42_000,
        "file_id": uuid.UUID(TAKE_FILE),
        "events": EVENTS,
        "expired_at": None,
    }
    return VideoTake(**{**fields, **values})


def _clip(**values) -> VideoClip:
    fields = {
        "id": uuid.UUID(CLIP_ID),
        "user_id": OWNER,
        "name": None,
        "provider": "gemini",
        "model": "veo-3.1",
        "prompt": "Slow orbit around a dark globe",
        "params": {},
        "status": JOB_READY,
        "duration_ms": 8_000,
        "file_id": uuid.UUID(CLIP_FILE),
        "expired_at": None,
    }
    return VideoClip(**{**fields, **values})


def _file(file_id: str, content_type: str, filename: str) -> FileAttachment:
    return FileAttachment(id=uuid.UUID(file_id), filename=filename, content_type=content_type)


class _Db:
    """The rows a start reads, by model and id; the clip it adds."""

    def __init__(self, *rows):
        self.rows = {(type(row), str(row.id)): row for row in rows}
        self.added: list = []

    async def get(self, model, key):
        return self.rows.get((model, str(key)))

    async def execute(self, _statement):
        # The render whose MP4 a video file is (``projects.visible_render_file``): none made these.
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: None))

    def add(self, row) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None


@pytest.fixture
def env(monkeypatch):
    """A non-admin who owns nothing but what a row says, with no video shared to them."""
    world = SimpleNamespace(
        enqueued=[], shared=False, readable_files={VIDEO_FILE, IMAGE_FILE, PDF_FILE}, pictures_checked=[]
    )

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        return object(), model or "veo-3.1", SimpleNamespace(id=uuid.uuid4(), provider="gemini", display_name="Gemini")

    async def enqueue(_db, kind, row, **_kwargs):
        world.enqueued.append((kind, row))

    async def in_visible_project(_db, _kind, _asset_id, _user):
        return world.shared

    async def can_read_file(row, _user, _db):
        return str(row.id) in world.readable_files

    async def check_start_picture(row):
        world.pictures_checked.append(str(row.id))

    monkeypatch.setattr(clips, "check_start_picture", check_start_picture)
    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    monkeypatch.setattr(projects, "_asset_in_visible_project", in_visible_project)
    monkeypatch.setattr("app.services.file_access.can_read_file", can_read_file)
    world.user = SimpleNamespace(id=OWNER, role="user")
    world.db = _Db(
        _take(),
        _clip(),
        _file(VIDEO_FILE, "video/mp4", "keynote.mp4"),
        _file(IMAGE_FILE, "image/png", "dashboard.png"),
        _file(PDF_FILE, "application/pdf", "report.pdf"),
    )
    return world


def _start(env, **reference) -> dict:
    """``clips.start_clip`` as both the assistant and the editor call it: the params the shot records."""
    clip, _label = asyncio.run(clips.start_clip(env.db, env.user, prompt="The dashboard comes alive", **reference))
    return clip.params


# ── The first frame is checked before anything is paid for ────────────────


def test_a_recording_step_starts_the_shot_on_the_frame_its_contact_sheet_shows(env):
    params = _start(env, reference_recording_id=TAKE_ID, reference_at_step=2)
    assert params["reference_file_id"] is None
    assert params["reference_video"] == {
        "kind": "recording",
        "id": TAKE_ID,
        "file_id": TAKE_FILE,
        "name": "OpenCRQ demo",
        "at_seconds": 4.5,
        "step": 2,
    }
    # The contact sheet picks its frame for that step with the same rule.
    result = capture.CaptureResult(
        workdir="",
        frames=[(t / 10, f"f{t}.jpg") for t in range(420)],
        duration=42.0,
        events=EVENTS,
        cursor=[],
        viewport=(1600, 900),
        pixel_ratio=1.5,
        frame_size=None,
        storage_state=None,
        signed_in=None,
    )
    shown = {label.split(" ")[1]: t for t, _name, label in capture.pick_sheet_frames(result)}
    assert shown["2"] == capture.step_result_seconds(EVENTS[2], 42.0) == 4.5
    assert len(env.enqueued) == 1


def test_without_a_moment_a_recording_or_a_shot_is_continued_from_its_last_frame(env):
    recording = _start(env, reference_recording_id=TAKE_ID)["reference_video"]
    assert (recording["kind"], recording["at_seconds"], "step" in recording) == ("recording", None, False)
    shot = _start(env, reference_clip_id=CLIP_ID)["reference_video"]
    assert shot == {
        "kind": "shot",
        "id": CLIP_ID,
        "file_id": CLIP_FILE,
        "name": "Slow orbit around a dark globe",
        "at_seconds": None,
    }


def test_a_video_file_gives_its_frame_and_an_image_stays_the_frame_it_is(env):
    video = _start(env, reference_file_id=VIDEO_FILE, reference_at_seconds="12.25")
    assert video["reference_file_id"] is None
    assert video["reference_video"] == {
        "kind": "file",
        "id": VIDEO_FILE,
        "file_id": VIDEO_FILE,
        "name": "keynote.mp4",
        "at_seconds": 12.25,
    }
    image = _start(env, reference_file_id=IMAGE_FILE)
    assert image["reference_file_id"] == IMAGE_FILE and "reference_video" not in image
    # A start picture is read before anything is paid for; a video's frame is taken by the worker.
    assert env.pictures_checked == [IMAGE_FILE]
    # No first frame at all: nothing recorded.
    plain = _start(env)
    assert plain["reference_file_id"] is None and "reference_video" not in plain


@pytest.mark.parametrize(
    ("reference", "refusal"),
    [
        pytest.param(
            {"reference_recording_id": TAKE_ID, "reference_clip_id": CLIP_ID},
            "give one of reference_recording_id, reference_clip_id",
            id="two-sources",
        ),
        pytest.param(
            {"reference_recording_id": TAKE_ID, "reference_at_step": 1, "reference_at_seconds": 2},
            "not both",
            id="step-and-moment",
        ),
        pytest.param(
            {"reference_clip_id": CLIP_ID, "reference_at_step": 1},
            "give reference_recording_id with it",
            id="step-of-a-shot",
        ),
        pytest.param({"reference_at_seconds": 2}, "name the recording, shot or video file", id="moment-of-nothing"),
        pytest.param(
            {"reference_recording_id": TAKE_ID, "reference_at_step": 9},
            "has no step 9: its steps are 0 to 3",
            id="no-such-step",
        ),
        pytest.param(
            {"reference_recording_id": TAKE_ID, "reference_at_step": 3}, "skipped step 3", id="skipped-step"
        ),
        pytest.param(
            {"reference_recording_id": TAKE_ID, "reference_at_seconds": 50},
            "lasts 42.0 s",
            id="past-the-recording",
        ),
        pytest.param({"reference_clip_id": CLIP_ID, "reference_at_seconds": 9}, "lasts 8.0 s", id="past-the-shot"),
        pytest.param(
            {"reference_file_id": IMAGE_FILE, "reference_at_seconds": 1}, "An image is one frame", id="moment-of-image"
        ),
        pytest.param({"reference_file_id": PDF_FILE}, "No image or video", id="not-a-picture"),
        pytest.param({"reference_file_id": UNKNOWN}, "No image or video", id="unknown-file"),
        pytest.param({"reference_recording_id": UNKNOWN}, "No recording", id="unknown-recording"),
        pytest.param({"reference_clip_id": UNKNOWN}, "No AI shot", id="unknown-shot"),
        pytest.param(
            {"reference_recording_id": TAKE_ID, "reference_at_seconds": -1}, "must be 0 or more", id="negative"
        ),
        pytest.param(
            {"reference_recording_id": TAKE_ID, "reference_at_step": 1.5}, "whole number", id="half-a-step"
        ),
        pytest.param(
            {"reference_recording_id": TAKE_ID, "reference_at_seconds": "soon"}, "must be a number", id="not-a-number"
        ),
    ],
)
def test_a_first_frame_that_cannot_be_used_is_refused_before_anything_is_paid_for(env, reference, refusal):
    with pytest.raises(clips.ClipError, match=refusal):
        _start(env, **reference)
    assert env.enqueued == [] and env.db.added == []


@pytest.mark.parametrize(
    ("state", "refusal"),
    [
        pytest.param({"status": JOB_PENDING}, "is still being made", id="pending"),
        pytest.param({"status": JOB_RUNNING}, "is still being made", id="running"),
        pytest.param({"status": JOB_FAILED, "file_id": None}, "failed and made no video", id="failed"),
        pytest.param({"expired_at": datetime.now(UTC)}, "has no media any more", id="expired"),
    ],
)
def test_a_recording_or_a_shot_with_no_video_yet_or_any_more_is_refused(env, state, refusal):
    env.db = _Db(_take(**state), _clip(**state))
    for reference in ({"reference_recording_id": TAKE_ID}, {"reference_clip_id": CLIP_ID}):
        with pytest.raises(clips.ClipError, match=refusal):
            _start(env, **reference)
    assert env.enqueued == []


def test_another_persons_footage_is_refused_unless_a_video_they_share_uses_it(env):
    env.user = SimpleNamespace(id=uuid.uuid4(), role="user")
    with pytest.raises(clips.ClipError, match="No recording"):
        _start(env, reference_recording_id=TAKE_ID)
    with pytest.raises(clips.ClipError, match="No AI shot"):
        _start(env, reference_clip_id=CLIP_ID)
    env.readable_files = set()
    with pytest.raises(clips.ClipError, match="No image or video"):
        _start(env, reference_file_id=VIDEO_FILE)
    # The rule of a scene: a video they can open uses it.
    env.shared = True
    assert _start(env, reference_recording_id=TAKE_ID)["reference_video"]["id"] == TAKE_ID
    assert _start(env, reference_clip_id=CLIP_ID)["reference_video"]["id"] == CLIP_ID


# ── The frame has the shot's shape ────────────────────────────────


def test_a_frame_of_another_shape_than_the_shot_is_refused_unless_cropped(env):
    # A 1.5x recording of a 1600x900 page: 2400x1350, a 16:9 frame.
    env.db = _Db(_take(viewport_width=1600, viewport_height=900, pixel_ratio=1.5), _clip(width=1280, height=720))
    assert "crop" not in _start(env, reference_recording_id=TAKE_ID)["reference_video"]
    with pytest.raises(clips.ClipError) as refused:
        _start(env, reference_recording_id=TAKE_ID, aspect_ratio="9:16")
    assert str(refused.value) == (
        f'Recording `{TAKE_ID}` is 2400x1350, not 9:16 like the shot: pass crop: "center" to start from its '
        "middle 758x1350, or make the shot 16:9."
    )
    with pytest.raises(clips.ClipError, match=r"AI shot `.*` is 1280x720, not 9:16 like the shot"):
        _start(env, reference_clip_id=CLIP_ID, aspect_ratio="9:16")
    # Only the 16:9 shot was queued: a refusal costs nothing.
    assert len(env.enqueued) == 1
    # Cropped, it starts from the middle: the worker cuts it so.
    cropped = _start(env, reference_recording_id=TAKE_ID, aspect_ratio="9:16", crop="center")
    assert cropped["reference_video"]["crop"] == "center" and cropped["aspect_ratio"] == "9:16"


def test_a_frame_close_to_the_shots_shape_is_its_shape_and_16_10_is_not(env):
    # An encoder's rounding (1920x1088) is the shot's shape; a 16:10 page is not, and is not 9:16 either.
    env.db = _Db(_take(viewport_width=1440, viewport_height=900, pixel_ratio=1), _clip(width=1920, height=1088))
    assert _start(env, reference_clip_id=CLIP_ID)["reference_video"]["kind"] == "shot"
    with pytest.raises(clips.ClipError) as refused:
        _start(env, reference_recording_id=TAKE_ID)
    assert str(refused.value).endswith('pass crop: "center" to start from its middle 1440x810.')
    # A recording made before its size was recorded, or a video file, is measured by the worker.
    env.db = _Db(_take(), _file(VIDEO_FILE, "video/mp4", "keynote.mp4"))
    assert _start(env, reference_recording_id=TAKE_ID, aspect_ratio="9:16")["reference_video"]["id"] == TAKE_ID
    assert _start(env, reference_file_id=VIDEO_FILE, aspect_ratio="9:16")["reference_video"]["kind"] == "file"


@pytest.mark.parametrize(
    ("reference", "refusal"),
    [
        pytest.param({"reference_recording_id": TAKE_ID, "crop": "left"}, 'crop is "center"', id="unknown-crop"),
        pytest.param({"reference_file_id": IMAGE_FILE, "crop": "center"}, "An image is sent as it is", id="image"),
        pytest.param({"crop": "center"}, "name the recording, shot or video file", id="nothing-to-crop"),
    ],
)
def test_crop_fits_a_frame_of_a_video_only(env, reference, refusal):
    with pytest.raises(clips.ClipError, match=refusal):
        _start(env, **reference)
    assert env.enqueued == []


def test_a_frame_kept_as_a_picture_has_any_shape(env):
    # save_video_frame is no shot: a vertical AI shot's frame is kept as it is.
    env.db = _Db(_clip(width=1080, height=1920))
    source = asyncio.run(clips.resolve_frame(env.db, env.user, clip_id=CLIP_ID))
    assert source["id"] == CLIP_ID and "crop" not in source


# ── The worker takes the frame ────────────────────────────────────────────


def _source(**values) -> dict:
    return {"kind": "recording", "id": TAKE_ID, "file_id": TAKE_FILE, "name": "OpenCRQ demo", "at_seconds": None, **values}


def _jpeg(width: int, height: int) -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (40, 90, 200)).save(buffer, "JPEG")
    return buffer.getvalue()


@pytest.fixture
def media(monkeypatch):
    """Stored files, ffprobe and ffmpeg as the worker reaches them; ``gone`` files are deleted.

    A kept frame downloads as ``kept`` (a 16:9 JPEG), a cut one is ``FRAME``;
    the video is ``size`` pixels; ``stored`` is every file saved, with what it
    was saved as.
    """
    world = SimpleNamespace(
        downloads=[],
        stills=[],
        stored=[],
        gone=set(),
        duration=42.0,
        size=(2400, 1350),
        kept=_jpeg(32, 18),
        broken=False,
        full=False,
    )

    async def download(file_id, path):
        world.downloads.append(str(file_id))
        if str(file_id) in world.gone:
            return None
        data = PNG if str(file_id) == IMAGE_FILE else world.kept if str(file_id) == FRAME_FILE else b"VIDEO"
        Path(path).write_bytes(data)
        return SimpleNamespace(content_type="image/png" if str(file_id) == IMAGE_FILE else "video/mp4")

    async def probe(_path):
        return SimpleNamespace(duration=world.duration, width=world.size[0], height=world.size[1], has_audio=False)

    async def extract_still(_path, *, at, out_path, max_side, crop_to=None):
        if world.broken:
            raise encoder.EncoderError("ffmpeg failed (exit 1):\nInvalid data found when processing input")
        world.stills.append((at, max_side) if crop_to is None else (at, max_side, crop_to))
        Path(out_path).write_bytes(b"FRAME")

    async def store_path(path, **saved):
        if world.full:
            raise OSError("the object store refused the upload")
        world.stored.append({**saved, "data": Path(path).read_bytes()})
        frame = saved["source"] == assets.SOURCE_CLIP_START_FRAME
        return SimpleNamespace(id=uuid.UUID(FRAME_FILE) if frame else uuid.uuid4())

    monkeypatch.setattr(assets, "download_file", download)
    monkeypatch.setattr(assets, "store_path", store_path)
    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(encoder, "extract_still", extract_still)
    return world


def test_the_worker_takes_the_frame_the_shot_was_asked_from(media, tmp_path):
    frame = asyncio.run(clips.start_frame({"reference_video": _source(at_seconds=4.5, step=2)}, str(tmp_path)))
    assert (frame.data, frame.mime) == (b"FRAME", "image/jpeg")
    assert media.downloads == [TAKE_FILE] and media.stills == [(4.5, clips.START_FRAME_MAX_SIDE)]
    # The last frame, asked or a moment at the very end: decoded from the end, never sought past it.
    asyncio.run(clips.start_frame({"reference_video": _source()}, str(tmp_path)))
    asyncio.run(clips.start_frame({"reference_video": _source(at_seconds=41.98)}, str(tmp_path)))
    assert [at for at, _side in media.stills[1:]] == [None, None]
    # A PNG or a JPEG is the frame as it is (any other picture is converted): nothing cut, nothing saved.
    image = asyncio.run(clips.start_frame({"reference_file_id": IMAGE_FILE}, str(tmp_path)))
    assert image == clips.StartFrame(PNG, "image/png", file_id=IMAGE_FILE) and len(media.stills) == 3
    assert len(media.stored) == 3
    assert asyncio.run(clips.start_frame({"provider_id": "x"}, str(tmp_path))) is None


def test_a_frame_cut_from_a_video_is_kept_as_a_file_of_the_shots_conversation(media, tmp_path):
    params = {"reference_video": _source(at_seconds=4.5, step=2)}
    frame = asyncio.run(clips.start_frame(params, str(tmp_path), user_id=OWNER, conversation_id=CONVERSATION))
    assert frame == clips.StartFrame(b"FRAME", "image/jpeg", file_id=FRAME_FILE, saved=True)
    assert media.stored == [
        {
            "filename": "opencrq-demo-start-frame-4.5s.jpg",
            "user_id": OWNER,
            "source": "clip-start-frame",
            "content_type": "image/jpeg",
            "conversation_id": CONVERSATION,
            # What it is, so a second read in a turn answers with it.
            "description": 'The frame after step 2 (4.5 s) of the recording "OpenCRQ demo": the frame an AI shot starts from.',
            "data": b"FRAME",
        }
    ]
    assert params["reference_video"] == {**_source(at_seconds=4.5, step=2), "frame_file_id": FRAME_FILE}
    assert clips.start_frame_file_id(params) == FRAME_FILE
    # The start image is what an image-started shot opened on; a shot with neither has none.
    assert clips.start_frame_file_id({"reference_file_id": IMAGE_FILE}) == IMAGE_FILE
    assert clips.start_frame_file_id({"reference_video": _source()}) is None
    assert clips.start_frame_file_id(None) is None


def test_a_shot_generated_again_starts_from_the_kept_frame_without_cutting_it(media, tmp_path):
    params = {"reference_video": _source(at_seconds=4.5, step=2, frame_file_id=FRAME_FILE)}
    # The recording itself may be gone by now: the kept frame is all a new generation needs.
    media.gone = {TAKE_FILE}
    frame = asyncio.run(clips.start_frame(params, str(tmp_path), user_id=OWNER, conversation_id=CONVERSATION))
    assert frame == clips.StartFrame(media.kept, "image/jpeg", file_id=FRAME_FILE)
    assert media.downloads == [FRAME_FILE] and media.stills == [] and media.stored == []


def test_a_kept_frame_deleted_since_is_cut_again_and_kept_anew(media, tmp_path):
    media.gone = {FRAME_FILE}
    params = {"reference_video": _source(at_seconds=4.5, frame_file_id=FRAME_FILE)}
    frame = asyncio.run(clips.start_frame(params, str(tmp_path), user_id=OWNER))
    assert (frame.data, frame.saved) == (b"FRAME", True)
    assert media.downloads == [FRAME_FILE, TAKE_FILE] and len(media.stills) == len(media.stored) == 1


def test_a_frame_that_could_not_be_kept_still_starts_the_shot(media, tmp_path):
    media.full = True
    params = {"reference_video": _source(at_seconds=4.5)}
    frame = asyncio.run(clips.start_frame(params, str(tmp_path), user_id=OWNER))
    assert frame == clips.StartFrame(b"FRAME", "image/jpeg")
    assert "frame_file_id" not in params["reference_video"]


def test_the_worker_measures_a_video_file_and_crops_what_is_asked_cropped(media, tmp_path):
    media.size = (720, 1280)
    upright = {"aspect_ratio": "16:9", "reference_video": _source(kind="file", name="keynote.mp4")}
    with pytest.raises(clips.ClipError, match=r'The video "keynote.mp4" is 720x1280, not 16:9 like the shot: pass'):
        asyncio.run(clips.start_frame(upright, str(tmp_path)))
    assert media.stills == [] and media.stored == []
    upright["reference_video"]["crop"] = "center"
    asyncio.run(clips.start_frame(upright, str(tmp_path)))
    assert media.stills == [(None, clips.START_FRAME_MAX_SIDE, (16, 9))]


def test_a_kept_frame_of_another_shape_than_the_shot_now_asked_is_cut_again(media, tmp_path):
    # Kept for a 16:9 shot, then generated again as 9:16: the kept frame no longer fits.
    params = {"aspect_ratio": "9:16", "reference_video": _source(at_seconds=4.5, crop="center", frame_file_id=FRAME_FILE)}
    frame = asyncio.run(clips.start_frame(params, str(tmp_path), user_id=OWNER))
    assert frame.saved and media.stills == [(4.5, clips.START_FRAME_MAX_SIDE, (9, 16))]
    assert clips.start_note(params) == (
        'Started from the frame at 4.5 s of the recording "OpenCRQ demo", its middle cut to the shot\'s shape.'
    )


def test_the_notes_say_which_frame_of_which_video_the_shot_started_from():
    assert (
        clips.start_note({"reference_video": _source(at_seconds=4.5, step=2)})
        == 'Started from the frame after step 2 (4.5 s) of the recording "OpenCRQ demo".'
    )
    assert (
        clips.start_note({"reference_video": _source(kind="shot", name="Globe", at_seconds=3.0)})
        == 'Started from the frame at 3.0 s of the AI shot "Globe".'
    )
    assert (
        clips.start_note({"reference_video": _source(kind="file", name="keynote.mp4")})
        == 'Started from the last frame of the video "keynote.mp4".'
    )
    assert clips.start_note({"reference_file_id": IMAGE_FILE}) is None


@pytest.mark.parametrize(
    ("params", "refusal"),
    [
        pytest.param({"reference_file_id": IMAGE_FILE}, "The image this shot starts from is gone", id="image-gone"),
        pytest.param(
            {"reference_video": _source()},
            'The recording "OpenCRQ demo" this shot starts from is gone',
            id="recording-gone",
        ),
    ],
)
def test_a_first_frame_gone_since_fails_the_shot_rather_than_making_another(media, tmp_path, params, refusal):
    media.gone = {IMAGE_FILE, TAKE_FILE}
    with pytest.raises(clips.ClipError, match=refusal):
        asyncio.run(clips.start_frame(params, str(tmp_path)))


def test_a_moment_past_a_video_files_end_or_an_unreadable_video_fails_the_shot(media, tmp_path):
    media.duration = 10.0
    late = {"reference_video": _source(kind="file", name="keynote.mp4", at_seconds=12.0)}
    with pytest.raises(clips.ClipError, match=r'The video "keynote.mp4" lasts 10.0 s: the shot cannot start at 12.0 s'):
        asyncio.run(clips.start_frame(late, str(tmp_path)))
    media.broken = True
    with pytest.raises(clips.ClipError, match="No frame could be taken from the recording"):
        asyncio.run(clips.start_frame({"reference_video": _source()}, str(tmp_path)))


# ── The shot is asked with it ─────────────────────────────────────────────


@pytest.fixture
def worker(media, monkeypatch):
    """``runners.run_clip`` on one shot row, its provider recording what it was asked."""
    world = SimpleNamespace(clip=None, marks=[], requests=[], marks_before_provider=None, media=media, text_checks=[])

    class _Session:
        async def get(self, _model, _key):
            return world.clip

        async def commit(self):
            return None

    @asynccontextmanager
    async def session():
        yield _Session()

    class _Provider:
        provider_name = "gemini"

        def takes_negative_prompt(self, model):
            return True

        async def generate_video(self, request, *, progress=None, on_job=None, resume_job=None, on_state=None):
            world.requests.append((request.reference_image, request.reference_mime, resume_job))
            world.marks_before_provider = list(world.marks)
            return VideoResult(data=b"MP4", job_id=resume_job or "operations/1", notes=["Duration snapped to 8 s."])

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        return _Provider(), model, SimpleNamespace(id=provider_id, provider="gemini")

    async def mark(_kind, _job_id, **values):
        world.marks.append(copy.deepcopy(values))

    async def poster(_path, *, at, out_path, width):
        Path(out_path).write_bytes(b"JPG")

    async def nothing(*_args, **_kwargs):
        return None

    async def text_check(_user_id, picture, _video_path, _workdir, **_options):
        world.text_checks.append(picture)
        return text_drift.TextCheck(True)

    async def content_check(*_args, **_kwargs):
        return content_drift.ContentCheck(True)

    monkeypatch.setattr(content_drift, "check", content_check)
    monkeypatch.setattr(runners, "async_session", session)
    monkeypatch.setattr(runners, "bind_llm_caller", nothing)
    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(jobs, "mark", mark)
    monkeypatch.setattr(assets, "delete_files", nothing)
    monkeypatch.setattr(encoder, "extract_frame", poster)
    monkeypatch.setattr(text_drift, "check", text_check)
    return world


def _shot(**params) -> VideoClip:
    return _clip(
        id=uuid.uuid4(),
        status=JOB_PENDING,
        file_id=None,
        provider_job_id=params.pop("provider_job_id", None),
        conversation_id=params.pop("conversation_id", None),
        params={"provider_id": str(uuid.uuid4()), "duration_seconds": 8, **params},
    )


def test_the_shot_is_asked_with_the_frame_and_its_notes_say_which(worker):
    worker.clip = _shot(reference_file_id=None, reference_video=_source(at_seconds=4.5, step=2))
    outcome = asyncio.run(runners.run_clip(str(worker.clip.id)))
    assert outcome.kind == "done"
    assert worker.requests == [(b"FRAME", "image/jpeg", None)]
    ready = worker.marks[-1]
    assert ready["status"] == JOB_READY
    assert ready["params"]["notes"] == [
        'Started from the frame after step 2 (4.5 s) of the recording "OpenCRQ demo".',
        "Duration snapped to 8 s.",
    ]
    # Its text is checked against the very frame it was asked with.
    assert worker.text_checks == [(b"FRAME", "image/jpeg")]


def test_a_generation_resumed_upstream_needs_no_frame(worker):
    worker.media.gone = {TAKE_FILE}
    worker.clip = _shot(provider_job_id="operations/1", reference_video=_source())
    asyncio.run(runners.run_clip(str(worker.clip.id)))
    # The paid job is picked up where the provider is: the deleted recording does not fail it.
    assert worker.requests == [(None, None, "operations/1")]
    ready = worker.marks[-1]
    assert ready["status"] == JOB_READY
    # Only the text check looked for the frame: gone, the shot is made unchecked, and says so.
    assert worker.media.downloads == [TAKE_FILE] and worker.text_checks == []
    assert ready["params"]["notes"][-1].startswith("The text of the shot was not checked: The recording")


def _start_frames_cut(stills: list[tuple]) -> list[tuple]:
    """The stills cut for a start frame: the shot's own first, middle and last frames are not."""
    return [still for still in stills if still[1] != frame_qc.FRAME_MAX_SIDE]


def test_a_resumed_generation_checks_its_text_against_the_kept_frame(worker):
    worker.media.gone = {TAKE_FILE}
    worker.clip = _shot(provider_job_id="operations/1", reference_video=_source(frame_file_id=FRAME_FILE))
    asyncio.run(runners.run_clip(str(worker.clip.id)))
    # The frame the shot was asked with, kept: nothing cut, and the recording's deletion changes nothing.
    assert worker.text_checks == [(worker.media.kept, "image/jpeg")] and _start_frames_cut(worker.media.stills) == []
    assert worker.marks[-1]["status"] == JOB_READY


def test_a_gone_source_fails_the_shot_before_the_provider_is_asked(worker):
    worker.media.gone = {TAKE_FILE}
    worker.clip = _shot(reference_video=_source())
    asyncio.run(runners.run_clip(str(worker.clip.id)))
    assert worker.requests == []
    assert worker.marks[-1]["status"] == JOB_FAILED
    assert worker.marks[-1]["error"].startswith('The recording "OpenCRQ demo" this shot starts from is gone')


def test_the_kept_frame_is_named_on_the_shot_before_the_provider_is_asked(worker):
    worker.clip = _shot(conversation_id=CONVERSATION, reference_video=_source(at_seconds=4.5, step=2))
    asyncio.run(runners.run_clip(str(worker.clip.id)))
    kept = [m["params"]["reference_video"].get("frame_file_id") for m in worker.marks_before_provider if "params" in m]
    # A generation that fails at the provider is made again from this very frame.
    assert kept == [FRAME_FILE]
    frame = next(s for s in worker.media.stored if s["source"] == "clip-start-frame")
    assert (frame["conversation_id"], frame["user_id"], frame["data"]) == (CONVERSATION, OWNER, b"FRAME")
    ready = worker.marks[-1]
    assert ready["status"] == JOB_READY and ready["params"]["reference_video"]["frame_file_id"] == FRAME_FILE
    assert worker.requests == [(b"FRAME", "image/jpeg", None)]


def test_a_shot_generated_again_asks_the_provider_with_the_kept_frame(worker):
    worker.media.gone = {TAKE_FILE}
    worker.clip = _shot(reference_video=_source(at_seconds=4.5, frame_file_id=FRAME_FILE), attempt=1)
    asyncio.run(runners.run_clip(str(worker.clip.id)))
    assert worker.requests == [(worker.media.kept, "image/jpeg", None)]
    assert _start_frames_cut(worker.media.stills) == []
    assert not any(s["source"] == "clip-start-frame" for s in worker.media.stored)
    assert worker.marks[-1]["status"] == JOB_READY


# ── The person sees what the shot opened on ───────────────────────────────


def _ready_answer(monkeypatch, params: dict):
    """``generate_video_clip`` answering a shot made inside the call: its result and the files it attached."""
    from contextlib import asynccontextmanager

    from app.services.builtin_tools import video_studio as tool
    from app.tools.base import ToolCall

    poster = uuid.uuid4()
    clip = _clip(id=uuid.uuid4(), params=params, width=1920, height=1080, has_audio=False, poster_file_id=poster)

    class _Session:
        async def commit(self):
            return None

    @asynccontextmanager
    async def session():
        yield _Session()

    async def find_user(_db, _user_id):
        return SimpleNamespace(id=OWNER, role="user")

    async def plan(_db, _user, **_settings):
        return SimpleNamespace(label="Gemini", notes=[])

    async def queue(_db, _user, _plan, **_kwargs):
        return clip

    async def wait_for(*_args, **_kwargs):
        return [clip]

    async def nothing(*_args, **_kwargs):
        return None

    async def file_refs(file_ids, **_where):
        shown = [str(f) for f in file_ids if f]
        return tool._Pictures(refs=[SimpleNamespace(file_id=f) for f in shown], ids=shown)

    monkeypatch.setattr(tool, "async_session", session)
    monkeypatch.setattr(tool, "_user", find_user)
    monkeypatch.setattr(tool, "_file_refs", file_refs)
    monkeypatch.setattr(clips, "plan_clip", plan)
    monkeypatch.setattr(clips, "queue_clip", queue)
    monkeypatch.setattr(jobs, "wake", nothing)
    monkeypatch.setattr(jobs, "wait_for", wait_for)
    _definitions, executor = tool.build_video_studio_tools(None, str(OWNER))
    result = asyncio.run(
        executor.execute_tool(ToolCall(id="c1", name="generate_video_clip", arguments={"prompt": "It comes alive"}))
    )
    return result, [ref.file_id for ref in result.files or []], str(poster)


def test_the_tool_answer_shows_the_frame_the_shot_opened_on(monkeypatch):
    import json

    params = {"reference_video": _source(at_seconds=4.5, step=2, frame_file_id=FRAME_FILE)}
    result, attached, poster = _ready_answer(monkeypatch, params)
    head = json.loads(result.content.split("\n", 1)[0])
    assert head["status"] == "ready" and head["start_frame_file_id"] == FRAME_FILE
    assert f"The frame it opened on (attached before the shot's poster): [[FILE:{FRAME_FILE}]]." in result.content
    assert attached == [FRAME_FILE, poster]
    # An image-started shot names its image, without attaching it again.
    result, attached, poster = _ready_answer(monkeypatch, {"reference_file_id": IMAGE_FILE})
    assert json.loads(result.content.split("\n", 1)[0])["start_frame_file_id"] == IMAGE_FILE
    assert attached == [poster] and "[[FILE:" not in result.content


def test_the_editor_reads_the_start_frame_through_the_shots_media_token(monkeypatch):
    from app.api.v1 import video_studio as api
    from app.api.v1.schemas.video_studio import MediaTokenRequest

    shot = _clip(params={"reference_video": _source(at_seconds=4.5, frame_file_id=FRAME_FILE)})

    async def readable(_db, kind, asset_id, _user):
        assert (kind, asset_id) == ("clip", CLIP_ID)
        return shot

    monkeypatch.setattr(api, "_readable_asset", readable)
    body = MediaTokenRequest(kind="clip", id=CLIP_ID, asset="start_frame")
    file_id, filename = asyncio.run(api._media_file(None, body, SimpleNamespace(id=OWNER)))
    assert (file_id, filename) == (uuid.UUID(FRAME_FILE), "Slow orbit around a dark globe.jpg")
    assert jobs.serialize_clip(shot)["start_frame_file_id"] == FRAME_FILE
    # A shot that started from nothing has no first frame to show.
    shot.params = {}
    assert asyncio.run(api._media_file(None, body, SimpleNamespace(id=OWNER)))[0] is None


# ── The editor asks it like the assistant ─────────────────────────────────


def test_the_editor_starts_a_shot_from_a_recording_like_the_assistant(monkeypatch):
    from app.api.v1 import video_studio as api
    from app.api.v1.schemas.video_studio import ClipCreate

    asked: dict = {}

    async def start(_db, _user, **kwargs):
        asked.update(kwargs)
        return SimpleNamespace(id=uuid.uuid4(), model="veo-3.1", name=None, prompt="p"), "Gemini"

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(clips, "start_clip", start)
    monkeypatch.setattr(api, "log_audit", nothing)
    monkeypatch.setattr(jobs, "wake", nothing)
    monkeypatch.setattr(jobs, "serialize_clip", lambda clip: {"id": str(clip.id)})
    body = ClipCreate(prompt="The dashboard comes alive", reference_recording_id=TAKE_ID, reference_at_step=2)
    db = SimpleNamespace(commit=nothing)
    asyncio.run(api.create_clip(body, user=SimpleNamespace(id=OWNER), db=db, _ai_gate=None))
    assert (asked["reference_recording_id"], asked["reference_at_step"]) == (TAKE_ID, 2)
    assert asked["reference_clip_id"] is None and asked["reference_at_seconds"] is None
    assert asked["crop"] is None
    body = ClipCreate(prompt="Vertical", reference_recording_id=TAKE_ID, aspect_ratio="9:16", crop="center")
    asyncio.run(api.create_clip(body, user=SimpleNamespace(id=OWNER), db=db, _ai_gate=None))
    assert (asked["aspect_ratio"], asked["crop"]) == ("9:16", "center")


# ── ffmpeg ────────────────────────────────────────────────────────────────


@pytest.mark.skipif(not encoder.encoder_available(), reason="ffmpeg is not installed")
def test_a_still_is_the_frame_asked_or_the_last_and_never_larger_than_a_shot(tmp_path):
    from PIL import Image

    video = tmp_path / "recording.mp4"
    tall = tmp_path / "vertical.mp4"
    # A 1.5x recording's size: one second of red, then one of blue.
    asyncio.run(
        encoder.ffmpeg(
            [
                "-f", "lavfi", "-i", "color=c=red:s=2400x1350:d=1:r=30",
                "-f", "lavfi", "-i", "color=c=blue:s=2400x1350:d=1:r=30",
                "-filter_complex", "[0][1]concat=n=2:v=1:a=0",
                "-pix_fmt", "yuv420p", str(video),
            ]
        )
    )
    vertical_source = ["-f", "lavfi", "-i", "color=c=green:s=720x1280:d=0.5:r=30", "-pix_fmt", "yuv420p", str(tall)]
    asyncio.run(encoder.ffmpeg(vertical_source))
    early, last, small = tmp_path / "early.jpg", tmp_path / "last.jpg", tmp_path / "small.jpg"
    asyncio.run(encoder.extract_still(str(video), at=0.3, out_path=str(early), max_side=1920))
    asyncio.run(encoder.extract_still(str(video), at=None, out_path=str(last), max_side=1920))
    asyncio.run(encoder.extract_still(str(tall), at=None, out_path=str(small), max_side=1920))
    with Image.open(early) as first, Image.open(last) as final, Image.open(small) as vertical:
        assert first.size == final.size == (1920, 1080)
        assert vertical.size == (720, 1280)
        red, _green, blue = first.convert("RGB").getpixel((960, 540))
        assert red > 200 and blue < 60
        red, _green, blue = final.convert("RGB").getpixel((960, 540))
        assert blue > 200 and red < 60


@pytest.mark.skipif(not encoder.encoder_available(), reason="ffmpeg is not installed")
def test_a_cropped_still_is_the_middle_of_the_frame_at_the_shots_shape(tmp_path):
    from PIL import Image

    video = tmp_path / "wide.mp4"
    # A 16:9 recording in three bands - red, green, blue - a 9:16 shot keeps the green middle.
    bands = [arg for colour in ("red", "lime", "blue") for arg in ("-f", "lavfi", "-i", f"color=c={colour}:s=800x1350:d=0.5")]
    asyncio.run(encoder.ffmpeg([*bands, "-filter_complex", "[0][1][2]hstack=3", "-pix_fmt", "yuv420p", str(video)]))
    upright = tmp_path / "upright.jpg"
    asyncio.run(encoder.extract_still(str(video), at=0.2, out_path=str(upright), max_side=1920, crop_to=(9, 16)))
    with Image.open(upright) as still:
        assert still.size == clips.cropped_size((2400, 1350), (9, 16)) == (758, 1350)
        assert clips.fits(still.size, (9, 16))
        for x in (5, 379, 752):
            red, green, blue = still.convert("RGB").getpixel((x, 675))
            assert green > 200 and red < 60 and blue < 60
