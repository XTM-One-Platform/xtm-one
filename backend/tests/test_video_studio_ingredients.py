"""Reference ingredients of an AI shot. Synthetic pictures, prompts and ids only.

An ingredient is a picture the shot keeps exactly - a logo, a product, a
character - beside its start frame. What is proven:

* each provider adapter's own declarations decide the path: the model's own
  reference mechanism (Veo 3.1 reference images, Kling 3.0 Omni, Runway
  seedance2) when it takes them all with the shot's start frame, the prompt
  otherwise (OpenAI Sora, Seedance on BytePlus, ElevenLabs, the models of a
  native provider that take none);
* on the native path each adapter sends the synthetic pictures through its
  own field; on the prompt path the request's prompt says "keep <label>
  exactly as in the reference image", which points at the start frame - so a
  shot without one is refused by the model's name;
* the shot's params record the ingredients and the path, and a shot generated
  again keeps them, re-deciding the path for its model;
* on the prompt path a logo with a start frame is also drawn back onto the
  made shot (``composited``, ``test_video_studio_shot_rescue.py``).
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
from app.models.video_studio import JOB_PENDING, JOB_READY, VideoClip
from app.services.video_studio import assets, clips, content_drift, encoder, jobs, runners, text_drift

pytestmark = pytest.mark.usefixtures("no_default_video_template")

USER = SimpleNamespace(id=uuid.UUID("00000000-0000-0000-0000-00000000b001"), role="user")
LOGO, PRODUCT, MASCOT = (f"00000000-0000-0000-0000-0000000000e{i}" for i in range(1, 4))
START = "00000000-0000-0000-0000-0000000000f1"


def _png(colour=(30, 140, 220), size=(64, 48)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, format="PNG")
    return out.getvalue()


def _ingredients(*items: tuple[str, str, str | None]) -> list[dict]:
    return [{"file_id": fid, "role": role, **({"label": label} if label else {})} for fid, role, label in items]


LOGO_ONLY = _ingredients((LOGO, "logo", "the Filigran logo"))


class _Files:
    async def get(self, _model, key):
        return FileAttachment(id=key, filename="ingredient.png", content_type="image/png", size=10, s3_object_name="o")

    def add(self, _row):
        return None

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


# ── Which path, from each adapter's own declarations ──────────────────────


def _adapter(name: str):
    from app.llm.elevenlabs_provider import ElevenLabsProvider
    from app.llm.gemini_provider import GeminiProvider
    from app.llm.kling_provider import KlingProvider
    from app.llm.openai_provider import OpenAIProvider
    from app.llm.runway_provider import RunwayProvider
    from app.llm.seedance_provider import SeedanceProvider

    classes = {
        "gemini": GeminiProvider,
        "kling": KlingProvider,
        "runway": RunwayProvider,
        "openai": OpenAIProvider,
        "seedance": SeedanceProvider,
        "elevenlabs": ElevenLabsProvider,
    }
    # Only the declarations are read: no client is built.
    return classes[name].__new__(classes[name])


@pytest.mark.parametrize(
    ("adapter", "model", "start_frame", "path"),
    [
        ("gemini", "veo-3.1-generate-preview", False, clips.PATH_NATIVE),
        # Veo takes reference images or a start image, never both: with a start frame, the prompt.
        ("gemini", "veo-3.1-generate-preview", True, clips.PATH_PROMPT),
        ("gemini", "veo-3.0-generate-001", True, clips.PATH_PROMPT),
        ("kling", "kling-3.0-omni", True, clips.PATH_NATIVE),
        ("kling", "kling-v3", True, clips.PATH_PROMPT),
        ("runway", "seedance2", False, clips.PATH_NATIVE),
        ("runway", "seedance2", True, clips.PATH_PROMPT),
        ("runway", "gen4.5", True, clips.PATH_PROMPT),
        ("openai", "sora-2", True, clips.PATH_PROMPT),
        ("seedance", "seedance-2-0-pro", True, clips.PATH_PROMPT),
        ("elevenlabs", "veo-3.1", True, clips.PATH_PROMPT),
    ],
)
def test_each_adapter_declares_the_path_of_its_models(adapter, model, start_frame, path):
    assert clips.ingredient_path(_adapter(adapter), model, 2, start_frame=start_frame) == path


def test_a_model_that_takes_fewer_than_asked_is_told_in_the_prompt():
    class _One:
        def max_reference_images(self, _model):
            return 1

        def reference_images_with_start_frame(self, _model):
            return True

    assert clips.ingredient_path(_One(), "m", 1, start_frame=True) == clips.PATH_NATIVE
    assert clips.ingredient_path(_One(), "m", 2, start_frame=True) == clips.PATH_PROMPT


# ── What each native adapter sends ────────────────────────────────────────


def test_veo_is_sent_the_ingredients_as_asset_reference_images():
    from google.genai import types

    asked: dict = {}

    async def generate_videos(**kwargs):
        asked.update(kwargs)
        return SimpleNamespace(
            done=True,
            error=None,
            name="operations/i1",
            response=SimpleNamespace(generated_videos=[SimpleNamespace(video=SimpleNamespace(video_bytes=b"VEO"))]),
        )

    veo = _adapter("gemini")
    veo.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_videos=generate_videos)))
    logo, product = _png((250, 80, 0)), _png((0, 90, 200))
    request = VideoRequest(
        model="veo-3.1-generate-preview", prompt="A booth at dusk", reference_images=[(logo, "image/png"), (product, "image/png")]
    )
    asyncio.run(veo.generate_video(request))
    sent = asked["config"].reference_images
    assert [ref.image.image_bytes for ref in sent] == [logo, product]
    assert {ref.reference_type for ref in sent} == {types.VideoGenerationReferenceType.ASSET}
    assert asked["prompt"] == "A booth at dusk"


_REAL_CLIENT = httpx.AsyncClient
_SIGNED = "https://storage.example.com/made.mp4?signature=abc"


@pytest.fixture
def api(monkeypatch):
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


def test_kling_omni_is_sent_the_ingredients_beside_the_start_frame(api):
    from app.llm.kling_provider import KlingProvider

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "storage.example.com":
            return httpx.Response(200, content=b"MP4", headers={"content-type": "video/mp4"})
        if request.method == "POST":
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"code": 0, "data": {"id": "k1"}})
        outputs = [{"type": "video", "url": _SIGNED}]
        return httpx.Response(200, json={"code": 0, "data": [{"id": "k1", "status": "succeeded", "outputs": outputs}]})

    api(handler)
    start, logo = _png((5, 5, 5)), _png((240, 120, 0))
    request = VideoRequest(
        model="kling-3.0-omni",
        prompt="The mascot waves",
        reference_image=start,
        reference_mime="image/png",
        reference_images=[(logo, "image/png")],
    )
    asyncio.run(KlingProvider(api_key="k").generate_video(request))
    contents = seen["body"]["contents"]
    assert [c["type"] for c in contents] == ["prompt", "first_frame", "refer_image"]
    assert contents[2]["url"] == base64.b64encode(logo).decode("ascii")


def test_runway_seedance2_is_sent_the_ingredients_as_references(api):
    from app.llm.runway_provider import RunwayProvider

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "storage.example.com":
            return httpx.Response(200, content=b"MP4", headers={"content-type": "video/mp4"})
        if request.method == "POST":
            seen["path"], seen["body"] = request.url.path, json.loads(request.content)
            return httpx.Response(200, json={"id": "t1"})
        return httpx.Response(200, json={"id": "t1", "status": "SUCCEEDED", "output": [_SIGNED]})

    api(handler)
    logo, mascot = _png((240, 120, 0)), _png((90, 200, 90))
    request = VideoRequest(
        model="seedance2",
        prompt="The mascot waves the flag",
        duration_seconds=6,
        resolution="720p",
        reference_images=[(logo, "image/png"), (mascot, "image/png")],
    )
    result = asyncio.run(RunwayProvider(api_key="key_x").generate_video(request))
    assert result.data == b"MP4"
    assert seen["path"] == "/v1/text_to_video"
    assert seen["body"]["references"] == [
        {"type": "image", "uri": f"data:image/png;base64,{base64.b64encode(logo).decode('ascii')}"},
        {"type": "image", "uri": f"data:image/png;base64,{base64.b64encode(mascot).decode('ascii')}"},
    ]
    assert "promptImage" not in seen["body"] and seen["body"]["duration"] == 6


def test_a_runway_model_without_references_refuses_them():
    from app.llm.runway_provider import RunwayProvider

    asked = VideoRequest(model="gen4.5", prompt="x", reference_images=[(_png(), "image/png")])
    with pytest.raises(VideoGenerationError, match=r"gen4\.5 takes no reference images: seedance2 does"):
        RunwayProvider._video_body(asked, [])
    both = VideoRequest(
        model="seedance2", prompt="x", reference_image=_png(), reference_mime="image/png", reference_images=[(_png(), "image/png")]
    )
    with pytest.raises(VideoGenerationError, match="reference images or a start frame, not both"):
        RunwayProvider._video_body(both, [])


# ── What the shot records, and what the request says ──────────────────────


def _resolving(monkeypatch, provider, *, start_frame: bool) -> None:
    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        return provider, model or "sora-2", SimpleNamespace(id=uuid.uuid4(), provider="openai")

    async def frame(*_args, **_kwargs):
        return {"reference_file_id": START} if start_frame else {}

    async def enqueue(*_args, **_kwargs):
        return None

    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(clips, "resolve_reference", frame)
    monkeypatch.setattr(jobs, "enqueue", enqueue)


def test_ingredients_are_checked_field_by_field():
    with pytest.raises(clips.ClipError, match="role is one of logo, product, character"):
        clips.parse_ingredients([{"file_id": LOGO, "role": "background"}])
    with pytest.raises(clips.ClipError, match="names its picture"):
        clips.parse_ingredients([{"role": "logo"}])
    with pytest.raises(clips.ClipError, match="at most 3 ingredients"):
        clips.parse_ingredients(_ingredients(*((LOGO, "logo", None),) * 4))
    with pytest.raises(clips.ClipError, match="different picture"):
        clips.parse_ingredients(_ingredients((LOGO, "logo", None), (LOGO, "product", None)))
    parsed = clips.parse_ingredients(_ingredients((f"[[FILE:{LOGO}]]", "logo", "  the   Filigran logo ")))
    assert parsed == [clips.Ingredient(LOGO, "logo", "the Filigran logo")]


def test_on_the_prompt_path_the_prompt_names_each_ingredient():
    named = clips.ingredient_prompt(
        "A booth at dusk, the screen glowing.",
        [clips.Ingredient(LOGO, "logo", "the Filigran logo"), clips.Ingredient(PRODUCT, "product")],
    )
    assert named == (
        "A booth at dusk, the screen glowing. Keep the Filigran logo exactly as in the reference image. "
        "Keep the product exactly as in the reference image."
    )


def test_a_native_model_records_the_path_and_sends_them_as_its_reference_images(monkeypatch, readable):
    _resolving(monkeypatch, _adapter("kling"), start_frame=True)
    clip, _label = asyncio.run(
        clips.start_clip(
            _Files(),
            USER,
            model="kling-3.0-omni",
            prompt="The mascot waves",
            ingredients=_ingredients((LOGO, "logo", None), (MASCOT, "character", "the mascot")),
        )
    )
    assert clip.params[clips.INGREDIENTS_PATH] == clips.PATH_NATIVE
    assert clip.params[clips.INGREDIENTS] == [
        {"file_id": LOGO, "role": "logo"},
        {"file_id": MASCOT, "role": "character", "label": "the mascot"},
    ]
    assert clip.params[clips.REFERENCE_IMAGES] == [LOGO, MASCOT]


def test_a_model_without_references_names_them_in_the_prompt_or_refuses_without_a_start_frame(monkeypatch, readable):
    _resolving(monkeypatch, _adapter("openai"), start_frame=True)
    clip, _label = asyncio.run(clips.start_clip(_Files(), USER, prompt="A booth at dusk", ingredients=LOGO_ONLY))
    # A logo is named in the prompt and drawn back onto the made shot.
    assert clip.params[clips.INGREDIENTS_PATH] == clips.PATH_COMPOSITED
    assert clips.REFERENCE_IMAGES not in clip.params
    assert clip.prompt == "A booth at dusk"
    assert clips.reference_note(clip.params).startswith("Ingredients (the Filigran logo) named in the prompt, the logo drawn back")
    # Without a logo, the ingredients are only named.
    product = [{"file_id": PRODUCT, "role": "product"}]
    clip, _label = asyncio.run(clips.start_clip(_Files(), USER, prompt="A booth at dusk", ingredients=product))
    assert clip.params[clips.INGREDIENTS_PATH] == clips.PATH_PROMPT
    assert clips.reference_note(clip.params).startswith("Ingredients (the product) named in the prompt:")
    _resolving(monkeypatch, _adapter("openai"), start_frame=False)
    with pytest.raises(clips.ClipError, match="sora-2 takes no reference images for this shot") as refused:
        asyncio.run(clips.start_clip(_Files(), USER, prompt="A booth at dusk", ingredients=LOGO_ONLY))
    assert refused.value.about_model
    with pytest.raises(clips.ClipError, match="not both"):
        asyncio.run(
            clips.start_clip(_Files(), USER, prompt="x", ingredients=LOGO_ONLY, reference_image_file_ids=[PRODUCT])
        )


def test_a_shot_generated_again_keeps_its_ingredients_and_re_decides_the_path(monkeypatch, readable):
    _resolving(monkeypatch, _adapter("kling"), start_frame=True)
    clip, _label = asyncio.run(clips.start_clip(_Files(), USER, model="kling-3.0-omni", prompt="x", ingredients=LOGO_ONLY))
    clip.status = JOB_READY
    # Generated again with a model that takes none: the same ingredients, now named in the prompt (a logo drawn back).
    asyncio.run(clips.restart_clip(_Files(), USER, clip, model="kling-v3"))
    assert clip.params[clips.INGREDIENTS_PATH] == clips.PATH_COMPOSITED and clips.REFERENCE_IMAGES not in clip.params
    assert clip.params[clips.INGREDIENTS] == [{"file_id": LOGO, "role": "logo", "label": "the Filigran logo"}]
    clip.status = JOB_READY
    asyncio.run(clips.restart_clip(_Files(), USER, clip, model="kling-3.0-omni"))
    assert clip.params[clips.INGREDIENTS_PATH] == clips.PATH_NATIVE and clip.params[clips.REFERENCE_IMAGES] == [LOGO]
    # An empty list drops them, reference images with them.
    clip.status = JOB_READY
    asyncio.run(clips.restart_clip(_Files(), USER, clip, ingredients=[]))
    assert not {clips.INGREDIENTS, clips.INGREDIENTS_PATH, clips.REFERENCE_IMAGES} & set(clip.params)
    assert clip.status == JOB_PENDING


def test_a_shot_generated_again_takes_its_ingredients_wherever_its_start_frame_goes(monkeypatch, readable):
    _resolving(monkeypatch, _adapter("kling"), start_frame=True)
    clip, _label = asyncio.run(
        clips.start_clip(
            _Files(), USER, model="kling-3.0-omni", prompt="x", ingredients=LOGO_ONLY, min_reference_similarity=0.6
        )
    )
    assert clip.params[clips.REFERENCE_IMAGES] == [LOGO] and clip.params[clips.MIN_REFERENCE_SIMILARITY] == 0.6
    # A model taking reference images alone: the ingredients' pictures are not taken for reference images
    # that would drop the start frame - the frame stays and the ingredients go in the prompt.
    clip.status = JOB_READY
    _resolving(monkeypatch, _adapter("gemini"), start_frame=True)
    plan = asyncio.run(clips.restart_clip(_Files(), USER, clip, model="veo-3.1-generate-preview"))
    # Its start frame is one flat blue surface, and no scene draws a screen insert on the shot: not checked.
    assert plan.notes == [f"{clips.NOT_MEASURED}."]
    assert clip.params["reference_file_id"] == START and clip.params[clips.MIN_REFERENCE_SIMILARITY] == 0.6
    assert clip.params[clips.INGREDIENTS_PATH] == clips.PATH_COMPOSITED and clips.REFERENCE_IMAGES not in clip.params
    # Its start frame dropped: the same model takes the pictures, and the threshold has nothing left to compare.
    clip.status = JOB_READY
    plan = asyncio.run(clips.restart_clip(_Files(), USER, clip, start_frame=False))
    assert clip.params["reference_file_id"] is None
    assert clip.params[clips.INGREDIENTS_PATH] == clips.PATH_NATIVE and clip.params[clips.REFERENCE_IMAGES] == [LOGO]
    assert clips.MIN_REFERENCE_SIMILARITY not in clip.params
    assert any(note.startswith("min_reference_similarity dropped") for note in plan.notes)
    # validate_only answers the request as it would be made, its ingredients and their path with it.
    clip.status = JOB_READY
    summary = asyncio.run(clips.plan_restart(_Files(), USER, clip)).summary()
    assert summary["ingredients"] == LOGO_ONLY and summary["ingredients_path"] == clips.PATH_NATIVE


@pytest.fixture
def made(monkeypatch):
    """``run_clip`` on one shot row: what the provider is asked."""
    row = VideoClip(
        id=uuid.uuid4(),
        user_id=USER.id,
        provider="openai",
        model="sora-2",
        prompt="A booth at dusk",
        params={"provider_id": str(uuid.uuid4()), "duration_seconds": 8, "content_check": False},
        status=JOB_PENDING,
    )
    world = SimpleNamespace(row=row, asked=None)

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
            world.asked = request
            return VideoResult(data=b"MP4", job_id="v1")

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        return _Provider(), model, SimpleNamespace(id=provider_id, provider="openai")

    async def mark(_kind, _job_id, **changes):
        for key, value in changes.items():
            setattr(row, key, value)

    async def store_path(_path, **_kwargs):
        return SimpleNamespace(id=uuid.uuid4())

    async def probe(_path):
        return SimpleNamespace(duration=8.0, width=1280, height=720, has_audio=False)

    async def poster(_path, *, at, out_path, width):
        Path(out_path).write_bytes(b"JPG")

    async def nothing(*_args, **_kwargs):
        return None

    async def pictures(params, _workdir):
        return [(_png(), "image/png") for _fid in params.get(clips.REFERENCE_IMAGES) or []]

    async def text_check(*_args, **_kwargs):
        return text_drift.TextCheck(True)

    monkeypatch.setattr(runners, "async_session", session)
    monkeypatch.setattr(runners, "bind_llm_caller", nothing)
    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(clips, "reference_pictures", pictures)
    monkeypatch.setattr(clips, "start_frame", nothing)
    monkeypatch.setattr(jobs, "mark", mark)
    monkeypatch.setattr(assets, "store_path", store_path)
    monkeypatch.setattr(assets, "delete_files", nothing)
    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(encoder, "extract_frame", poster)
    monkeypatch.setattr(text_drift, "check", text_check)
    monkeypatch.setattr(content_drift, "check", nothing)
    return world


def test_the_prompt_path_tells_the_model_and_the_native_path_sends_the_pictures(made):
    made.row.params.update({clips.INGREDIENTS: LOGO_ONLY, clips.INGREDIENTS_PATH: clips.PATH_PROMPT})
    asyncio.run(runners.run_clip(str(made.row.id)))
    assert made.asked.prompt == "A booth at dusk Keep the Filigran logo exactly as in the reference image."
    assert made.asked.reference_images == []
    # The shot keeps the prompt as asked: the sentence is the request's.
    assert made.row.prompt == "A booth at dusk"
    assert any(note.startswith("Ingredients (the Filigran logo) named in the prompt") for note in made.row.params["notes"])

    made.row.status, made.row.file_id, made.row.provider_job_id = JOB_PENDING, None, None
    made.row.params = {
        "provider_id": str(uuid.uuid4()),
        "content_check": False,
        clips.INGREDIENTS: LOGO_ONLY,
        clips.INGREDIENTS_PATH: clips.PATH_NATIVE,
        clips.REFERENCE_IMAGES: [LOGO],
    }
    asyncio.run(runners.run_clip(str(made.row.id)))
    assert made.asked.prompt == "A booth at dusk" and len(made.asked.reference_images) == 1
