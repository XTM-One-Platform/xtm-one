"""The frame an AI shot would start from, looked at before paying for it.

``preview_start_frame`` (the tool) and ``GET /video-studio/clips/start-frame``
take the ``reference_*`` arguments of ``generate_video_clip``, run the same
checks and cut the same frame as the worker, and save it as a file of the
conversation. A recording's frame caught while its page still loads - the
frames after it, within a second, differ from it - is flagged with the moment
the page settles.

Synthetic takes only: a red page, a blank page while the next one loads, a
blue page. The settle rule is checked on synthetic gray frames, and end to end
on a synthetic video where ffmpeg is installed.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.file_attachment import FileAttachment
from app.models.video_studio import JOB_READY, VideoClip, VideoTake
from app.services.video_studio import assets, clips, encoder

OWNER = uuid.UUID("00000000-0000-0000-0000-00000000a001")
CONVERSATION = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
TAKE_ID = "00000000-0000-0000-0000-0000000000b1"
TAKE_FILE = "00000000-0000-0000-0000-0000000000b2"
CLIP_ID = "00000000-0000-0000-0000-0000000000c1"
CLIP_FILE = "00000000-0000-0000-0000-0000000000c2"
IMAGE_FILE = "00000000-0000-0000-0000-0000000000d2"
FRAME_FILE = "00000000-0000-0000-0000-0000000000e1"

#: The synthetic take: a red page for 6 s; step 1 clicks at 5.9 s and the next
#: page loads - blank from 6 s to 7.5 s - then shows, blue, until 12 s. Step 1's
#: result is the frame at 6.6 s (half a second after it ended): mid-load.
EVENTS = [
    {"step": 0, "action": "goto", "t0": 0.0, "ta": 0.0, "t1": 1.2},
    {"step": 1, "action": "click", "label": "Risk", "t0": 5.6, "ta": 5.9, "t1": 6.1},
    {"step": 2, "action": "wait_for", "label": "Dashboard", "t0": 6.1, "ta": 7.5, "t1": 7.5},
    {"step": 3, "action": "click", "label": "Export", "t0": 10.6, "ta": 11.0, "t1": 11.2},
]
PIXELS = clips.SETTLE_SIZE[0] * clips.SETTLE_SIZE[1]


def _gray(level: int) -> bytes:
    return bytes([level]) * PIXELS


#: The pages as the settle rule sees them: red, white and blue in 8-bit luma.
RED, BLANK, BLUE = _gray(76), _gray(255), _gray(29)


def _page(t: float) -> bytes:
    return RED if t < 6.0 else BLANK if t < 7.5 else BLUE


def _frames(start: float, seconds: float, fps: int = clips.SETTLE_FPS) -> list[bytes]:
    return [_page(round(start + i / fps, 3)) for i in range(round(seconds * fps))]


# ── The settle rule ───────────────────────────────────────────────────────


def test_a_blank_page_that_fills_in_within_a_second_is_loading_until_it_settles():
    loading, settled = clips.settle(_frames(6.6, 5.0))
    # 0.9 s after 6.6 s the blue page is there, and stays.
    assert (loading, settled) == (True, 9)


def test_a_page_that_stays_is_not_loading_and_a_spinner_alone_is_not_a_load():
    assert clips.settle(_frames(1.0, 3.0)) == (False, None)
    spinning = []
    for i in range(30):
        frame = bytearray(BLANK)
        # A 10x10 spinner turning on a blank page: 100 of 14,400 pixels change.
        offset = (i % 4) * 10
        for row in range(10):
            frame[(40 + row) * 160 + 70 + offset : (40 + row) * 160 + 80 + offset] = bytes([60]) * 10
        spinning.append(bytes(frame))
    assert clips.settle(spinning) == (False, None)
    assert clips.changed(spinning[0], spinning[1]) < clips.SETTLE_CHANGE


def _flashing(count: int) -> list[bytes]:
    """A page changing every 0.2 s to the end of what is watched."""
    return [RED if (i // 2) % 2 else BLUE for i in range(count)]


def test_a_page_that_never_settles_in_what_was_watched_has_no_settled_moment():
    assert clips.settle(_flashing(40)) == (True, None)
    # Too few frames after the first to tell (the next step comes at once): not loading.
    assert clips.settle([BLANK, BLUE]) == (False, None)


def test_the_page_is_watched_until_the_next_step_acts_on_it():
    # From step 1's result: the wait_for only waits, the next click at 11.0 s acts.
    assert clips._watch_until(EVENTS, 6.6, 12.0) == 11.0
    # Before step 1's click, up to it; never past the horizon or the recording's end.
    assert clips._watch_until(EVENTS, 1.5, 12.0) == 5.9
    assert clips._watch_until([], 1.5, 12.0) == 1.5 + clips.SETTLE_HORIZON_SECONDS
    assert clips._watch_until(EVENTS, 11.5, 12.0) == 12.0
    # A skipped step did nothing to the page.
    skipped = [*EVENTS[:3], {**EVENTS[3], "ok": False}]
    assert clips._watch_until(skipped, 6.6, 12.0) == 12.0


# ── The preview ───────────────────────────────────────────────────────────


class _Db:
    def __init__(self, *rows):
        self.rows = {(type(row), str(row.id)): row for row in rows}

    async def get(self, model, key):
        return self.rows.get((model, str(key)))


def _take(**values) -> VideoTake:
    fields = {
        "id": uuid.UUID(TAKE_ID),
        "user_id": OWNER,
        "name": "OpenCRQ demo",
        "status": JOB_READY,
        "duration_ms": 12_000,
        "file_id": uuid.UUID(TAKE_FILE),
        "events": EVENTS,
        "expired_at": None,
        "viewport_width": 1600,
        "viewport_height": 900,
        "pixel_ratio": 1.0,
    }
    return VideoTake(**{**fields, **values})


@pytest.fixture
def studio(monkeypatch, tmp_path):
    """A person's recording, AI shot and image; the stored files, ffmpeg and the frames after a moment faked."""
    world = SimpleNamespace(
        user=SimpleNamespace(id=OWNER, role="user"),
        stored=[],
        watched=[],
        video=None,
        db=_Db(
            _take(),
            VideoClip(
                id=uuid.UUID(CLIP_ID),
                user_id=OWNER,
                name="Globe",
                prompt="A globe",
                status=JOB_READY,
                duration_ms=8_000,
                file_id=uuid.UUID(CLIP_FILE),
                width=1920,
                height=1080,
                expired_at=None,
            ),
            FileAttachment(id=uuid.UUID(IMAGE_FILE), filename="dashboard.png", content_type="image/png"),
        ),
    )

    async def can_read_file(_row, _user, _db):
        return True

    async def download(file_id, path):
        if world.video is not None:
            shutil.copyfile(world.video, path)
        else:
            Path(path).write_bytes(b"VIDEO")
        return SimpleNamespace(content_type="video/mp4")

    async def store_path(path, **saved):
        world.stored.append({**saved, "size": clips.image_size(Path(path))})
        return SimpleNamespace(id=uuid.UUID(FRAME_FILE))

    async def check_start_picture(_row):
        return None

    monkeypatch.setattr(clips, "check_start_picture", check_start_picture)
    monkeypatch.setattr("app.services.file_access.can_read_file", can_read_file)
    monkeypatch.setattr(assets, "download_file", download)
    monkeypatch.setattr(assets, "store_path", store_path)
    return world


