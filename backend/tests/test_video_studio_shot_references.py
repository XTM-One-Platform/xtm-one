"""What an AI shot keeps and what it is checked against. Synthetic pictures and prompts only.

* reference images: a subject the shot keeps across shots, sent to the
  models whose provider declares it (Veo 3.1, Kling 3.0 Omni), refused by the
  model's name elsewhere, kept on the row and sent again on every generation;
* the content check: the subjects a prompt names read on the shot's first,
  middle and last frames, decided in code; a prompt naming none reads no frame;
* image edits: ``generate_image`` drawing from reference pictures and a mask,
  where the image model takes them.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from app.llm.video import VideoGenerationError, VideoRequest, VideoResult
from app.models.file_attachment import FileAttachment
from app.models.video_studio import JOB_FAILED, JOB_PENDING, JOB_READY, VideoClip
from app.services.video_studio import assets, clips, content_drift, encoder, jobs, runners, text_drift

pytestmark = pytest.mark.usefixtures("no_default_video_template")

USER = SimpleNamespace(id=uuid.UUID("00000000-0000-0000-0000-00000000b001"), role="user")
PICTURES = [f"00000000-0000-0000-0000-0000000000a{i}" for i in range(1, 6)]


def _png(colour=(40, 120, 200)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 48), colour).save(out, format="PNG")
    return out.getvalue()


class _Veo:
    """Declares Veo 3.1's limits: three reference images, never with a start frame."""

    def max_reference_images(self, model):
        return 3

    def reference_images_with_start_frame(self, model):
        return False


class _Omni(_Veo):
    def reference_images_with_start_frame(self, model):
        return True


class _None:
    def max_reference_images(self, model):
        return 0

    def reference_images_with_start_frame(self, model):
        return True


class _Files:
    def __init__(self, content_type: str = "image/png") -> None:
        self.content_type = content_type
        self.added: list = []

    async def get(self, _model, key):
        return FileAttachment(id=key, filename="subject.png", content_type=self.content_type, size=10, s3_object_name="o")

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None

    async def refresh(self, _row, with_for_update=False):
        return None


@pytest.fixture
def readable(monkeypatch):
    from app.services import file_access

    async def can_read(_row, _user, _db):
        return True

    async def fine(_row):
        return None

    monkeypatch.setattr(file_access, "can_read_file", can_read)
    monkeypatch.setattr(clips, "check_start_picture", fine)


# ── Reference images: checked against the model before anything is paid ─


def test_reference_images_are_refused_by_the_models_name_where_it_takes_none(readable):
    with pytest.raises(clips.ClipError, match="sora-2 takes no reference images") as refused:
        asyncio.run(
            clips.resolve_reference_images(_Files(), USER, PICTURES[:1], provider=_None(), model="sora-2", start_frame=False)
        )
    assert refused.value.about_model
    with pytest.raises(clips.ClipError, match="at most 3 reference images"):
        asyncio.run(
            clips.resolve_reference_images(_Files(), USER, PICTURES[:4], provider=_Veo(), model="veo", start_frame=False)
        )


def test_a_model_that_takes_them_or_a_start_frame_says_so(readable):
    with pytest.raises(clips.ClipError, match="reference images or a start frame, not both"):
        asyncio.run(
            clips.resolve_reference_images(
                _Files(), USER, PICTURES[:2], provider=_Veo(), model="veo-3.1-generate-preview", start_frame=True
            )
        )
    kept = asyncio.run(
        clips.resolve_reference_images(
            _Files(), USER, [f"[[FILE:{PICTURES[0]}]]", PICTURES[1], PICTURES[0]], provider=_Omni(), model="omni", start_frame=True
        )
    )
    assert kept == PICTURES[:2]
    with pytest.raises(clips.ClipError, match="No picture"):
        asyncio.run(
            clips.resolve_reference_images(
                _Files("video/mp4"), USER, PICTURES[:1], provider=_Veo(), model="veo", start_frame=False
            )
        )
    assert asyncio.run(clips.resolve_reference_images(_Files(), USER, None, provider=_None(), model="m", start_frame=True)) == []


def _resolving(monkeypatch, provider) -> None:
    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        return provider, model or "veo-3.1-generate-preview", SimpleNamespace(id=uuid.uuid4(), provider="gemini")

    async def no_frame(*_args, **_kwargs):
        return {}

    async def enqueue(*_args, **_kwargs):
        return None

    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(clips, "resolve_reference", no_frame)
    monkeypatch.setattr(jobs, "enqueue", enqueue)


def test_a_shot_keeps_its_reference_images_and_sends_them_again_when_generated_again(monkeypatch, readable):
    _resolving(monkeypatch, _Veo())
    files = _Files()
    clip, _label = asyncio.run(
        clips.start_clip(files, USER, prompt="A grey heron wading at dawn", reference_image_file_ids=PICTURES[:2])
    )
    assert clip.params[clips.REFERENCE_IMAGES] == PICTURES[:2] and files.added == [clip]
    clip.status = JOB_READY
    asyncio.run(clips.restart_clip(files, USER, clip, prompt="The heron takes off"))
    assert clip.params[clips.REFERENCE_IMAGES] == PICTURES[:2] and clip.status == JOB_PENDING
    # Others named replace them; an empty list drops them.
    clip.status = JOB_READY
    asyncio.run(clips.restart_clip(files, USER, clip, reference_image_file_ids=[PICTURES[2]]))
    assert clip.params[clips.REFERENCE_IMAGES] == [PICTURES[2]]
    clip.status = JOB_READY
    asyncio.run(clips.restart_clip(files, USER, clip, reference_image_file_ids=[]))
    assert clips.REFERENCE_IMAGES not in clip.params


