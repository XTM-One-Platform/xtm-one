"""What producing the Filigran brand film found missing in the Video Studio.

* a frame of a recording, a shot or a video file kept as a picture
  (``save_video_frame``); a slide drawn without the deck's chrome;
* AI shots: a start picture every provider reads (SVG rasterised, any other
  type re-encoded, an unreadable one refused at save), silent shots, the text
  of a shot checked against its start picture ("text drift"), a ready shot
  generated again in place;
* editing: a field of a one-of group replaces its siblings; the transition
  planned into each scene listed; a ``classic`` pace (``test_video_studio_transitions``);
* jobs: renders queue with a per-person bound, ``get_video_jobs`` without ids
  lists what is in flight, ``cancel_video_job``;
* every picture a tool shows is a file of the conversation;
* a voice style the speech model ignores is said at save, or spoken by a
  model of the same provider that takes it;
* an optional step waits 3 s and the wait of a skipped one is cut.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.llm.video import VideoRequest, VideoResult
from app.models.video_studio import JOB_FAILED, JOB_PENDING, JOB_READY, JOB_RUNNING, VideoClip
from app.services.video_studio import (
    assets,
    clips,
    content_drift,
    encoder,
    jobs,
    projects,
    runners,
    text_drift,
    timeline,
    voice,
)
from app.services.video_studio.spec import TakeStep, VideoSpecError, parse_video_spec, validate_video_spec
from app.tools.base import ToolCall

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = BACKEND_ROOT.parent / "frontend"
USER_ID = uuid.UUID("00000000-0000-0000-0000-00000000e001")
CONVERSATION = uuid.UUID("00000000-0000-0000-0000-00000000e002")
TAKE_ID = "00000000-0000-0000-0000-00000000e003"
CLIP_ID = "00000000-0000-0000-0000-00000000e004"
FILE_ID = "00000000-0000-0000-0000-00000000e005"


def _picture(fmt: str, size: tuple[int, int] = (64, 36), mode: str = "RGB") -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new(mode, size, (20, 120, 220, 128)[: len(mode)]).save(out, format=fmt)
    return out.getvalue()


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=USER_ID, role="user", email="maker@example.com")


@asynccontextmanager
async def _session_of(db):
    yield db


# ── 5 and 6: a start picture every provider reads ─────────────────────────


def test_a_png_or_a_jpeg_start_picture_goes_as_it_is():
    png, jpeg = _picture("PNG"), _picture("JPEG")
    assert asyncio.run(clips.start_picture(png, "image/png")) == clips.StartFrame(png, "image/png")
    assert asyncio.run(clips.start_picture(jpeg, "image/jpeg")) == clips.StartFrame(jpeg, "image/jpeg")


def test_any_other_picture_is_re_encoded_as_png_and_a_large_one_bounded():
    from PIL import Image

    webp = asyncio.run(clips.start_picture(_picture("WEBP", mode="RGBA"), "image/webp"))
    assert webp.mime == "image/png" and webp.data.startswith(b"\x89PNG")
    with Image.open(io.BytesIO(webp.data)) as decoded:
        assert decoded.mode == "RGBA" and decoded.size == (64, 36)
    # A type declared wrong is read from its bytes all the same.
    gif = asyncio.run(clips.start_picture(_picture("GIF"), "application/octet-stream"))
    assert gif.mime == "image/png"
    huge = asyncio.run(clips.start_picture(_picture("JPEG", size=(4000, 2250)), "image/jpeg"))
    with Image.open(io.BytesIO(huge.data)) as decoded:
        assert max(decoded.size) == clips.START_FRAME_MAX_SIDE


def test_an_svg_start_picture_is_drawn_by_the_bounded_rasteriser(monkeypatch):
    """The template assets that failed the brand film's shots: the G2 visual, the product mockup, the logo."""
    from PIL import Image

    from app.services import svg_raster

    asked: list[dict] = []

    async def rasterize(data, **options):
        asked.append(options)
        return svg_raster.rasterize_svg(data, **{k: v for k, v in options.items() if k != "timeout"})

    monkeypatch.setattr(svg_raster, "rasterize_svg_async", rasterize)
    svg = (BACKEND_ROOT / "app/agents/seed_data/brand_assets/filigran/g2-achievements-2025.svg").read_bytes()
    frame = asyncio.run(clips.start_picture(svg, "image/svg+xml"))
    assert frame.mime == "image/png" and asked[0]["box"] == clips.START_FRAME_MAX_SIDE
    with Image.open(io.BytesIO(frame.data)) as decoded:
        assert decoded.size == (1920, 1080)
    # Declared as anything, an SVG is still read as one.
    badge = '<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="10" height="20"><rect width="10" height="20"/></svg>'
    tall = asyncio.run(clips.start_picture(badge.encode(), "application/octet-stream"))
    with Image.open(io.BytesIO(tall.data)) as decoded:
        assert decoded.size == (960, 1920)


def test_bytes_that_are_no_picture_are_refused():
    with pytest.raises(clips.StartPictureError, match="could not be read as an image"):
        asyncio.run(clips.start_picture(b"not a picture at all", "image/webp"))
    with pytest.raises(clips.StartPictureError, match="empty"):
        asyncio.run(clips.start_picture(b"", "image/png"))


