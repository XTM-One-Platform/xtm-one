"""A model the provider no longer serves, and a frozen picture read part by part. Synthetic frames and models.

* a provider that answers HTTP 404 for model B - to the probe that makes no
  video, or to a generation - has B recorded as not served: the listing
  ``video_studio_capabilities`` shows drops it and says why, and a shot asked
  with model A and ``fallback_models: [B]`` is refused at ``validate_only``
  and on the real call, naming B, before anything is paid;
* a 404 met polling a job that exists is about the job, never the model;
* ``frozen``: a clip's footage and each screen insert it shows are watched
  apart - a flickering candle in a locked-off room under a static page is
  not frozen, while the same footage frozen from 3 s is, naming the footage;
  a still room under a page that scrolls is not, and a page that stops under
  a still room is, naming the insert.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import numpy as np
import pytest

from app.llm import provider_access, router, video_served
from app.llm.video import VideoAPIError, VideoGenerationError, VideoModelNotServedError, VideoResult, status_error
from app.models.user import User
from app.models.video_studio import JOB_FAILED, JOB_PENDING, VideoClip
from app.services.video_studio import assets, clips, encoder, frame_freeze, jobs, runners

pytestmark = pytest.mark.usefixtures("no_default_video_template")

USER = SimpleNamespace(id=uuid.UUID("00000000-0000-0000-0000-000000006338"), role="user", email="maker@example.com")

# ── A model the provider no longer serves ────────────────────────────────


class _Provider:
    """A video provider listing a catalog of its own; its API answers HTTP 404 for the models in *gone*.

    *probes*: it has a read that makes no video (``probe_video_model``); without, only a generation tells.
    """

    provider_name = "kling"
    provider_config_label = "Kling AI"

    def __init__(self, models=("model-a", "model-b"), gone=("model-b",), probes=True):
        self.models, self.gone, self.probes = list(models), set(gone), probes
        self.probed: list[str] = []
        self.generated: list[str] = []

    async def list_video_models(self):
        return list(self.models)

    async def video_model_unavailable(self, _model):
        return None

    async def probe_video_model(self, model):
        if not self.probes:
            raise NotImplementedError
        self.probed.append(model)
        if model in self.gone:
            raise VideoAPIError(404, f"model {model} not found")

    async def generate_video(self, request, **_kwargs):
        self.generated.append(request.model)
        if request.model in self.gone:
            raise status_error(404, VideoAPIError(404, f"model {request.model} not found"), request.model)
        return VideoResult(data=b"MP4", job_id="job-1")

    def max_reference_images(self, _model):
        return 0

    def reference_images_with_start_frame(self, _model):
        return True

    def takes_negative_prompt(self, _model):
        return False

    def video_guidance(self, _model):
        return None

    def holds_locked_camera(self, _model, *, start_frame):
        return None


class _Redis:
    """The few Redis commands the record uses, in memory."""

    def __init__(self):
        self.data: dict[str, str] = {}

    async def mget(self, keys):
        return [self.data.get(key) for key in keys]

    async def set(self, key, value, ex=None):
        self.data[key] = value
        return True

    async def delete(self, *keys):
        return sum(self.data.pop(key, None) is not None for key in keys)


class _Db:
    """The provider configurations every query of the router reads, and the shot row the worker reads."""

    def __init__(self, configs, row=None):
        self.configs, self.row = configs, row

    async def execute(self, _statement):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(self.configs)))

    async def get(self, model, _key, **_kwargs):
        return USER if model is User else self.row

    async def commit(self):
        return None

    async def flush(self):
        return None

    async def rollback(self):
        return None

    def add(self, _row):
        return None


@pytest.fixture
def world(monkeypatch):
    """One provider configuration (Kling AI) behind the real router, the record in an in-memory Redis."""
    config = SimpleNamespace(id=uuid.uuid4(), provider="kling", display_name="Kling AI")
    world = SimpleNamespace(config=config, provider=_Provider(), redis=_Redis(), db=_Db([config]))

    async def platform_settings(_db):
        return SimpleNamespace(video_provider_id=None, video_model=None)

    async def cached(_config):
        return world.provider

    async def allowed(*_args, **_kwargs):
        return None

    async def audience(*_args, **_kwargs):
        return provider_access.NO_ACCOUNT

    async def client():
        return world.redis

    monkeypatch.setattr(router, "_get_platform_settings", platform_settings)
    monkeypatch.setattr(router, "_cached_provider", cached)
    monkeypatch.setattr(router, "ensure_not_excluded", allowed)
    monkeypatch.setattr(provider_access, "load_provider_audience", audience)
    monkeypatch.setattr(video_served, "redis_client", client)
    return world


def _plan(world, **asked):
    return asyncio.run(clips.plan_clip(world.db, USER, prompt="A cat sitting by a candle", freestyle=True, **asked))


def test_a_fallback_the_provider_answers_404_for_is_refused_at_validate_naming_it(world):
    with pytest.raises(clips.ClipError) as refused:
        _plan(world, model="model-a", fallback_models=["model-b"])
    assert str(refused.value).startswith(
        "fallback_models: fallback model-b is not served by Kling AI (HTTP 404: model model-b not found)"
    )
    assert refused.value.about_model and world.provider.generated == []
    # Recorded for every worker and the API: the next listing no longer offers it, and says why.
    served, not_served = asyncio.run(router.video_model_listing(world.db, user_id=USER.id))
    assert [entry["model"] for entry in served] == ["model-a"]
    assert [(entry["model"], entry["label"]) for entry in not_served] == [("model-b", "Kling AI")]
    assert not_served[0]["reason"].startswith("model-b is not served by Kling AI (HTTP 404")
    assert [entry["model"] for entry in asyncio.run(router.list_video_models_for(world.db, user_id=USER.id))] == ["model-a"]
    # A fallback it serves is kept; the shot's own model named alone is refused the same way.
    world.provider.models.append("model-c")
    plan = _plan(world, model="model-a", fallback_models=["model-c"])
    assert [item["model"] for item in plan.params[clips.FALLBACK_MODELS]] == ["model-c"]
    with pytest.raises(clips.ClipError, match=r"^model-b is not served by Kling AI \(HTTP 404"):
        _plan(world, model="model-b")


def _tool(world, monkeypatch):
    """``generate_video_clip`` as the assistant calls it on the world's session: ``call(**arguments) -> head``."""
    from app.services.builtin_tools import video_studio as tool
    from app.tools.base import ToolCall

    @asynccontextmanager
    async def session():
        yield world.db

    async def find_user(_db, _user_id):
        return USER

    async def enqueue(*_args, **_kwargs):
        world.enqueued = True

    world.enqueued = False
    monkeypatch.setattr(tool, "async_session", session)
    monkeypatch.setattr(tool, "_user", find_user)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    _definitions, executor = tool.build_video_studio_tools(None, str(USER.id))

    def call(**arguments):
        answer = asyncio.run(executor.execute_tool(ToolCall(id="c1", name="generate_video_clip", arguments=arguments)))
        assert not answer.is_error, answer.content
        return json.loads(answer.content.splitlines()[0])

    return call