def test_a_shot_generated_again_with_a_model_that_takes_none_is_refused_by_name(monkeypatch, readable):
    _resolving(monkeypatch, _Veo())
    clip, _label = asyncio.run(clips.start_clip(_Files(), USER, prompt="A heron", reference_image_file_ids=PICTURES[:1]))
    clip.status = JOB_READY
    _resolving(monkeypatch, _None())
    with pytest.raises(clips.ClipError, match="sora-2 takes no reference images"):
        asyncio.run(clips.restart_clip(_Files(), USER, clip, model="sora-2"))


def test_every_generation_reads_the_same_pictures_as_every_provider_reads_them(monkeypatch, tmp_path):
    async def download(file_id, path):
        Path(path).write_bytes(_png())
        return SimpleNamespace(content_type="image/png")

    monkeypatch.setattr(assets, "download_file", download)
    sent = asyncio.run(clips.reference_pictures({clips.REFERENCE_IMAGES: PICTURES[:2]}, str(tmp_path)))
    assert [mime for _data, mime in sent] == ["image/png", "image/png"]
    assert clips.reference_note({clips.REFERENCE_IMAGES: PICTURES[:2]}) == "Asked with 2 reference images to keep."

    async def gone(_file_id, _path):
        return None

    monkeypatch.setattr(assets, "download_file", gone)
    with pytest.raises(clips.ClipError, match="reference image of this shot is gone"):
        asyncio.run(clips.reference_pictures({clips.REFERENCE_IMAGES: PICTURES[:1]}, str(tmp_path)))


def test_the_capabilities_say_what_each_model_takes():
    from app.llm.gemini_provider import GeminiProvider
    from app.llm.kling_provider import KlingProvider
    from app.llm.router import video_reference_limits

    veo = GeminiProvider.__new__(GeminiProvider)
    assert video_reference_limits(veo, "veo-3.1-generate-preview") == {
        "reference_images": 3,
        "reference_images_with_start_frame": False,
    }
    assert veo.max_reference_images("veo-3.1-fast-generate-preview") == 3
    assert veo.max_reference_images("veo-3.1-lite-generate-preview") == 0
    assert veo.max_reference_images("veo-3.0-generate-001") == 0
    kling = KlingProvider(api_key="k")
    assert video_reference_limits(kling, "kling-3.0-omni") == {
        "reference_images": 3,
        "reference_images_with_start_frame": True,
    }
    assert kling.max_reference_images("kling-v3") == 0


# ── Generated again in place with reference images ───────────────

TAKE = "00000000-0000-0000-0000-0000000000e1"
FRAME = "00000000-0000-0000-0000-0000000000f1"
ALONE = "veo-3.1-generate-preview takes reference images or a start frame, not both"


class _Locking(_Files):
    def __init__(self) -> None:
        super().__init__()
        self.locked: list = []

    async def refresh(self, row, with_for_update=False):
        if with_for_update:
            self.locked.append(row)


def _declaring(monkeypatch, provider, model: str = "veo-3.1-generate-preview") -> list:
    """What the model declares before it is resolved (``router.declared_video_reference_limits``); the lookups."""
    from app.llm import router

    looked_up: list = []

    async def declared(_db, *, provider_id=None, model=None, user_id=None, **_kwargs):
        looked_up.append((provider_id, model))
        chosen = model or default
        return chosen, router.video_reference_limits(provider, chosen)

    default = model
    monkeypatch.setattr(router, "declared_video_reference_limits", declared)
    return looked_up


def _shot(**params) -> VideoClip:
    return VideoClip(
        id=uuid.uuid4(),
        user_id=USER.id,
        provider="gemini",
        model="veo-3.1-generate-preview",
        prompt="A grey heron wading at dawn",
        params={"provider_id": str(uuid.uuid4()), "duration_seconds": 8, "aspect_ratio": "16:9", **params},
        status=JOB_READY,
    )


def _cut_from_a_recording(**source) -> dict:
    return {"kind": "recording", "id": TAKE, "file_id": TAKE, "at_seconds": 2.0, "frame_file_id": FRAME, **source}


def test_a_models_limits_are_read_as_the_generation_picks_it_without_asking_the_provider(monkeypatch):
    from app.llm import router

    named, platform = SimpleNamespace(id=uuid.uuid4(), provider="gemini"), SimpleNamespace(id=uuid.uuid4(), provider="gemini")
    settings = SimpleNamespace(video_provider_id=platform.id, video_model="veo-3.1-generate-preview")

    class _Db:
        async def execute(self, _query):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [named, platform]))

    async def platform_settings(_db):
        return settings

    async def cached(config):
        return _Omni() if config is named else _Veo()

    monkeypatch.setattr(router, "_get_platform_settings", platform_settings)
    monkeypatch.setattr(router, "_cached_provider", cached)

    def declared(**kwargs):
        return asyncio.run(router.declared_video_reference_limits(_Db(), **kwargs))

    # The provider named, with its model; the one chosen in Settings, with its model or the one named.
    assert declared(provider_id=str(named.id), model="kling-3.0-omni") == (
        "kling-3.0-omni",
        {"reference_images": 3, "reference_images_with_start_frame": True},
    )
    assert declared() == ("veo-3.1-generate-preview", {"reference_images": 3, "reference_images_with_start_frame": False})
    assert declared(model="veo-3.1-fast-generate-preview")[0] == "veo-3.1-fast-generate-preview"
    # Only the resolution can tell: an unknown provider, nothing chosen in Settings.
    assert declared(provider_id=str(uuid.uuid4())) is None
    # A provider that excludes the caller names no model and nothing it takes; the resolution refuses.
    outsider = uuid.uuid4()
    named.excluded_user_ids = [str(outsider)]
    assert declared(provider_id=str(named.id), model="kling-3.0-omni", user_id=outsider) is None
    assert declared(provider_id=str(named.id), model="kling-3.0-omni", user_id=uuid.uuid4())[0] == "kling-3.0-omni"
    settings.video_provider_id = None
    assert declared(model="veo-3.1-generate-preview") is None