def test_a_start_picture_is_read_before_anything_is_paid_for(monkeypatch):
    from app.services import file_storage as storage

    async def download(_name):
        return b"<html>not an image</html>"

    monkeypatch.setattr(storage.file_storage, "download", download)
    row = SimpleNamespace(filename="g2-badges.webp", content_type="image/webp", size=1200, s3_object_name="x")
    with pytest.raises(clips.StartPictureError, match=r"`g2-badges\.webp`: The start picture could not be read"):
        asyncio.run(clips.check_start_picture(row))
    too_big = SimpleNamespace(**{**vars(row), "size": clips.MAX_START_PICTURE_BYTES + 1})
    with pytest.raises(clips.StartPictureError, match="too large"):
        asyncio.run(clips.check_start_picture(too_big))


def test_an_unreadable_picture_fails_the_save_and_an_animation_is_asked_silent(monkeypatch):
    asked: list[dict] = []
    outcome: dict = {}

    async def start_clip(_db, _user, **settings):
        asked.append(settings)
        if outcome.get("refusal"):
            raise outcome["refusal"]
        return SimpleNamespace(id=uuid.uuid4()), "Gemini"

    monkeypatch.setattr(clips, "start_clip", start_clip)
    authored = {
        "title": "Brand film",
        "scenes": [{"id": "badges", "type": "image", "file_id": FILE_ID, "animate": {}}],
    }
    started, notes = asyncio.run(projects.start_animations(None, _user(), authored, authored))
    assert started == 1 and notes == [] and asked[0]["sound"] is False
    # A picture no provider can read: the save fails, naming the scene - never a still played quietly at render.
    outcome["refusal"] = clips.StartPictureError("The start picture could not be read as an image (UnidentifiedImageError).")
    authored["scenes"][0]["animate"] = {}
    with pytest.raises(VideoSpecError) as refused:
        asyncio.run(projects.start_animations(None, _user(), authored, authored))
    assert refused.value.problems[0].path == "scenes.0.file_id"
    assert "badges cannot be animated" in refused.value.problems[0].message
    # No video model at all: the scene is its still, and the save says so.
    outcome["refusal"] = clips.ClipError("No video model is available to you.", about_model=True)
    authored["scenes"][0]["animate"] = {}
    _started, notes = asyncio.run(projects.start_animations(None, _user(), authored, authored))
    assert notes == ["Scene badges plays as a still: No video model is available to you."]


# ── 13: silent shots ──────────────────────────────────────────────────────


def test_a_silent_shot_is_asked_silent_where_the_provider_can_be_told():
    from app.llm.kling_provider import KlingProvider
    from app.llm.runway_provider import RunwayProvider
    from app.llm.seedance_provider import SeedanceProvider

    for sound, kling_on, seedance, runway in ((None, True, True, True), (False, False, False, False)):
        _endpoint, kling = KlingProvider._video_body(VideoRequest(model="kling-v3", prompt="x", sound=sound), [])
        assert ("sound" in kling) is kling_on
        body = SeedanceProvider._video_body(VideoRequest(model="seedance-1-5-pro-251215", prompt="x", sound=sound), [])
        assert body["generate_audio"] is seedance
        _path, runway_body = RunwayProvider._video_body(VideoRequest(model="veo3.1", prompt="x", sound=sound), [])
        assert runway_body["audio"] is runway
    assert clips.build_request(model="veo", prompt="x", sound=False).sound is False


# ── 3, 4, 13: what the worker does once a shot is made ─────────────────────


@pytest.fixture
def worker(monkeypatch):
    """``runners.run_clip`` on one shot row: the provider, the storage and ffmpeg as the worker reaches them."""
    world = SimpleNamespace(
        clip=None, marks=[], deleted=[], silenced=[], has_audio=False, verdict=text_drift.TextCheck(True)
    )

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
            world.request = request
            return VideoResult(data=b"MP4", job_id="operations/1", notes=[])

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        return _Provider(), model, SimpleNamespace(id=provider_id, provider="gemini")

    async def mark(_kind, _job_id, **values):
        world.marks.append(values)

    async def store_path(_path, **_kwargs):
        return SimpleNamespace(id=uuid.uuid4(), size=3)

    async def download(_file_id, path):
        Path(path).write_bytes(_picture("PNG"))
        return SimpleNamespace(content_type="image/png")

    async def probe(_path):
        return SimpleNamespace(duration=8.0, width=1920, height=1080, has_audio=world.has_audio)

    async def poster(_path, *, at, out_path, width):
        Path(out_path).write_bytes(b"JPG")

    async def without_sound(source, out):
        world.silenced.append(source)
        world.has_audio = False
        Path(out).write_bytes(b"MP4-SILENT")

    async def delete_files(ids):
        world.deleted.extend(str(i) for i in ids)

    async def check(_user_id, picture, _path, _workdir, **_options):
        world.checked = picture
        return world.verdict

    async def nothing(*_args, **_kwargs):
        return None

    async def content_check(*_args, **_kwargs):
        return content_drift.ContentCheck(True)

    monkeypatch.setattr(content_drift, "check", content_check)
    monkeypatch.setattr(runners, "async_session", session)
    monkeypatch.setattr(runners, "bind_llm_caller", nothing)
    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(jobs, "mark", mark)
    monkeypatch.setattr(assets, "store_path", store_path)
    monkeypatch.setattr(assets, "download_file", download)
    monkeypatch.setattr(assets, "delete_files", delete_files)
    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(encoder, "extract_frame", poster)
    monkeypatch.setattr(encoder, "without_sound", without_sound)
    monkeypatch.setattr(text_drift, "check", check)
    return world