def test_validate_only_and_the_real_call_refuse_it_before_anything_is_paid(world, monkeypatch):
    call = _tool(world, monkeypatch)
    for validate_only in (True, False):
        head = call(prompt="A cat sitting by a candle", model="model-a", fallback_models=["model-b"], validate_only=validate_only)
        assert head["status"] == "invalid_parameters" and head["queued"] is False
        assert "fallback model-b is not served by Kling AI" in head["reason"]
    assert world.enqueued is False and world.provider.generated == []


def test_a_generation_answering_404_records_the_model_and_the_next_listing_drops_it(world, monkeypatch):
    # No probe: the catalog is what the provider lists, until a generation answers 404.
    world.provider = _Provider(probes=False)
    served, _not_served = asyncio.run(router.video_model_listing(world.db, user_id=USER.id))
    assert [entry["model"] for entry in served] == ["model-a", "model-b"]
    row = VideoClip(
        id=uuid.uuid4(),
        user_id=USER.id,
        provider="kling",
        model="model-b",
        prompt="A cat sitting by a candle",
        params={"provider_id": str(world.config.id), "duration_seconds": 8},
        status=JOB_PENDING,
    )
    world.db.row = row

    @asynccontextmanager
    async def session():
        yield world.db

    async def mark(_kind, _job_id, **changes):
        for key, value in changes.items():
            setattr(row, key, value)

    async def nothing(*_args, **_kwargs):
        return None

    async def no_pictures(*_args, **_kwargs):
        return []

    monkeypatch.setattr(runners, "async_session", session)
    monkeypatch.setattr(runners, "bind_llm_caller", nothing)
    monkeypatch.setattr(clips, "start_frame", nothing)
    monkeypatch.setattr(clips, "reference_pictures", no_pictures)
    monkeypatch.setattr(jobs, "mark", mark)
    monkeypatch.setattr(assets, "delete_files", nothing)
    asyncio.run(runners.run_clip(str(row.id)))
    assert world.provider.generated == ["model-b"] and row.status == JOB_FAILED
    assert "HTTP 404" in row.error
    # The worker recorded it: the listing drops it, and a shot naming it is refused before it is paid.
    served, not_served = asyncio.run(router.video_model_listing(world.db, user_id=USER.id))
    assert [entry["model"] for entry in served] == ["model-a"] and [e["model"] for e in not_served] == ["model-b"]
    with pytest.raises(clips.ClipError, match=r"^fallback_models: fallback model-b is not served by Kling AI"):
        _plan(world, model="model-a", fallback_models=["model-b"])
    # A job the provider then creates for it says it serves it again.
    asyncio.run(video_served.clear(world.config.id, "model-b"))
    assert [entry["model"] for entry in asyncio.run(router.list_video_models_for(world.db, user_id=USER.id))] == [
        "model-a",
        "model-b",
    ]