def test_a_shot_made_from_a_picture_is_generated_again_with_reference_images_alone(monkeypatch, readable):
    _resolving(monkeypatch, _Veo())
    _declaring(monkeypatch, _Veo())
    shot = _shot(reference_file_id=PICTURES[4])

    plan = asyncio.run(clips.restart_clip(_Files(), USER, shot, reference_image_file_ids=PICTURES[:2]))

    assert shot.status == JOB_PENDING and shot.params[clips.REFERENCE_IMAGES] == PICTURES[:2]
    assert clips.start_frame_file_id(shot.params) is None and "reference_video" not in shot.params
    assert plan.notes == ["Start frame dropped: veo-3.1-generate-preview takes reference images alone."]
    # A frame cut from a video goes the same way; its file stays in the conversation, never among the media replaced.
    cut = _shot(reference_file_id=None, reference_video=_cut_from_a_recording())
    cut.file_id, cut.poster_file_id = uuid.uuid4(), uuid.uuid4()
    asyncio.run(clips.restart_clip(_Files(), USER, cut, reference_image_file_ids=PICTURES[:2]))
    assert clips.start_frame_file_id(cut.params) is None and "reference_video" not in cut.params
    assert FRAME not in clips.owned_files(cut)


def test_a_model_that_takes_both_keeps_the_start_frame_beside_the_reference_images(monkeypatch, readable):
    _resolving(monkeypatch, _Omni())
    _declaring(monkeypatch, _Omni(), "kling-3.0-omni")
    shot = _shot(reference_file_id=PICTURES[4])

    plan = asyncio.run(clips.restart_clip(_Files(), USER, shot, reference_image_file_ids=PICTURES[:2]))

    assert shot.params[clips.REFERENCE_IMAGES] == PICTURES[:2] and shot.params["reference_file_id"] == PICTURES[4]
    assert plan.notes == []


def test_start_frame_false_drops_the_frame_and_sends_the_same_reference_images_again(monkeypatch, readable):
    _resolving(monkeypatch, _Omni())
    shot = _shot(reference_file_id=PICTURES[4], **{clips.REFERENCE_IMAGES: PICTURES[:2]})

    plan = asyncio.run(clips.restart_clip(_Files(), USER, shot, start_frame=False))

    assert shot.params[clips.REFERENCE_IMAGES] == PICTURES[:2] and clips.start_frame_file_id(shot.params) is None
    assert plan.notes == ["Start frame dropped (start_frame: false)."]


def test_what_the_model_cannot_take_is_refused_before_a_row_is_locked_or_a_model_resolved(monkeypatch, readable):
    resolved: list = []

    async def resolve(*_args, **kwargs):
        resolved.append(kwargs)
        raise AssertionError("resolved before the refusal")

    monkeypatch.setattr(clips, "resolve", resolve)
    looked_up = _declaring(monkeypatch, _Veo())
    db = _Locking()
    asked = {
        "the frame kept on purpose": (_shot(reference_file_id=PICTURES[4]), {"reference_image_file_ids": PICTURES[:2], "start_frame": True}),
        "a new start": (_shot(), {"reference_image_file_ids": PICTURES[:1], "reference_recording_id": TAKE}),
        "another model": (
            _shot(reference_file_id=PICTURES[4], **{clips.REFERENCE_IMAGES: PICTURES[:2]}),
            {"model": "veo-3.1-generate-preview"},
        ),
    }
    for case, (shot, changes) in asked.items():
        with pytest.raises(clips.ClipError) as refused:
            asyncio.run(clips.restart_clip(db, USER, shot, **changes))
        message = str(refused.value)
        assert message.startswith(ALONE), case
        # The two ways out, as arguments of the retry: the reference images alone, the start frame alone.
        assert "start_frame: false" in message and "start_frame: true with the reference_*" in message, case
        assert refused.value.about_model and shot.status == JOB_READY, case
    # The model the shot keeps is looked up on its own provider; a model named, wherever the platform serves it.
    assert looked_up[0] == (asked["the frame kept on purpose"][0].params["provider_id"], "veo-3.1-generate-preview")
    assert looked_up[-1] == (None, "veo-3.1-generate-preview")
    # A new shot too: nothing resolved, the ways out named as its own arguments.
    with pytest.raises(clips.ClipError, match="leave out the reference_\\* start") as refused:
        asyncio.run(
            clips.start_clip(
                db, USER, prompt="A heron", reference_file_id=PICTURES[4], reference_image_file_ids=PICTURES[:2]
            )
        )
    assert str(refused.value).startswith(ALONE)
    assert db.locked == [] and resolved == [] and db.added == []


def test_a_start_frame_dropped_and_named_at_once_or_kept_where_there_is_none_is_refused(monkeypatch, readable):
    _resolving(monkeypatch, _Veo())
    with pytest.raises(clips.ClipError, match="start_frame: false drops the shot's start frame: name no"):
        asyncio.run(clips.restart_clip(_Files(), USER, _shot(reference_file_id=PICTURES[4]), start_frame=False, reference_clip_id=TAKE))
    with pytest.raises(clips.ClipError, match="no start frame to keep"):
        asyncio.run(clips.restart_clip(_Files(), USER, _shot(), start_frame=True))