@pytest.fixture
def fake_ffmpeg(studio, monkeypatch):
    """ffprobe, the still and the frames after a moment, from the synthetic take's timeline."""

    async def probe(_path):
        return SimpleNamespace(duration=12.0, width=1600, height=900, has_audio=False)

    async def extract_still(_path, *, at, out_path, max_side, crop_to=None):
        from PIL import Image

        Image.new("RGB", (1600, 900) if crop_to is None else (506, 900), (30, 30, 30)).save(out_path, "JPEG")

    async def gray_frames(_path, *, start, seconds, fps, size, out_path):
        studio.watched.append((round(start, 3), round(seconds, 3), fps, size))
        return _frames(start, seconds, fps)

    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(encoder, "extract_still", extract_still)
    monkeypatch.setattr(encoder, "gray_frames", gray_frames)
    return studio


def _preview(studio, **reference) -> clips.FramePreview:
    async def run():
        plan = await clips.plan_start_frame_preview(studio.db, studio.user, **reference)
        return await clips.preview_start_frame(plan, user_id=OWNER, conversation_id=CONVERSATION)

    return asyncio.run(run())


def test_a_step_caught_mid_load_is_flagged_with_the_moment_the_page_settles(fake_ffmpeg):
    preview = _preview(fake_ffmpeg, recording_id=TAKE_ID, at_step=1)
    assert (preview.at_seconds, preview.loading, preview.settled_at) == (6.6, True, 7.5)
    assert "page still loading at 6.6 s, settled at 7.5 s" in preview.note
    assert "reference_at_seconds=7.5 instead of reference_at_step" in preview.note
    assert preview.note.startswith('This is the frame after step 1 (6.6 s) of the recording "OpenCRQ demo", 1600x900.')
    # Watched from the frame up to the next step that acts on the page, ten frames a second.
    assert fake_ffmpeg.watched == [(6.6, 4.4, clips.SETTLE_FPS, clips.SETTLE_SIZE)]
    assert preview.as_dict() == {
        "file_id": FRAME_FILE,
        "at_seconds": 6.6,
        "width": 1600,
        "height": 900,
        "loading": {"at_seconds": 6.6, "settled_at_seconds": 7.5},
    }
    # Saved as a file of the conversation, like the frame a shot keeps.
    assert fake_ffmpeg.stored == [
        {
            "filename": "opencrq-demo-start-frame-6.6s.jpg",
            "user_id": OWNER,
            "source": "clip-start-frame",
            "content_type": "image/jpeg",
            "conversation_id": CONVERSATION,
            # What it is, so a second read in a turn answers with it.
            "description": (
                'The frame after step 1 (6.6 s) of the recording "OpenCRQ demo", 1600x900: the frame an AI shot starts from.'
            ),
            "size": (1600, 900),
        }
    ]