def test_a_404_polling_a_job_is_about_the_job_never_the_model():
    created = status_error(404, VideoAPIError(404, "model not found"), "model-b")
    polled = status_error(404, VideoAPIError(404, "task not found"), "model-b", about_job=True)
    assert isinstance(created, VideoModelNotServedError) and created.detail == "model not found"
    assert isinstance(polled, VideoGenerationError) and not isinstance(polled, VideoModelNotServedError)


def test_geminis_probe_reads_the_models_own_record():
    from google.genai import errors as genai_errors

    from app.llm.gemini_provider import GeminiProvider

    async def get(*, model):
        if model.startswith("veo-2"):
            raise genai_errors.ClientError(404, {"error": {"code": 404, "message": f"models/{model} is not found"}})
        return SimpleNamespace(name=f"models/{model}")

    gemini = GeminiProvider.__new__(GeminiProvider)
    gemini.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(get=get)))
    config = SimpleNamespace(id=uuid.uuid4(), provider="gemini", display_name=None)
    reasons = asyncio.run(video_served.problems(gemini, config, ["veo-3.1-generate-preview", "veo-2.0-generate-001"]))
    assert list(reasons) == ["veo-2.0-generate-001"]
    assert reasons["veo-2.0-generate-001"].startswith("veo-2.0-generate-001 is not served by gemini (HTTP 404")


# ── A frozen picture, part by part ────────────────────────────────────────

FPS = 30.0
W, H = 192, 108
#: A screen on the room's wall, a static page drawn into it (``quad``: 0-1 of the footage, which covers the frame).
QUAD = [[0.38, 0.1], [0.96, 0.1], [0.96, 0.9], [0.38, 0.9]]
DOCUMENT = {
    "duration": 8.0,
    "scenes": [
        {
            "id": "room",
            "type": "clip",
            "start": 0.0,
            "end": 8.0,
            "src_w": 1920,
            "src_h": 1080,
            "extend": {"mode": "zoom"},
            "inserts": [{"src": "insert/room.0", "kind": "image", "quad": QUAD, "t0": 0.0, "t1": 8.0}],
        }
    ],
}
SCREEN = (slice(round(0.1 * H) + 1, round(0.9 * H) - 1), slice(round(0.38 * W) + 1, round(0.96 * W) - 1))
#: A candle's flame in the room, left of the screen: 16 pixels of the copy, flickering by 5 grey levels.
FLAME = (slice(60, 64), slice(24, 28))