def test_a_shot_generated_again_starts_anew_or_from_another_moment_of_its_video(monkeypatch, readable):
    _resolving(monkeypatch, _Veo())
    asked: list[dict] = []

    async def resolve_reference(_db, _user, **kwargs):
        asked.append(kwargs)
        if kwargs.get("file_id"):
            return {"reference_file_id": kwargs["file_id"]}
        return {"reference_video": {"kind": "recording", "id": TAKE, "file_id": TAKE, "at_seconds": kwargs.get("at_seconds")}}

    monkeypatch.setattr(clips, "resolve_reference", resolve_reference)
    shot = _shot(reference_file_id=None, reference_video=_cut_from_a_recording(step=2, crop="center"))

    # The start the shot has, named again as it was asked: its kept frame is used again, nothing re-read.
    same = {"reference_recording_id": TAKE, "reference_at_step": 2, "crop": "center"}
    asyncio.run(clips.restart_clip(_Files(), USER, shot, **same))
    assert asked == [] and shot.params["reference_video"]["frame_file_id"] == FRAME
    # A moment re-sent with more decimals than the millisecond it is kept at names the same frame.
    timed = _shot(reference_file_id=None, reference_video=_cut_from_a_recording())
    asyncio.run(clips.restart_clip(_Files(), USER, timed, reference_recording_id=TAKE, reference_at_seconds=2.0004))
    assert asked == [] and timed.params["reference_video"]["frame_file_id"] == FRAME
    timed.status = JOB_READY
    asyncio.run(clips.restart_clip(_Files(), USER, timed, reference_recording_id=TAKE, reference_at_seconds=2.5))
    assert asked[-1]["at_seconds"] == 2.5 and "frame_file_id" not in timed.params["reference_video"]
    asked.clear()
    # Named with reference images, it is a start frame asked for: refused by a model taking one or the other.
    _declaring(monkeypatch, _Veo())
    shot.status = JOB_READY
    with pytest.raises(clips.ClipError, match=ALONE):
        asyncio.run(clips.restart_clip(_Files(), USER, shot, reference_image_file_ids=PICTURES[:1], **same))

    # A moment alone moves the frame of the recording the shot started from, its crop kept; the frame is cut anew.
    asyncio.run(clips.restart_clip(_Files(), USER, shot, reference_at_seconds=4.5))
    assert asked[-1] == {"aspect_ratio": "16:9", "recording_id": TAKE, "at_seconds": 4.5, "crop": "center"}
    assert shot.params["reference_video"]["at_seconds"] == 4.5 and "frame_file_id" not in shot.params["reference_video"]
    # A source named is a new start, checked as a new shot's is.
    shot.status = JOB_READY
    asyncio.run(clips.restart_clip(_Files(), USER, shot, reference_file_id=PICTURES[4]))
    assert asked[-1] == {"aspect_ratio": "16:9", "file_id": PICTURES[4]}
    assert shot.params["reference_file_id"] == PICTURES[4] and "reference_video" not in shot.params


def _run_tool(monkeypatch, shot: VideoClip, arguments: dict):
    """``generate_video_clip`` as the assistant calls it: ``(answer, session)``."""
    from app.services.builtin_tools import video_studio as tool
    from app.tools.base import ToolCall

    class _Db(_Locking):
        committed = 0

        async def get(self, model, key):
            return shot if model is VideoClip else await super().get(model, key)

        async def commit(self):
            self.committed += 1

    db = _Db()

    @asynccontextmanager
    async def session():
        yield db

    async def find_user(_db, _user_id):
        return USER

    monkeypatch.setattr(tool, "async_session", session)
    monkeypatch.setattr(tool, "_user", find_user)
    _definitions, executor = tool.build_video_studio_tools(None, str(USER.id))
    answer = asyncio.run(executor.execute_tool(ToolCall(id="c1", name="generate_video_clip", arguments=arguments)))
    return answer, db


def test_validate_only_checks_a_shot_and_answers_the_request_without_queueing_or_paying(monkeypatch, readable):
    _resolving(monkeypatch, _Veo())
    queued: list = []

    async def enqueue(*args, **_kwargs):
        queued.append(args)

    monkeypatch.setattr(jobs, "enqueue", enqueue)
    shot = _shot(reference_file_id=PICTURES[4])
    before = dict(shot.params)

    answer, db = _run_tool(
        monkeypatch, shot, {"clip_id": str(shot.id), "reference_image_file_ids": PICTURES[:2], "validate_only": True}
    )

    request = json.loads(answer.content.splitlines()[0])
    assert not answer.is_error and request["validated"] is True and request["clip_id"] == str(shot.id)
    assert request["model"] == "veo-3.1-generate-preview" and request["start_frame"] is None
    assert request["reference_image_file_ids"] == PICTURES[:2] and request["notes"][0].startswith("Start frame dropped")
    assert "nothing was queued or paid" in answer.content
    assert shot.params == before and shot.status == JOB_READY
    assert queued == [] and db.locked == [] and db.committed == 0
    # A new shot: checked, no row added.
    answer, db = _run_tool(monkeypatch, shot, {"prompt": "A heron", "duration_seconds": 4, "validate_only": True})
    request = json.loads(answer.content.splitlines()[0])
    assert request["validated"] is True and request["clip_id"] is None and request["duration_seconds"] == 4
    assert db.added == [] and queued == []
    # A refusal reads as on a real call: a validation note, never a tool error (nothing reached a provider).
    answer, _db = _run_tool(
        monkeypatch,
        shot,
        {"clip_id": str(shot.id), "reference_image_file_ids": PICTURES[:2], "start_frame": True, "validate_only": True},
    )
    refused = json.loads(answer.content.splitlines()[0])
    assert not answer.is_error and refused["status"] == "invalid_parameters" and refused["clip_id"] == str(shot.id)
    assert ALONE in refused["reason"] and "video_studio_capabilities" in answer.content