def _shot(**params) -> VideoClip:
    return VideoClip(
        id=uuid.uuid4(),
        user_id=USER_ID,
        provider="gemini",
        model="veo-3.1",
        prompt="The badges catch the light",
        status=JOB_PENDING,
        file_id=None,
        provider_job_id=None,
        params={"provider_id": str(uuid.uuid4()), "duration_seconds": 8, **params},
    )


def test_a_shot_whose_text_drifted_fails_and_keeps_its_video(worker):
    worker.verdict = text_drift.TextCheck(True, [text_drift.Drift("Orchestration", "Orcheatration", "last_frame")])
    old_video, old_poster = str(uuid.uuid4()), str(uuid.uuid4())
    worker.clip = _shot(reference_file_id=FILE_ID, replaced_media=[old_video, old_poster])
    asyncio.run(runners.run_clip(str(worker.clip.id)))
    failed = worker.marks[-1]
    assert failed["status"] == JOB_FAILED and failed["error"].startswith("text drift:")
    assert '"Orchestration" reads "Orcheatration" (last frame)' in failed["error"]
    # The video is the person's to see, and generating the shot again replaces it.
    assert failed["file_id"] and failed["poster_file_id"]
    # Checked against the very picture the provider got.
    assert worker.checked == (worker.request.reference_image, "image/png")
    # The previous take of a shot generated again goes with this generation, whichever way it ended.
    assert worker.deleted == [old_video, old_poster]
    assert clips.REPLACED_MEDIA not in failed["params"]


def test_a_shot_whose_text_holds_is_ready_and_one_not_checked_says_so(worker):
    worker.clip = _shot(reference_file_id=FILE_ID)
    asyncio.run(runners.run_clip(str(worker.clip.id)))
    assert worker.marks[-1]["status"] == JOB_READY
    worker.verdict = text_drift.TextCheck(False, note="The text of the shot was not checked: no chat model.")
    worker.marks.clear()
    worker.clip = _shot(reference_file_id=FILE_ID)
    asyncio.run(runners.run_clip(str(worker.clip.id)))
    ready = worker.marks[-1]
    assert ready["status"] == JOB_READY and ready["params"]["notes"] == ["The text of the shot was not checked: no chat model."]
    # A shot meant to change its words is not checked; one made from a prompt alone has nothing to check against.
    for params in ({"reference_file_id": FILE_ID, "text_check": False}, {}):
        worker.checked = None
        worker.clip = _shot(**params)
        asyncio.run(runners.run_clip(str(worker.clip.id)))
        assert worker.checked is None and worker.marks[-1]["status"] == JOB_READY


def test_a_silent_shot_loses_the_track_the_model_made_anyway(worker):
    worker.has_audio = True
    worker.clip = _shot(sound=False)
    asyncio.run(runners.run_clip(str(worker.clip.id)))
    assert worker.request.sound is False and len(worker.silenced) == 1
    ready = worker.marks[-1]
    assert ready["status"] == JOB_READY and ready["has_audio"] is False
    assert ready["params"]["notes"] == ["The shot was asked silent: the sound track the model made anyway was removed."]
    # A shot left to the model keeps whatever sound it made.
    worker.has_audio, worker.silenced = True, []
    worker.clip = _shot()
    asyncio.run(runners.run_clip(str(worker.clip.id)))
    assert worker.request.sound is None and worker.silenced == [] and worker.marks[-1]["has_audio"] is True


def test_a_ready_shot_generated_again_loses_its_previous_take_once_the_new_one_lands(worker):
    old = [str(uuid.uuid4()), str(uuid.uuid4())]
    worker.clip = _shot(replaced_media=old)
    asyncio.run(runners.run_clip(str(worker.clip.id)))
    assert worker.deleted == old
    stored = next(mark for mark in worker.marks if mark.get("stage") == "Saving the shot")
    assert clips.REPLACED_MEDIA not in stored["params"]
    assert worker.marks[-1]["status"] == JOB_READY


def test_the_retention_deletes_a_previous_take_a_dead_run_left_behind():
    from app.services.video_studio import retention

    clip = VideoClip(file_id=uuid.uuid4(), params={"replaced_media": ["a", "b"]})
    assert retention._owned_files(clip)[1:] == ["a", "b"]


# ── 3: reading the text ───────────────────────────────────────────────────


def test_a_drift_is_a_word_of_the_picture_drawn_wrong():
    picture = "XTM One Agentic Orchestration. Threat Intelligence for every team."
    drifts = text_drift.drifted_words(
        picture,
        {
            "first_frame": "XTM One Agentic Orchestration Threat Intelligence",
            "last_frame": "XTM One Agentics Orcheatration Snteligance Orchestrat every team Leader",
        },
    )
    assert {(d.expected, d.seen, d.frame) for d in drifts} == {
        ("orchestration", "Orcheatration", "last_frame"),
        ("intelligence", "Snteligance", "last_frame"),
    }
    # A word missing from a frame, cut by its edge ("Orchestrat"), run on ("Agentics") or new ("Leader") is no drift.
    assert text_drift.drifted_words("No text worth checking: OK", {"last_frame": "Nothing"}) == []
    assert text_drift.drifted_words("", {"last_frame": "Orcheatration"}) == []