def _room() -> np.ndarray:
    rng = np.random.default_rng(1)
    return np.kron(rng.uniform(40, 90, (H // 6, W // 6)), np.ones((6, 6)))


def _page() -> np.ndarray:
    """A page of text lines, as long as the screen plus the 8 s it may scroll, a line a frame."""
    rng = np.random.default_rng(2)
    rows = SCREEN[0].stop - SCREEN[0].start + int(8 * FPS)
    lines = np.where(rng.uniform(0, 1, (rows, 1)) > 0.5, 230.0, 60.0)
    return np.repeat(lines, SCREEN[1].stop - SCREEN[1].start, axis=1)


def _frames(*, flame_until: float = 8.0, scroll_until: float = 0.0) -> list[bytes]:
    """8 s of the room: the flame flickers until *flame_until*, then holds; the page scrolls until *scroll_until*."""
    room, page = _room(), _page()
    rows = SCREEN[0].stop - SCREEN[0].start
    frames = []
    for n in range(int(8 * FPS)):
        t = n / FPS
        picture = room.copy()
        lit = min(t, flame_until) * FPS
        picture[FLAME] = 200.0 + 5.0 * np.sin(lit * 1.3)
        offset = int(min(t, scroll_until) * FPS)
        picture[SCREEN] = page[offset : offset + rows]
        frames.append(np.clip(np.round(picture), 0, 255).astype(np.uint8))
    return [frame.tobytes() for frame in frames]


def _check(monkeypatch, frames: list[bytes]) -> frame_freeze.FrameCheck:
    async def stream(_path, *, width, height, fps, start=0.0):
        assert (width, height, fps, start) == (W, H, FPS, 0.0)
        for frame in frames:
            yield frame

    monkeypatch.setattr(encoder, "iter_gray_frames", stream)
    return asyncio.run(frame_freeze.check_frames("final.mp4", document=DOCUMENT, width=1920, height=1080, fps=30))


def test_a_flickering_candle_under_a_static_page_is_never_frozen(monkeypatch):
    frames = _frames()
    assert _check(monkeypatch, frames).findings == []
    # Read on the whole frame, as before: the page dilutes the flame's flicker, frozen all through.
    whole = [np.frombuffer(raw, dtype=np.uint8).reshape(H, W) for raw in frames]
    assert frame_freeze.frozen_runs(whole, FPS, takes=["room"] * len(whole)) == [(0, len(whole))]


def test_the_same_footage_frozen_from_3_s_under_its_static_page_is_frozen_naming_the_footage(monkeypatch):
    [finding] = _check(monkeypatch, _frames(flame_until=3.0)).findings
    assert (finding.kind, finding.scene_id, finding.at, finding.end) == ("frozen", "room", 3.0, 8.0)
    assert "in a clip: its footage stops moving while its screen insert 0 is still: give its footage more length" in finding.message
    assert finding.data["still"] == ["footage", "insert/room.0"] and finding.data["stopped"] == ["footage"]


def test_a_still_room_under_a_page_that_scrolls_is_not_frozen_until_the_page_stops(monkeypatch):
    still_room = _frames(flame_until=0.0, scroll_until=8.0)
    assert _check(monkeypatch, still_room).findings == []
    [finding] = _check(monkeypatch, _frames(flame_until=0.0, scroll_until=3.0)).findings
    assert (finding.at, finding.end) == (3.0, 8.0)
    assert "its screen insert 0 stops moving while its footage is still: its footage holds still under it" in finding.message
    assert finding.data["stopped"] == ["insert/room.0"]


def test_the_parts_are_placed_where_the_page_draws_them():
    [(footage, room), (key, screen)] = frame_freeze.frame_parts(DOCUMENT["scenes"][0], 1.0, (W, H))
    assert (footage, key) == ("footage", "insert/room.0") and not (room & screen).any() and (room | screen).all()
    assert screen[SCREEN].all() and not screen[FLAME].any()
    # Outside its time on screen, or in a scene that is no clip, the frame is one part.
    assert frame_freeze.frame_parts({**DOCUMENT["scenes"][0], "inserts": [{**DOCUMENT["scenes"][0]["inserts"][0], "t1": 0.5}]}, 1.0, (W, H)) is None
    assert frame_freeze.frame_parts({**DOCUMENT["scenes"][0], "type": "recording"}, 1.0, (W, H)) is None
    # A clip of another shape stands in the frame clear of its edges, its insert on it.
    assert frame_freeze.footage_box({"src_w": 1080, "src_h": 1920}, 1920.0, 1080.0) == pytest.approx(
        (960 - 0.82 * 1080 * 1080 / 1920 / 2, 0.09 * 1080, 0.82 * 1080 * 1080 / 1920, 0.82 * 1080)
    )