def test_a_retry_says_the_start_frame_it_dropped_and_every_retry_argument_is_offered(monkeypatch, readable):
    from app.api.v1.schemas.video_studio import ClipRetry
    from app.services.builtin_tools import video_studio as tool

    _resolving(monkeypatch, _Veo())
    _declaring(monkeypatch, _Veo())
    shot = _shot(reference_file_id=PICTURES[4])

    async def nothing(*_args, **_kwargs):
        return None

    async def waited(*_args, **_kwargs):
        return [shot]

    async def failed(_kind, _row):
        return SimpleNamespace(status=jobs.STATE_FAILED, reason="the provider refused it")

    monkeypatch.setattr(jobs, "wake", nothing)
    monkeypatch.setattr(jobs, "wait_for", waited)
    monkeypatch.setattr(tool, "_state_of", failed)
    answer, _db = _run_tool(monkeypatch, shot, {"clip_id": str(shot.id), "reference_image_file_ids": PICTURES[:2]})
    assert "Start frame dropped: veo-3.1-generate-preview takes reference images alone." in answer.content
    # start_frame belongs to a shot generated again.
    answer, _db = _run_tool(monkeypatch, shot, {"prompt": "A heron", "start_frame": False})
    assert not answer.is_error and json.loads(answer.content.splitlines()[0])["status"] == "invalid_parameters"
    assert "with clip_id" in answer.content

    schema = next(d for d in tool._definitions(["high"]) if d.name == "generate_video_clip")
    assert {"start_frame", "validate_only", *clips.START_FIELDS} <= set(schema.input_schema["properties"])
    assert "validate_only" in schema.description and "start_frame: false" in schema.description
    assert set(clips.RETRY_FIELDS) == set(ClipRetry.model_fields)


# ── The providers: Veo 3.1 and Kling 3.0 Omni ─────────────────────────────


def test_veo_is_asked_with_asset_references_at_its_one_length():
    from google.genai import types

    from app.llm.gemini_provider import GeminiProvider

    asked: dict = {}
    done = SimpleNamespace(
        done=True,
        error=None,
        name="operations/r1",
        response=SimpleNamespace(generated_videos=[SimpleNamespace(video=SimpleNamespace(video_bytes=b"VEO"))]),
    )

    async def generate_videos(**kwargs):
        asked.update(kwargs)
        return done

    provider = GeminiProvider.__new__(GeminiProvider)
    provider.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_videos=generate_videos)))
    request = VideoRequest(
        model="veo-3.1-generate-preview", prompt="The heron lands", duration_seconds=4, reference_images=[(_png(), "image/png")]
    )
    result = asyncio.run(provider.generate_video(request))
    config = asked["config"]
    assert result.data == b"VEO" and asked["image"] is None
    assert config.duration_seconds == 8 and len(config.reference_images) == 1
    assert config.reference_images[0].reference_type == types.VideoGenerationReferenceType.ASSET
    assert any("reference images at 8 s only" in note for note in result.notes)


_REAL_CLIENT = httpx.AsyncClient
_SIGNED = "https://storage.example.com/made.mp4?signature=abc"


@pytest.fixture
def kling_api(monkeypatch):
    from app.llm import video
    from app.services import web_fetch

    monkeypatch.setattr(video, "POLL_SECONDS", 0)
    monkeypatch.setattr(web_fetch, "resolve_pinned_address", lambda host, port=None: "93.184.216.34")

    def install(handler):
        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return _REAL_CLIENT(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)

    return install


def test_kling_omni_keeps_the_subjects_of_its_references_beside_a_first_frame(kling_api):
    from app.llm.kling_provider import KlingProvider

    seen: dict = {"polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "storage.example.com":
            return httpx.Response(200, content=b"MP4-OMNI", headers={"content-type": "video/mp4"})
        if request.method == "POST":
            seen["path"], seen["body"] = request.url.path, json.loads(request.content)
            return httpx.Response(200, json={"code": 0, "data": {"id": "o1", "status": "submitted"}})
        seen["query"] = (request.url.path, dict(request.url.params))
        seen["polls"] += 1
        if seen["polls"] == 1:
            return httpx.Response(200, json={"code": 0, "data": [{"id": "o1", "status": "processing"}]})
        outputs = [{"type": "video", "url": _SIGNED, "duration": "5"}]
        return httpx.Response(200, json={"code": 0, "data": [{"id": "o1", "status": "succeeded", "outputs": outputs}]})

    kling_api(handler)
    jobs_seen: list[str] = []

    async def on_job(job):
        jobs_seen.append(job)

    first, subject = _png((10, 10, 10)), _png((200, 200, 200))
    request = VideoRequest(
        model="kling-3.0-omni",
        prompt="The same heron walks along the shore",
        duration_seconds=5,
        negative_prompt="extra legs",
        reference_image=first,
        reference_mime="image/png",
        reference_images=[(subject, "image/png")],
        sound=False,
    )
    result = asyncio.run(KlingProvider(api_key="k").generate_video(request, on_job=on_job))
    assert result.data == b"MP4-OMNI" and result.job_id == "omni:o1" and jobs_seen == ["omni:o1"]
    assert seen["path"] == "/omni-video/kling-3.0-omni" and seen["query"] == ("/tasks", {"task_ids": "o1"})
    contents = seen["body"]["contents"]
    assert [c["type"] for c in contents] == ["prompt", "first_frame", "refer_image"]
    assert contents[0]["text"].endswith("Avoid: extra legs")
    assert contents[2] == {"type": "refer_image", "url": base64.b64encode(subject).decode("ascii"), "id": "reference_1"}
    assert seen["body"]["settings"] == {"duration": 5, "resolution": "1080p", "audio": "off", "multi_shot": False}


def test_kling_omni_failed_says_why_and_a_classic_model_refuses_references(kling_api):
    from app.llm.kling_provider import KlingProvider

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"code": 0, "data": {"id": "o2"}})
        return httpx.Response(200, json={"code": 0, "data": [{"id": "o2", "status": "failed", "message": "risk control"}]})

    kling_api(handler)
    with pytest.raises(VideoGenerationError, match="risk control"):
        asyncio.run(KlingProvider(api_key="k").generate_video(VideoRequest(model="kling-3.0-omni", prompt="x")))
    classic = VideoRequest(model="kling-v3", prompt="x", reference_images=[(_png(), "image/png")])
    with pytest.raises(VideoGenerationError, match="kling-v3 takes no reference images"):
        asyncio.run(KlingProvider(api_key="k").generate_video(classic))