def test_the_check_reads_the_three_pictures_in_one_call(monkeypatch, tmp_path):
    from app.core import database
    from app.llm import router

    calls: list = []
    answer = {
        "content": '```json\n{"start_picture": "Agentic Orchestration", "first_frame": "Agentic Orchestration", '
        '"last_frame": "Agentic Orcheatration"}\n```'
    }

    class _Llm:
        async def chat(self, messages, **options):
            calls.append((messages, options))
            return SimpleNamespace(content=answer["content"])

    async def llm_for_user(_db, user_id, *_args, **_kwargs):
        assert user_id == str(USER_ID)
        return _Llm(), "a-vision-model"

    async def extract_still(_video, *, at, out_path, max_side, crop_to=None):
        Path(out_path).write_bytes(f"frame at {at}".encode())

    monkeypatch.setattr(router, "get_llm_for_user", llm_for_user)
    monkeypatch.setattr(database, "async_session", lambda: _session_of(None))
    monkeypatch.setattr(encoder, "extract_still", extract_still)
    verdict = asyncio.run(text_drift.check(USER_ID, (b"PNG", "image/png"), "clip.mp4", str(tmp_path), duration=8.0))
    assert verdict.checked and [(d.expected, d.seen) for d in verdict.drifts] == [("orchestration", "Orcheatration")]
    messages, options = calls[0]
    assert len(calls) == 1 and options["model"] == "a-vision-model" and options["temperature"] == 0.0
    # The start picture, then the first, the middle (half the duration) and the last frame (dev:invented-text-check).
    assert [f.inline_data for f in messages[0].files] == [b"PNG", b"frame at 0.0", b"frame at 4.0", b"frame at None"]
    assert "never correct" in messages[0].content
    # An answer that is no reading leaves the shot made, unchecked, and says so.
    answer["content"] = "I cannot read these images."
    verdict = asyncio.run(text_drift.check(USER_ID, (b"PNG", "image/png"), "clip.mp4", str(tmp_path)))
    assert not verdict.checked and verdict.drifts == [] and "not checked" in verdict.note


# ── 1: a frame kept as a picture ──────────────────────────────────────────


def test_a_frame_of_a_recording_is_saved_as_a_picture_of_the_conversation(monkeypatch):
    from app.services.builtin_tools import _shared
    from app.services.builtin_tools import video_studio as tool

    user = _user()
    source = {"kind": "recording", "id": TAKE_ID, "file_id": FILE_ID, "name": "openaev-live-take.mp4", "at_seconds": 12.5, "step": 4}
    persisted: list[dict] = []

    async def resolve_frame(_db, _user, **asked):
        assert asked == {"recording_id": TAKE_ID, "clip_id": None, "file_id": None, "at_seconds": None, "at_step": 4}
        return source

    async def frame_picture(got, _workdir):
        assert got is source
        return b"PNG-FRAME"

    async def persist(**kwargs):
        persisted.append(kwargs)
        return SimpleNamespace(
            id=uuid.UUID(FILE_ID), filename=kwargs["filename"], content_type="image/png", size=9, s3_object_name="o"
        )

    async def find_user(_db, _user_id):
        return user

    monkeypatch.setattr(clips, "resolve_frame", resolve_frame)
    monkeypatch.setattr(clips, "frame_picture", frame_picture)
    monkeypatch.setattr(_shared, "_persist_generated_file", persist)
    monkeypatch.setattr(tool, "async_session", lambda: _session_of(None))
    monkeypatch.setattr(tool, "_user", find_user)
    _definitions, executor = tool.build_video_studio_tools(None, str(user.id), str(CONVERSATION))
    result = asyncio.run(
        executor.execute_tool(ToolCall(id="f1", name="save_video_frame", arguments={"recording_id": TAKE_ID, "at_step": 4}))
    )
    assert not result.is_error and persisted[0]["conversation_id"] == str(CONVERSATION)
    assert persisted[0]["raw_bytes"] == b"PNG-FRAME" and persisted[0]["filename"] == "openaev-live-take - step 4.png"
    assert 'the frame after step 4 (12.5 s) of the recording "openaev-live-take.mp4"' in result.content
    assert f'{{"type": "image", "file_id": "{FILE_ID}"}}' in result.content and "reference_file_id" in result.content
    assert [ref.file_id for ref in result.files] == [FILE_ID]


def test_a_picture_file_is_already_a_picture(monkeypatch):
    from app.models.file_attachment import FileAttachment

    class _Db:
        async def get(self, _model, _key):
            return FileAttachment(id=uuid.UUID(FILE_ID), filename="badge.png", content_type="image/png")

    with pytest.raises(clips.ClipError, match="already a picture"):
        asyncio.run(clips.resolve_frame(_Db(), _user(), file_id=FILE_ID))