def test_the_settled_moment_previews_clean(fake_ffmpeg):
    preview = _preview(fake_ffmpeg, recording_id=TAKE_ID, at_seconds=7.5)
    assert (preview.loading, preview.settled_at) == (False, None) and "Careful" not in preview.note
    assert preview.as_dict()["loading"] is None
    assert preview.note.endswith("then call generate_video_clip with the same reference arguments.")


def test_a_moment_right_before_the_next_step_is_not_called_loading(fake_ffmpeg):
    # The click at 5.9 s makes the page go blank: the script's doing, not a load.
    preview = _preview(fake_ffmpeg, recording_id=TAKE_ID, at_seconds=5.5)
    assert preview.loading is False and fake_ffmpeg.watched == [(5.5, 0.4, clips.SETTLE_FPS, clips.SETTLE_SIZE)]


def test_a_page_still_changing_when_the_watch_ends_says_so(fake_ffmpeg, monkeypatch):
    async def flashing(_path, *, start, seconds, fps, size, out_path):
        return _flashing(round(seconds * fps))

    monkeypatch.setattr(encoder, "gray_frames", flashing)
    preview = _preview(fake_ffmpeg, recording_id=TAKE_ID, at_seconds=6.6)
    assert (preview.loading, preview.settled_at) == (True, None)
    assert "page still loading at 6.6 s and not settled by 11.0 s" in preview.note


def test_an_ai_shot_or_an_image_is_shown_without_a_load_check(fake_ffmpeg):
    shot = _preview(fake_ffmpeg, clip_id=CLIP_ID)
    assert shot.note.startswith('This is the last frame of the AI shot "Globe", 1600x900.')
    assert shot.at_seconds == 12.0 and shot.loading is False and fake_ffmpeg.watched == []
    image = _preview(fake_ffmpeg, file_id=IMAGE_FILE)
    assert image == clips.FramePreview(
        file_id=IMAGE_FILE, note="An image is the frame as it is: a shot starts from it unchanged."
    )
    assert len(fake_ffmpeg.stored) == 1


def test_a_preview_checks_what_a_start_checks(fake_ffmpeg):
    with pytest.raises(clips.ClipError, match="Name the image, recording, shot or video file"):
        _preview(fake_ffmpeg)
    with pytest.raises(clips.ClipError, match=r"is 1600x900, not 9:16 like the shot"):
        _preview(fake_ffmpeg, recording_id=TAKE_ID, aspect_ratio="9:16")
    with pytest.raises(clips.ClipError, match="has no step 9"):
        _preview(fake_ffmpeg, recording_id=TAKE_ID, at_step=9)
    assert fake_ffmpeg.stored == []
    # Cropped, it shows the middle the shot would start from.
    upright = _preview(fake_ffmpeg, recording_id=TAKE_ID, at_seconds=7.5, aspect_ratio="9:16", crop="center")
    assert upright.size == (506, 900) and "its middle cut to the shot's shape, 506x900." in upright.note


# ── The tool and the route ────────────────────────────────────────────────