# ── The content check ─────────────────────────────────────────────────────


SUBJECTS = [content_drift.Subject("cows", "animals: dairy cows")]


def test_the_negative_prompt_is_split_into_terms_in_code():
    assert content_drift.excluded_terms("morphing, extra limbs; human hands\n blur.,") == [
        "morphing",
        "extra limbs",
        "human hands",
        "blur",
    ]
    assert content_drift.excluded_terms(None) == []


def test_the_subjects_of_a_prompt_are_read_from_an_answer_or_refused():
    answer = '```json\n{"subjects": [{"name": "cows", "kind": "dairy cows"}, {"name": "Cows"}, {"kind": "x"}]}\n```'
    assert content_drift.parse_subjects(answer) == [content_drift.Subject("Cows", "Cows")]
    assert content_drift.parse_subjects('{"subjects": []}') == []
    assert content_drift.parse_subjects("no json here") is None


def _frame(*subjects, excluded=()):
    return {"subjects": list(subjects), "excluded_seen": list(excluded)}


def test_a_subject_of_another_kind_deformed_or_replaced_is_a_drift_decided_in_code():
    answer = {
        "frames": {
            "first_frame": _frame({"name": "cows", "present": True, "kind_matches": True}),
            "middle_frame": _frame(
                {"name": "cows", "present": True, "kind_matches": True, "anomalies": ["human hands"]},
                {"name": "sheep", "present": True, "kind_matches": False},
                excluded=["Human hands", "a dragon"],
            ),
            "last_frame": _frame({"name": "cows", "present": False, "replaced_by": "people in suits"}),
        }
    }
    found = content_drift.drifted(SUBJECTS, ["human hands"], answer)
    whats = [(f.frame, f.what) for f in found]
    assert ("middle_frame", "cows with human hands") in whats
    assert ("middle_frame", "shows human hands, which the negative prompt excludes") in whats
    assert ("last_frame", "cows replaced by people in suits") in whats
    # A subject the check did not ask about, a term the negative prompt did not name: ignored.
    assert not any("sheep" in what or "dragon" in what for _frame_name, what in whats)
    reason = content_drift.ContentCheck(True, found).reason()
    assert reason.startswith("content drift: the shot does not show what its prompt asks - ")
    # Said without a judgement of how much it shows: major, as before severities existed.
    assert {f.severity for f in found} == {content_drift.MAJOR}


def test_a_detail_seen_only_up_close_is_minor_and_the_rest_stays_major():
    answer = {
        "frames": {
            "middle_frame": _frame(
                {
                    "name": "cows",
                    "present": True,
                    "kind_matches": True,
                    "anomalies": [
                        {"what": "a hoof missing its dot of shine", "noticeable": False},
                        {"what": "a fifth leg", "noticeable": True},
                    ],
                },
                excluded=[{"term": "human hands", "noticeable": False}],
            ),
            "last_frame": _frame({"name": "cows", "present": False, "replaced_by": "people in suits"}),
        }
    }
    found = {f.what: f.severity for f in content_drift.drifted(SUBJECTS, ["human hands"], answer)}
    assert found == {
        "cows with a hoof missing its dot of shine": content_drift.MINOR,
        "cows with a fifth leg": content_drift.MAJOR,
        "shows human hands, which the negative prompt excludes": content_drift.MINOR,
        "cows replaced by people in suits": content_drift.MAJOR,
    }


def test_minor_drift_is_accepted_unless_the_shot_was_asked_strict():
    minor = content_drift.Finding("timeline", "middle_frame", "timeline markers without their dot ends", content_drift.MINOR)
    verdict = content_drift.ContentCheck(True, [minor])
    assert verdict.failing() == [] and "Minor content drift accepted" in verdict.accepted_note()
    assert verdict.failing(accept_minor=False) == [minor] and verdict.accepted_note(accept_minor=False) is None
    assert "(middle frame, minor)" in verdict.reason(accept_minor=False)


def test_a_subject_leaving_the_frame_is_no_drift_but_one_never_shown_is():
    out_then_back = {
        "frames": {
            "first_frame": _frame({"name": "cows", "present": True, "kind_matches": True}),
            "middle_frame": _frame({"name": "cows", "present": False}),
            "last_frame": _frame({"name": "cows", "present": True, "kind_matches": True}),
        }
    }
    assert content_drift.drifted(SUBJECTS, [], out_then_back) == []
    never = {"frames": {name: _frame({"name": "cows", "present": False}) for name in content_drift.FRAMES}}
    assert [f.what for f in content_drift.drifted(SUBJECTS, [], never)] == ["cows never shows"]
    turned = {"frames": {"last_frame": _frame({"name": "cows", "present": True, "kind_matches": False, "seen_as": "people"})}}
    assert [f.what for f in content_drift.drifted(SUBJECTS, [], turned)] == [
        "cows shows as people, not animals: dairy cows"
    ]
    assert content_drift.drifted(SUBJECTS, [], {"frames": "unusable"}) == []


class _Reader:
    """The person's chat model: the subjects of a prompt (text), then what three frames show (pictures)."""

    def __init__(self, subjects: str, frames: dict | None = None) -> None:
        self.subjects, self.frames, self.calls = subjects, frames, []

    async def chat(self, messages, model=None, temperature=None, max_tokens=None):
        files = messages[0].files or []
        self.calls.append(len(files))
        return SimpleNamespace(content=json.dumps(self.frames) if files else self.subjects)