def test_the_frame_kept_is_large_enough_for_a_4k_render(monkeypatch, tmp_path):
    asked: list = []

    async def download(_file_id, path):
        Path(path).write_bytes(b"VIDEO")
        return SimpleNamespace(content_type="video/mp4")

    async def probe(_path):
        return SimpleNamespace(duration=30.0)

    async def extract_still(_video, *, at, out_path, max_side, crop_to=None):
        asked.append((at, max_side, Path(out_path).suffix))
        Path(out_path).write_bytes(b"PNG")

    monkeypatch.setattr(assets, "download_file", download)
    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(encoder, "extract_still", extract_still)
    source = {"kind": "shot", "id": CLIP_ID, "file_id": FILE_ID, "name": "Globe", "at_seconds": 3.0}
    assert asyncio.run(clips.frame_picture(source, str(tmp_path))) == b"PNG"
    assert asked == [(3.0, clips.FRAME_PICTURE_MAX_SIDE, ".png")]
    assert clips.frame_label(source) == 'the frame at 3.0 s of the AI shot "Globe"'


# ── 11: a picture a tool shows is a file of the conversation ──────────────


def test_a_picture_a_tool_shows_is_a_file_of_its_conversation(monkeypatch):
    from app.models.file_attachment import FileAttachment
    from app.services import file_storage as storage
    from app.services.builtin_tools import _shared
    from app.services.builtin_tools import video_studio as tool

    sheet = FileAttachment(
        id=uuid.uuid4(),
        filename="take-contact-sheet.png",
        content_type="image/png",
        size=4,
        s3_object_name="sheet",
        description="The contact sheet of the recording: one frame per step.",
    )
    own = FileAttachment(
        id=uuid.uuid4(),
        filename="voice.wav",
        content_type="audio/wav",
        size=4,
        s3_object_name="voice",
        conversation_id=CONVERSATION,
    )
    copies: list[dict] = []

    class _Db:
        async def execute(self, _statement):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [sheet, own]))

    async def download(name):
        return f"bytes of {name}".encode()

    async def persist(**kwargs):
        copies.append(kwargs)
        return FileAttachment(
            id=uuid.uuid4(),
            filename=kwargs["filename"],
            content_type=kwargs["content_type"],
            size=len(kwargs["raw_bytes"]),
            s3_object_name="copy",
            conversation_id=CONVERSATION,
            description=kwargs.get("description"),
        )

    async def no_copy_yet(_conversation_id, _digest):
        return None

    monkeypatch.setattr(tool, "async_session", lambda: _session_of(_Db()))
    monkeypatch.setattr(tool, "_conversation_copy", no_copy_yet)
    monkeypatch.setattr(storage.file_storage, "download", download)
    monkeypatch.setattr(_shared, "_persist_generated_file", persist)
    refs = asyncio.run(tool._file_refs([sheet.id, own.id], conversation_id=CONVERSATION, user_id=str(USER_ID))).refs
    # The contact sheet stays the recording's; the conversation gets its own copy (one per bytes per conversation).
    assert copies == [
        {
            "user_id": str(USER_ID),
            "filename": "take-contact-sheet.png",
            "raw_bytes": b"bytes of sheet",
            "content_type": "image/png",
            "conversation_id": str(CONVERSATION),
            "assignment_run_id": None,
            "source": "agent_check",
            "description": "The contact sheet of the recording: one frame per step.",
        }
    ]
    assert refs[0].file_id != str(sheet.id) and refs[1].file_id == str(own.id)
    # The copy keeps what the picture is: a second read in the turn answers with it, never a refusal.
    assert refs[0].notes == "The contact sheet of the recording: one frame per step."
    # Outside a conversation the media's own picture is shown.
    copies.clear()
    refs = asyncio.run(tool._file_refs([sheet.id], conversation_id=None, user_id=str(USER_ID))).refs
    assert copies == [] and refs[0].file_id == str(sheet.id)


# ── 7: one source of an object at a time ──────────────────────────────────


def test_setting_one_source_of_an_object_drops_its_siblings():
    clip_a, clip_b, file_a = (str(uuid.uuid4()) for _ in range(3))
    spec = {
        "title": "Brand film",
        "scenes": [
            {"id": "s1", "type": "statement", "lines": ["We see it all."], "backdrop": {"file_id": file_a, "trim_start": 2}},
            {"id": "c1", "type": "clip", "file_id": file_a},
            {"id": "d1", "type": "slide", "presentation_id": str(uuid.uuid4()), "slide": 3},
        ],
        "music": {"track": "bright-horizon", "volume": 0.4},
    }
    edited, _lines = projects.apply_operations(
        spec,
        [
            {"op": "update_scene", "scene": "s1", "fields": {"backdrop": {"clip_id": clip_a}}},
            {"op": "update_scene", "scene": "c1", "fields": {"clip_id": clip_b}},
            {"op": "update_scene", "scene": "d1", "fields": {"slide_id": "slide-architecture"}},
            {"op": "update", "fields": {"music": {"music_id": clip_a}}},
        ],
    )
    scenes = {scene["id"]: scene for scene in edited["scenes"]}
    assert scenes["s1"]["backdrop"] == {"clip_id": clip_a, "trim_start": 2}
    assert scenes["c1"].get("file_id") is None and scenes["c1"]["clip_id"] == clip_b
    assert "slide" not in scenes["d1"] and scenes["d1"]["slide_id"] == "slide-architecture"
    assert edited["music"] == {"music_id": clip_a, "volume": 0.4}
    validate_video_spec(edited)
    # Both set in one edit is the author's contradiction: the save names it.
    both, _ = projects.apply_operations(
        spec, [{"op": "update_scene", "scene": "s1", "fields": {"backdrop": {"clip_id": clip_a, "file_id": file_a}}}]
    )
    with pytest.raises(VideoSpecError, match=r"exactly one source: clip_id \(an AI shot\), file_id"):
        validate_video_spec(both)