def test_the_tool_shows_the_frame_and_the_note(fake_ffmpeg, monkeypatch):
    from contextlib import asynccontextmanager

    from app.services.builtin_tools import video_studio as tool
    from app.tools.base import ToolCall

    @asynccontextmanager
    async def session():
        yield fake_ffmpeg.db

    async def find_user(_db, _user_id):
        return fake_ffmpeg.user

    async def file_refs(file_ids, **_where):
        shown = [str(f) for f in file_ids if f]
        return tool._Pictures(refs=[SimpleNamespace(file_id=f) for f in shown], ids=shown)

    monkeypatch.setattr(tool, "async_session", session)
    monkeypatch.setattr(tool, "_user", find_user)
    monkeypatch.setattr(tool, "_file_refs", file_refs)
    definitions, executor = tool.build_video_studio_tools(None, str(OWNER), str(CONVERSATION))
    arguments = {"reference_recording_id": TAKE_ID, "reference_at_step": 1}
    result = asyncio.run(executor.execute_tool(ToolCall(id="p1", name="preview_start_frame", arguments=arguments)))
    head = json.loads(result.content.split("\n", 1)[0])
    assert not result.is_error and head["loading"] == {"at_seconds": 6.6, "settled_at_seconds": 7.5}
    assert f"[[FILE:{FRAME_FILE}]]" in result.content and [f.file_id for f in result.files] == [FRAME_FILE]
    assert fake_ffmpeg.stored[0]["conversation_id"] == CONVERSATION
    refused = asyncio.run(executor.execute_tool(ToolCall(id="p2", name="preview_start_frame", arguments={})))
    assert refused.is_error and "Name the image, recording, shot or video file" in refused.content
    # The same first-frame arguments as generate_video_clip, by construction (reference images and how they are
    # fitted to the model, are not a frame).
    schemas = {d.name: d.input_schema["properties"] for d in definitions}
    not_a_frame = {"reference_image_file_ids", "reference_fit"}
    shared = {k for k in schemas["generate_video_clip"] if k.startswith("reference_") and k not in not_a_frame} | {"crop"}
    assert shared <= set(schemas["preview_start_frame"])


def test_the_route_is_matched_before_a_shot_id_and_answers_the_frame(fake_ffmpeg, monkeypatch):
    from app.api.v1 import video_studio as api
    from app.services.video_studio import media

    paths = [(r.path, sorted(r.methods)) for r in api.router.routes]
    assert paths.index(("/clips/start-frame", ["GET"])) < paths.index(("/clips/{clip_id}", ["GET"]))

    committed = []

    async def commit():
        committed.append(True)

    fake_ffmpeg.db.commit = commit
    monkeypatch.setattr(media, "mint_media_token", lambda file_id, user_id, **_kw: (f"tok-{file_id}", 0))
    answer = asyncio.run(
        api.preview_clip_start_frame(
            reference_file_id=None,
            reference_recording_id=TAKE_ID,
            reference_clip_id=None,
            reference_at_seconds=None,
            reference_at_step=1,
            aspect_ratio="16:9",
            crop=None,
            user=fake_ffmpeg.user,
            db=fake_ffmpeg.db,
        )
    )
    assert committed == [True]
    assert answer.url.endswith(f"/api/v1/video-studio/media/tok-{FRAME_FILE}")
    assert (answer.file_id, answer.loading.settled_at_seconds) == (FRAME_FILE, 7.5)
    assert "page still loading at 6.6 s, settled at 7.5 s" in answer.note
    # A refusal is the start's own reason.
    with pytest.raises(api.HTTPException) as refused:
        asyncio.run(
            api.preview_clip_start_frame(
                reference_file_id=None,
                reference_recording_id=TAKE_ID,
                reference_clip_id=None,
                reference_at_seconds=None,
                reference_at_step=9,
                aspect_ratio="16:9",
                crop=None,
                user=fake_ffmpeg.user,
                db=fake_ffmpeg.db,
            )
        )
    assert refused.value.status_code == 422 and "has no step 9" in refused.value.detail


# ── On a synthetic take, with ffmpeg ──────────────────────────────────────


@pytest.mark.skipif(not encoder.encoder_available(), reason="ffmpeg is not installed")
def test_a_synthetic_take_flags_the_blank_between_two_pages(studio, tmp_path):
    take = tmp_path / "take.mp4"
    pages = [("red", 6.0), ("white", 1.5), ("blue", 4.5)]
    inputs = [
        arg
        for colour, seconds in pages
        for arg in ("-f", "lavfi", "-i", f"color=c={colour}:s=320x180:d={seconds}:r=30")
    ]
    asyncio.run(
        encoder.ffmpeg([*inputs, "-filter_complex", "[0][1][2]concat=n=3:v=1:a=0", "-pix_fmt", "yuv420p", str(take)])
    )
    studio.video = take
    studio.db = _Db(_take(viewport_width=320, viewport_height=180))

    mid_load = _preview(studio, recording_id=TAKE_ID, at_step=1)
    assert mid_load.loading and mid_load.settled_at in (7.5, 7.6)
    assert f"page still loading at 6.6 s, settled at {mid_load.settled_at:.1f} s" in mid_load.note
    settled = _preview(studio, recording_id=TAKE_ID, at_seconds=mid_load.settled_at)
    assert settled.loading is False
    steady = _preview(studio, recording_id=TAKE_ID, at_seconds=3.0)
    assert steady.loading is False and steady.size == (320, 180)