@pytest.fixture
def reader(monkeypatch):
    world = SimpleNamespace(llm=None, stills=[])

    @asynccontextmanager
    async def session():
        yield SimpleNamespace()

    async def get_llm(_db, _user_id):
        return world.llm, "chat-model"

    async def extract_still(_video, *, at, out_path, max_side, crop_to=None):
        world.stills.append(at)
        Path(out_path).write_bytes(b"JPG")

    monkeypatch.setattr("app.core.database.async_session", session)
    monkeypatch.setattr("app.llm.router.get_llm_for_user", get_llm)
    monkeypatch.setattr(encoder, "extract_still", extract_still)
    return world


def _check(params: dict, *, prompt="Six real cows graze in a meadow", negative="human hands", tmp_path=None):
    return asyncio.run(
        content_drift.check(
            "u", prompt=prompt, negative_prompt=negative, video_path="shot.mp4", duration=8.0, workdir=str(tmp_path), params=params
        )
    )


def test_a_prompt_naming_no_subject_reads_no_frame(reader, tmp_path):
    reader.llm = _Reader('{"subjects": []}')
    verdict = _check({}, prompt="A slow aerial pass over misty hills", negative=None, tmp_path=tmp_path)
    assert verdict.checked and verdict.findings == [] and reader.stills == [] and reader.llm.calls == [0]


def test_the_frames_are_read_in_one_call_and_the_subjects_kept_for_the_same_prompt(reader, tmp_path):
    frames = {
        "frames": {
            "first_frame": _frame({"name": "cows", "present": True, "kind_matches": True}),
            "middle_frame": _frame({"name": "cows", "present": True, "kind_matches": True}),
            "last_frame": _frame({"name": "cows", "present": True, "kind_matches": False, "seen_as": "people"}),
        }
    }
    reader.llm = _Reader('{"subjects": [{"name": "cows", "kind": "dairy cows"}]}', frames)
    params: dict = {}
    verdict = _check(params, tmp_path=tmp_path)
    assert reader.llm.calls == [0, 3] and reader.stills == [0.0, 4.0, None]
    assert [f.what for f in verdict.findings] == ["cows shows as people, not dairy cows"]
    assert params[content_drift.SUBJECTS_KEY]["subjects"] == [{"name": "cows", "kind": "dairy cows"}]
    # Generated again with the same prompt: the subjects are not asked for again.
    _check(params, tmp_path=tmp_path)
    assert reader.llm.calls == [0, 3, 3]


def test_a_check_that_cannot_run_leaves_the_shot_with_a_note(reader, tmp_path):
    reader.llm = _Reader("not json")
    verdict = _check({}, tmp_path=tmp_path)
    assert not verdict.checked and "came back unusable" in verdict.note


@pytest.fixture
def made(monkeypatch):
    """``run_clip`` on one shot row whose video is made: the content check decides the rest."""
    row = VideoClip(
        id=uuid.uuid4(),
        user_id=USER.id,
        provider="gemini",
        model="veo-3.1-generate-preview",
        prompt="A grey heron wading at dawn",
        params={"provider_id": str(uuid.uuid4()), "duration_seconds": 8},
        status=JOB_PENDING,
    )
    world = SimpleNamespace(row=row, verdict=content_drift.ContentCheck(True), asked=None)

    class _Db:
        async def get(self, _model, _key):
            return row

        async def commit(self):
            return None

    @asynccontextmanager
    async def session():
        yield _Db()

    class _Provider:
        def takes_negative_prompt(self, model):
            return True

        async def generate_video(self, request, **_kwargs):
            world.asked = request
            return VideoResult(data=b"MP4", job_id="op/1")

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        return _Provider(), model, SimpleNamespace(id=provider_id, provider="gemini")

    async def mark(_kind, _job_id, **changes):
        for key, value in changes.items():
            setattr(row, key, value)

    async def store_path(_path, **_kwargs):
        return SimpleNamespace(id=uuid.uuid4())

    async def probe(_path):
        return SimpleNamespace(duration=8.0, width=1920, height=1080, has_audio=False)

    async def poster(_path, *, at, out_path, width):
        Path(out_path).write_bytes(b"JPG")

    async def check(*_args, **kwargs):
        world.checked = kwargs
        return world.verdict

    async def nothing(*_args, **_kwargs):
        return None

    async def text_check(*_args, **_kwargs):
        return text_drift.TextCheck(True)

    async def pictures(params, _workdir):
        return [(b"PNG", "image/png")] if params.get(clips.REFERENCE_IMAGES) else []

    monkeypatch.setattr(runners, "async_session", session)
    monkeypatch.setattr(runners, "bind_llm_caller", nothing)
    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(clips, "reference_pictures", pictures)
    monkeypatch.setattr(jobs, "mark", mark)
    monkeypatch.setattr(assets, "store_path", store_path)
    monkeypatch.setattr(assets, "delete_files", nothing)
    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(encoder, "extract_frame", poster)
    monkeypatch.setattr(content_drift, "check", check)
    monkeypatch.setattr(text_drift, "check", text_check)
    return world


def test_a_shot_whose_subject_drifted_fails_and_keeps_its_video(made):
    made.row.params[clips.REFERENCE_IMAGES] = PICTURES[:1]
    made.verdict = content_drift.ContentCheck(True, [content_drift.Finding("heron", "last_frame", "heron replaced by a crane")])
    asyncio.run(runners.run_clip(str(made.row.id)))
    row = made.row
    assert row.status == JOB_FAILED and row.error.startswith("content drift:") and "heron replaced by a crane" in row.error
    assert row.file_id is not None and row.poster_file_id is not None
    assert made.asked.reference_images == [(b"PNG", "image/png")]
    assert made.checked["prompt"] == "A grey heron wading at dawn" and made.checked["duration"] == 8.0
    assert "Asked with 1 reference image to keep." in row.params["notes"]