# ── 10: the transitions, before the render ────────────────────────────────


def test_the_transition_into_each_scene_is_listed_before_the_render():
    effective = validate_video_spec(
        {
            "title": "Brand film",
            "default_transition": {"type": "auto", "pace": "classic"},
            "scenes": [
                {"id": "open", "type": "title", "title": "Filigran", "duration": 3},
                {"id": "words", "type": "statement", "lines": ["We see it all."], "duration": 4},
                {"id": "cut", "type": "statement", "lines": ["Now."], "duration": 3, "transition": {"type": "cut"}},
                {"id": "end", "type": "outro", "title": "Thanks", "duration": 4},
            ],
        }
    )
    line = asyncio.run(projects.transitions_line(None, effective))
    assert line == (
        "Transitions into each scene, as planned now: words: crossfade 0.70 s (auto, classic); cut: cut; "
        "end: fade_black 0.80 s (auto, classic)."
    )


# ── 8, 9: the render queue and the jobs in flight ─────────────────────────


def _tool_with(monkeypatch, db, conversation: uuid.UUID | None = None):
    from app.services.builtin_tools import video_studio as tool

    async def find_user(_db, _user_id):
        return _user()

    monkeypatch.setattr(tool, "async_session", lambda: _session_of(db))
    monkeypatch.setattr(tool, "_user", find_user)
    _definitions, executor = tool.build_video_studio_tools(None, str(USER_ID), str(conversation) if conversation else None)
    return executor


def test_a_render_past_the_persons_limit_lists_the_renders_in_flight(monkeypatch):
    from app.models.video_studio import VideoRender

    project = SimpleNamespace(id=uuid.uuid4(), title="Brand film", revision=3)
    in_flight = [
        (VideoRender(id=uuid.uuid4(), project_id=uuid.uuid4(), status=JOB_RUNNING, progress=40, stage="Drawing frames"), "Launch film"),
        (VideoRender(id=uuid.uuid4(), project_id=uuid.uuid4(), status=JOB_PENDING, progress=0, stage="Queued"), "Event loop"),
    ]

    async def visible(*_args, **_kwargs):
        return project

    async def manages(*_args, **_kwargs):
        return True

    async def renders(_db, user_id):
        assert user_id == USER_ID
        return in_flight

    async def states(kind, rows):
        return [jobs.job_state(kind, row) for row in rows]

    async def open_gate(*_args, **_kwargs):
        return []

    async def effective(*_args, **_kwargs):
        return SimpleNamespace(spec={"scenes": []})

    from app.services.video_studio import render_checks

    monkeypatch.setattr(projects, "load_visible_project", visible)
    monkeypatch.setattr(projects, "can_manage_project", manages)
    monkeypatch.setattr(projects, "effective_spec", effective)
    monkeypatch.setattr(render_checks, "gate", open_gate)
    monkeypatch.setattr(jobs, "renders_in_flight", renders)
    monkeypatch.setattr(jobs, "job_states", states)
    monkeypatch.setattr(jobs, "max_renders_per_user", lambda: 2)

    class _Db:
        # The start's locks (the video's, the person's), then the video's own renders in progress: none.
        async def execute(self, _statement, _params=None):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    executor = _tool_with(monkeypatch, _Db())
    # Past the quality gate (tested on its own): the limit is what this render meets.
    asked = {"video_id": str(project.id), "force": True}
    result = asyncio.run(executor.execute_tool(ToolCall(id="r1", name="render_video", arguments=asked)))
    assert result.is_error and "You already have 2 renders queued or running (at most 2 at once)" in result.content
    assert "of **Launch film**" in result.content and "Drawing frames (40%)" in result.content
    assert "of **Event loop**" in result.content and "cancel_video_job(render_ids=[...])" in result.content


def test_the_bound_is_the_configured_one(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "video_studio_max_renders_per_user", 0)
    assert jobs.max_renders_per_user() == 1
    monkeypatch.setattr(settings, "video_studio_max_renders_per_user", 12)
    assert jobs.max_renders_per_user() == 12


def test_get_video_jobs_without_ids_lists_what_is_in_flight_at_once(monkeypatch):
    shot = VideoClip(id=uuid.UUID(CLIP_ID), user_id=USER_ID, status=JOB_RUNNING, progress=30, stage="Generating at the provider")
    waits: list[float] = []

    async def in_flight(user_id):
        assert user_id == USER_ID
        return {"clip": [CLIP_ID]}

    async def wait_for_jobs(asked, *, timeout, told_to=None):
        waits.append(timeout)
        return {"clip": [shot] if asked.get("clip") else []}, jobs.WAIT_BUDGET

    async def states(kind, rows):
        return [jobs.job_state(kind, row) for row in rows]

    monkeypatch.setattr(jobs, "jobs_in_flight", in_flight)
    monkeypatch.setattr(jobs, "wait_for_jobs", wait_for_jobs)
    monkeypatch.setattr(jobs, "job_states", states)
    executor = _tool_with(monkeypatch, SimpleNamespace())
    result = asyncio.run(executor.execute_tool(ToolCall(id="j1", name="get_video_jobs", arguments={})))
    assert not result.is_error and waits == [0, 0.0]
    assert '"returned_because": "listing what is in flight (no ids given)"' in result.content
    assert f'get_video_jobs(clip_ids=["{CLIP_ID}"])' in result.content
    # Nothing in flight: said, not an error.
    monkeypatch.setattr(jobs, "jobs_in_flight", lambda _user_id: asyncio.sleep(0, result={}))
    result = asyncio.run(executor.execute_tool(ToolCall(id="j2", name="get_video_jobs", arguments={})))
    assert not result.is_error and result.content.startswith("Nothing of yours is being made")