def test_a_shot_with_minor_drift_is_ready_with_a_note_unless_asked_strict(made):
    minor = content_drift.Finding("heron", "middle_frame", "heron with a feather out of place", content_drift.MINOR)
    made.verdict = content_drift.ContentCheck(True, [minor])
    asyncio.run(runners.run_clip(str(made.row.id)))
    assert made.row.status == JOB_READY
    assert any(note.startswith("Minor content drift accepted") for note in made.row.params["notes"])

    made.row.status, made.row.params = JOB_PENDING, {**made.row.params, content_drift.ACCEPT_MINOR_KEY: False}
    made.row.file_id = made.row.poster_file_id = made.row.provider_job_id = None
    asyncio.run(runners.run_clip(str(made.row.id)))
    assert made.row.status == JOB_FAILED and "feather out of place (middle frame, minor)" in made.row.error


def test_a_shot_asked_without_the_content_check_is_never_read(made):
    made.row.params["content_check"] = False
    made.verdict = content_drift.ContentCheck(True, [content_drift.Finding("heron", "", "heron never shows")])
    asyncio.run(runners.run_clip(str(made.row.id)))
    assert made.row.status == JOB_READY and not hasattr(made, "checked")


def test_a_content_check_that_could_not_run_leaves_the_shot_ready_with_its_note(made):
    made.verdict = content_drift.ContentCheck(False, note="The shot was not checked against its prompt: no chat model.")
    asyncio.run(runners.run_clip(str(made.row.id)))
    assert made.row.status == JOB_READY
    assert "The shot was not checked against its prompt: no chat model." in made.row.params["notes"]


# ── Image edits: reference pictures and a mask ────────────────────────────


class _ImageModel:
    def __init__(self, limit: int, mask: bool) -> None:
        self.limit, self.mask = limit, mask

    def image_reference_limit(self, model):
        return self.limit

    def image_takes_mask(self, model):
        return self.mask


@pytest.fixture
def stored(monkeypatch):
    from app.models.user import User
    from app.services import file_access
    from app.services.builtin_tools import media
    from app.services.file_storage import file_storage

    class _Db:
        async def get(self, model, key):
            if model is User:
                return SimpleNamespace(id=key, role="user")
            return FileAttachment(id=key, filename="mark.png", content_type="image/png", size=4, s3_object_name=str(key))

    @asynccontextmanager
    async def session():
        yield _Db()

    async def can_read(_row, _user, _db):
        return True

    async def download(name):
        return f"bytes of {name}".encode()

    monkeypatch.setattr(media, "_own_session", session)
    monkeypatch.setattr(file_access, "can_read_file", can_read)
    monkeypatch.setattr(file_storage, "download", download)
    return media


def test_reference_pictures_are_refused_by_the_image_models_name(stored):
    user = str(USER.id)
    with pytest.raises(stored.ImageInputError, match="imagen-4 draws from no reference picture"):
        asyncio.run(stored._image_inputs(object(), "imagen-4", user, PICTURES[:1], None))
    with pytest.raises(stored.ImageInputError, match="at most 3 reference pictures"):
        asyncio.run(stored._image_inputs(_ImageModel(3, False), "m", user, PICTURES[:4], None))
    with pytest.raises(stored.ImageInputError, match="takes no edit mask"):
        asyncio.run(stored._image_inputs(_ImageModel(3, False), "gemini-image", user, PICTURES[:1], PICTURES[4]))
    with pytest.raises(stored.ImageInputError, match="give reference_file_ids with it"):
        asyncio.run(stored._image_inputs(_ImageModel(3, True), "m", user, [], PICTURES[4]))
    pictures, mask = asyncio.run(stored._image_inputs(_ImageModel(3, True), "m", user, PICTURES[:2], PICTURES[4]))
    assert [mime for _data, mime in pictures] == ["image/png", "image/png"]
    assert mask == (f"bytes of {PICTURES[4]}".encode(), "image/png")
    assert asyncio.run(stored._image_inputs(object(), "m", user, [], None)) == ([], None)


def test_openai_edits_from_the_pictures_and_the_mask():
    from app.llm.openai_provider import OpenAIProvider

    asked: dict = {}

    async def edit(**kwargs):
        asked.update(kwargs)
        return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(b"IMG").decode())])

    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.client = SimpleNamespace(images=SimpleNamespace(edit=edit))
    assert provider.image_reference_limit("gpt-image-1") == 3 and provider.image_takes_mask("gpt-image-1")
    picture = _png()
    made = asyncio.run(
        provider.edit_image("Put the mark on the wall", [(picture, "image/png"), (picture, "image/png")], mask=(picture, "image/png"), size="1536x1024")
    )
    assert made == b"IMG" and asked["model"] == "gpt-image-1" and asked["size"] == "1536x1024"
    assert [name for name, _data, _mime in asked["image"]] == ["reference-1.png", "reference-2.png"]
    assert asked["mask"] == ("mask.png", picture, "image/png")


def test_a_gemini_image_model_draws_from_pictures_given_as_parts_and_takes_no_mask():
    from app.llm.gemini_provider import GeminiProvider

    asked: dict = {}

    async def generate_content(**kwargs):
        asked.update(kwargs)
        part = SimpleNamespace(inline_data=SimpleNamespace(data=b"IMG"))
        return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])

    provider = GeminiProvider.__new__(GeminiProvider)
    provider.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    assert provider.image_reference_limit("gemini-2.5-flash-image") == 3 and provider.image_reference_limit("imagen-4.0") == 0
    assert not provider.image_takes_mask("gemini-2.5-flash-image")
    made = asyncio.run(provider.edit_image("The same mark, embossed", [(_png(), "image/png")], model="gemini-2.5-flash-image"))
    assert made == b"IMG" and len(asked["contents"]) == 2 and asked["contents"][-1] == "The same mark, embossed"
    with pytest.raises(NotImplementedError):
        asyncio.run(provider.edit_image("x", [(_png(), "image/png")], mask=(_png(), "image/png"), model="gemini-2.5-flash-image"))