def test_cancel_video_job_stops_only_the_callers_own_jobs(monkeypatch):
    from app.models.video_studio import VideoRender
    from app.services import audit

    mine = VideoRender(id=uuid.uuid4(), user_id=USER_ID, status=JOB_RUNNING)
    theirs = VideoRender(id=uuid.uuid4(), user_id=uuid.uuid4(), status=JOB_RUNNING)
    done = VideoRender(id=uuid.uuid4(), user_id=USER_ID, status=JOB_READY)
    rows = {str(r.id): r for r in (mine, theirs, done)}
    cancelled: list = []
    audited: list = []

    class _Db:
        async def get(self, _model, key):
            return rows.get(str(key))

        async def commit(self):
            return None

    async def cancel(_db, kind, row, *, reason=None):
        if row.status in (JOB_READY, JOB_FAILED):
            return False
        cancelled.append((kind, row.id))
        return True

    async def log(_db, **entry):
        audited.append(entry)

    monkeypatch.setattr(jobs, "cancel", cancel)
    monkeypatch.setattr(audit, "log_audit", log)
    executor = _tool_with(monkeypatch, _Db())
    arguments = {"render_ids": [str(mine.id), str(theirs.id), str(done.id)]}
    result = asyncio.run(executor.execute_tool(ToolCall(id="c1", name="cancel_video_job", arguments=arguments)))
    assert cancelled == [("render", mine.id)] and not result.is_error
    assert f"render `{mine.id}`: cancelled." in result.content
    assert f"render `{theirs.id}`: none of yours" in result.content
    assert f"render `{done.id}`: already ready" in result.content
    assert audited[0]["summary"] == "Cancelled 1 Video Studio job(s) through an agent"


# ── 12: a voice style the speech model ignores ────────────────────────────


class _SpeechProvider:
    """OpenAI's speech surface: ``tts-1`` / ``tts-1-hd`` take no delivery instruction."""

    default_speech_model = "gpt-4o-mini-tts"

    def speech_takes_style(self, model: str) -> bool | None:
        from app.llm.speech import OpenAISpeechMixin

        return OpenAISpeechMixin.speech_takes_style(self, model)

    async def list_speech_models(self) -> list[str]:
        return ["gpt-4o-mini-tts", "tts-1", "tts-1-hd"]

    async def list_voices(self, model: str | None = None) -> list:
        from app.llm.speech import OpenAISpeechMixin

        return await OpenAISpeechMixin.list_voices(self, model)


class _GatewaySpeechProvider(_SpeechProvider):
    """A gateway: ElevenLabs speaks the account's voices and takes no style, OpenAI's models do."""

    speech_voices_are_fixed = False
    default_speech_model = None

    def speech_takes_style(self, model: str) -> bool | None:
        return False if model.startswith("eleven_") else super().speech_takes_style(model)

    async def list_speech_models(self) -> list[str]:
        return ["eleven_multilingual_v2", "gpt-4o-mini-tts"]


def test_openai_says_which_of_its_speech_models_take_a_style():
    provider = _SpeechProvider()
    assert provider.speech_takes_style("tts-1") is False and provider.speech_takes_style("tts-1-hd") is False
    assert provider.speech_takes_style("gpt-4o-mini-tts") is True
    # Another vendor's model behind a gateway is not OpenAI's to judge.
    assert provider.speech_takes_style("eleven_multilingual_v2") is None


def _speech_platform(monkeypatch, model: str, provider=None, voice: str = "alloy"):
    from app.llm import router
    from app.llm.router import SpeechTarget

    config = SimpleNamespace(id=uuid.uuid4(), provider="openai", display_name="OpenAI")
    provider = provider or _SpeechProvider()

    async def speech_provider(_db, *, user_id, provider_id=None, model: str | None = None, **_kwargs):
        return SpeechTarget(provider=provider, model=model or platform_model, config=config, voice=voice)

    platform_model = model
    monkeypatch.setattr(router, "get_speech_provider", speech_provider)


def _narrated(voiceover: dict):
    return parse_video_spec(
        validate_video_spec(
            {
                "title": "Brand film",
                "voiceover": voiceover,
                "scenes": [{"type": "statement", "lines": ["We see it all."], "narration": "We see it all."}],
            }
        )
    )


def test_a_style_the_platforms_model_ignores_is_spoken_by_one_of_its_provider_that_takes_it(monkeypatch):
    _speech_platform(monkeypatch, "tts-1-hd")
    spec = _narrated({"style": "A confident, cinematic brand voice."})
    target = asyncio.run(voice.resolve(None, USER_ID, spec.voiceover))
    assert target.model == "gpt-4o-mini-tts" and target.voice == "alloy"
    line = asyncio.run(voice.describe(None, USER_ID, spec))
    assert line.startswith("Voice-over: gpt-4o-mini-tts on OpenAI (the platform's provider), voice alloy.")
    assert "the platform's speech model, tts-1-hd, takes no delivery instruction, so gpt-4o-mini-tts" in line
    # Without a style, the platform's model speaks as chosen.
    assert asyncio.run(voice.resolve(None, USER_ID, _narrated({}).voiceover)).model == "tts-1-hd"


def test_a_styled_model_that_lacks_the_platforms_voice_speaks_with_its_own_default(monkeypatch):
    # An ElevenLabs voice id sent to an OpenAI model is refused (#713).
    _speech_platform(monkeypatch, "eleven_multilingual_v2", _GatewaySpeechProvider(), voice="v-rachel")
    target = asyncio.run(voice.resolve(None, USER_ID, _narrated({"style": "Warm and assured."}).voiceover))
    assert (target.model, target.voice) == ("gpt-4o-mini-tts", None)


@pytest.mark.parametrize(("own", "model"), [("v-clone", "eleven_multilingual_v2"), ("nova", "gpt-4o-mini-tts")])
def test_a_videos_own_voice_moves_with_its_style_only_to_a_model_that_has_it(monkeypatch, own, model):
    # The video's voice outranks its style: a model without it would refuse it (#713).
    _speech_platform(monkeypatch, "eleven_multilingual_v2", _GatewaySpeechProvider(), voice="v-rachel")
    target = asyncio.run(voice.resolve(None, USER_ID, _narrated({"style": "Warm.", "voice": own}).voiceover))
    assert target.model == model


def test_a_style_the_videos_own_model_ignores_is_said_at_save(monkeypatch):
    _speech_platform(monkeypatch, "gpt-4o-mini-tts")
    spec = _narrated({"model": "tts-1-hd", "style": "Warm and assured."})
    target = asyncio.run(voice.resolve(None, USER_ID, spec.voiceover))
    assert target.model == "tts-1-hd"
    line = asyncio.run(voice.describe(None, USER_ID, spec))
    assert "Its style is NOT applied: tts-1-hd takes no delivery instruction" in line


# ── 14: a skipped optional step ───────────────────────────────────────────


def test_an_optional_step_waits_three_seconds_unless_told():
    assert TakeStep(action="click", text="Accept cookies", optional=True).wait_seconds == 3.0
    assert TakeStep(action="click", text="Save").wait_seconds == 15.0
    assert TakeStep(action="click", text="Accept", optional=True, timeout=8).wait_seconds == 8.0


def test_the_wait_of_a_skipped_step_is_cut_from_the_take_whatever_the_idle_mode():
    events = [
        {"step": 0, "action": "goto", "t0": 0.0, "ta": 0.0, "t1": 1.0},
        {"step": 1, "action": "click", "t0": 1.2, "ta": 16.2, "t1": 16.2, "ok": False},
        {"step": 2, "action": "click", "t0": 16.4, "ta": 17.0, "t1": 17.5},
    ]
    for mode in ("compress", "cut", "off"):
        points = timeline.build_time_map(
            duration=20.0,
            trim_start=0.0,
            trim_end=None,
            speed=1.0,
            idle_mode=mode,
            idle_threshold=1.2,
            idle_speed=6.0,
            events=events,
        )
        # The 15 s the browser looked for the element play in well under a second.
        out_at_skip_end = timeline.map_inverse(points, 16.2)
        out_at_skip_start = timeline.map_inverse(points, 1.2)
        assert out_at_skip_end - out_at_skip_start < 0.5, mode
    # A skipped step is no action: it keeps nothing at real time around it.
    assert timeline._event_active_windows(events[1:2]) == []


# ── 2a: a slide without the deck's chrome ─────────────────────────────────


def test_a_slide_scene_is_drawn_without_the_decks_chrome_when_asked(monkeypatch):
    from app.services import presentation_export

    deck = str(uuid.uuid4())
    scene = parse_video_spec(
        validate_video_spec({"title": "x", "scenes": [{"type": "slide", "presentation_id": deck, "chrome": False}]})
    ).scenes[0]
    assert scene.chrome is False
    evaluated: list = []

    class _Slides:
        async def count(self):
            return 1

        def nth(self, _index):
            return SimpleNamespace(screenshot=lambda **_kw: asyncio.sleep(0, result=b"PNG"))

    class _Page:
        def locator(self, _selector):
            return _Slides()

        async def evaluate(self, script, arg=None):
            evaluated.append(arg)
            return 3

    @asynccontextmanager
    async def page(*_args, **_kwargs):
        yield _Page()

    monkeypatch.setattr(presentation_export, "open_render_page", page)
    monkeypatch.setattr(presentation_export, "mint_render_token", lambda *_a: "token")
    rendered = asyncio.run(
        presentation_export.render_deck(
            uuid.UUID(deck), USER_ID, aspect_ratio="16:9", want_slide_images=True, slide_indices=[0], hide_chrome=True
        )
    )
    assert rendered.slide_images == [b"PNG"] and presentation_export.EXPORT_NO_CHROME_ATTR in evaluated
    # The stylesheet answers the attribute by hiding the chrome.
    css = (FRONTEND_ROOT / "src/components/presentation/presentation.css").read_text(encoding="utf-8")
    assert f".deck-static-slide[{presentation_export.EXPORT_NO_CHROME_ATTR}] > .deck-chrome," in css
