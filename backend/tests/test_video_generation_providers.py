"""Video generation on ElevenLabs, Kling AI, Runway and Seedance, and ElevenLabs images.

Each provider's own job API mocked at the HTTP layer: the request each model
is sent (its lengths, resolutions, negative prompt, sound and start image),
the job handed out the moment it exists and resumed later, the polling to the
end, the refusals that are asked again and the ones that are not, and the
signed download - through ``web_fetch.fetch_url_bytes`` (the outbound policy,
the storage host resolved to a public address), never carrying the key.
"""

import asyncio
import base64
import io
import json

import httpx
import pytest

from app.llm import router
from app.llm.elevenlabs_provider import ElevenLabsProvider
from app.llm.images import ImageGenerationError, accepted_image, nearest_aspect_ratio
from app.llm.kling_provider import KlingProvider
from app.llm.runway_provider import RunwayProvider
from app.llm.seedance_provider import SeedanceProvider
from app.llm.video import (
    VideoAPIError,
    VideoGenerationError,
    VideoProviderBusyError,
    VideoRequest,
    download_video,
    fetch_signed_media,
)

_REAL_CLIENT = httpx.AsyncClient
#: The host of the signed URLs providers answer with in these tests.
_STORAGE = "storage.example.com"
_SIGNED = "https://storage.example.com/made.mp4?signature=abc"
VIDEO = b"\x00\x00\x00\x18ftypmp42-video"
PNG = b"\x89PNG\r\n\x1a\n-start-frame"


def _mock_client(handler):
    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_CLIENT(*args, **kwargs)

    return _factory


@pytest.fixture
def api(monkeypatch):
    """Route every client to a handler, poll without waiting, resolve every host to a public address."""
    from app.llm import elevenlabs_provider, video
    from app.services import web_fetch

    monkeypatch.setattr(video, "POLL_SECONDS", 0)
    monkeypatch.setattr(elevenlabs_provider, "_IMAGE_POLL_SECONDS", 0)
    monkeypatch.setattr(web_fetch, "resolve_pinned_address", lambda host, port=None: "93.184.216.34")

    def install(handler):
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client(handler))

    return install


def _storage(seen: dict, request: httpx.Request) -> httpx.Response:
    seen["download"] = request
    return httpx.Response(200, content=VIDEO, headers={"content-type": "video/mp4"})


def _assert_downloaded_without_credentials(seen: dict) -> None:
    download = seen["download"]
    assert download.url.host == _STORAGE and download.url.params["signature"] == "abc"
    assert not {"xi-api-key", "authorization", "x-runway-version"} & {name.lower() for name in download.headers}


def _collect():
    jobs: list[str] = []

    async def on_job(job: str) -> None:
        jobs.append(job)

    return jobs, on_job


# ── Registration ──────────────────────────────────────────────────────────


def test_kling_runway_and_seedance_serve_video_alone_and_generate_first():
    from app.api.v1 import settings as settings_api

    for name in ("kling", "runway", "seedance"):
        assert router.provider_capabilities(name) == ["video"]
        assert name in router.CHATLESS_PROVIDERS and name in router.VIDEO_DEFAULTS
    assert router.VIDEO_DEFAULTS["runway"] == "gen4.5"
    # Added for its voices, ElevenLabs never becomes the platform's video generator on its own.
    assert "elevenlabs" not in router.VIDEO_DEFAULTS
    assert set(router._VIDEO_PROVIDERS[:3]) == {"kling", "runway", "seedance"}
    assert settings_api.PROVIDER_MAP == router.PROVIDER_MAP


@pytest.mark.parametrize("name", sorted(router.PROVIDER_MAP))
def test_every_video_provider_takes_the_whole_generate_video_contract(name):
    """The clip runner passes every keyword of ``BaseLLMProvider.generate_video``."""
    import inspect

    from app.llm.base import BaseLLMProvider

    cls = router.PROVIDER_MAP[name]
    if cls.generate_video is BaseLLMProvider.generate_video:
        pytest.skip(f"{name} makes no video")
    expected = set(inspect.signature(BaseLLMProvider.generate_video).parameters) - {"self", "request"}
    assert expected <= set(inspect.signature(cls.generate_video).parameters)


def test_a_job_says_where_it_stands_at_every_poll(api):
    from app.llm.video import STATE_RATE_LIMITED

    answers = iter(
        [
            httpx.Response(200, json={"code": 0, "data": {"task_id": "t5", "task_status": "submitted"}}),
            httpx.Response(429, json={"code": 1302, "message": "Rate limit exceeded"}),
            httpx.Response(200, json={"code": 0, "data": {"task_id": "t5", "task_status": "processing"}}),
            httpx.Response(
                200,
                json={"code": 0, "data": {"task_id": "t5", "task_status": "succeed", "task_result": {"videos": [{"url": _SIGNED}]}}},
            ),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == _STORAGE:
            return httpx.Response(200, content=VIDEO)
        if request.method == "POST":
            return httpx.Response(200, json={"code": 0, "data": {"task_id": "t5"}})
        return next(answers)

    api(handler)
    states: list[tuple[str, int | None]] = []

    async def on_state(state) -> None:
        states.append((state.state, state.progress))

    asyncio.run(KlingProvider(api_key="k").generate_video(VideoRequest(model="kling-v3", prompt="x"), on_state=on_state))
    assert states == [
        ("queued", None),
        ("queued", None),
        (STATE_RATE_LIMITED, None),
        ("processing", None),
        ("completed", 100),
    ]


# ── ElevenLabs ────────────────────────────────────────────────────────────


def test_elevenlabs_makes_a_video_through_its_image_and_video_api(api):
    seen: dict = {"polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == _STORAGE:
            return _storage(seen, request)
        assert request.headers["xi-api-key"] == "sk_test"
        if request.method == "POST":
            seen["path"], seen["body"] = request.url.path, json.loads(request.content)
            return httpx.Response(200, json={"id": "gen-1", "status": "pending"})
        seen["polls"] += 1
        if seen["polls"] == 1:
            return httpx.Response(200, json={"id": "gen-1", "status": "generating"})
        return httpx.Response(200, json={"id": "gen-1", "status": "completed", "content_url": _SIGNED})

    api(handler)
    jobs, on_job = _collect()
    request = VideoRequest(
        model="bytedance-seedance-v2-fast",
        prompt="A fox in the snow",
        duration_seconds=20,
        resolution="1080p",
        negative_prompt="blur",
        reference_image=PNG,
        reference_mime="image/png",
    )
    result = asyncio.run(ElevenLabsProvider(api_key="sk_test").generate_video(request, on_job=on_job))

    assert result.data == VIDEO and result.job_id == "gen-1" and jobs == ["gen-1"] and seen["polls"] == 2
    body = seen["body"]
    assert seen["path"] == "/v1/flows/video" and body["model_id"] == "bytedance-seedance-v2-fast"
    assert body["duration_secs"] == 15 and body["resolution"] == "720p" and "negative_prompt" not in body
    assert body["start_frame"] == {
        "type": "inline_base64",
        "content_base64": base64.b64encode(PNG).decode("ascii"),
        "mime_type": "image/png",
    }
    notes = " ".join(result.notes)
    assert "15 s" in notes and "720p" in notes and "negative prompt" in notes
    _assert_downloaded_without_credentials(seen)


def test_elevenlabs_veo_takes_its_lengths_and_the_negative_prompt():
    notes: list[str] = []
    request = VideoRequest(model="veo-3.1-fast-generate-001", prompt="x", duration_seconds=3, negative_prompt="text")
    body = ElevenLabsProvider._video_body(request, notes)
    assert body["duration_secs"] == 4 and body["resolution"] == "1080p" and body["negative_prompt"] == "text"
    assert asyncio.run(ElevenLabsProvider(api_key="k").list_video_models())[0] == "veo-3.1-fast-generate-001"


def test_a_failed_elevenlabs_generation_says_why(api):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "gen-2", "status": "pending"})
        return httpx.Response(
            200, json={"id": "gen-2", "status": "failed", "failure_reason": "moderated", "error_message": "Blocked"}
        )

    api(handler)
    with pytest.raises(VideoGenerationError, match="moderated - Blocked"):
        asyncio.run(ElevenLabsProvider(api_key="k").generate_video(VideoRequest(model="veo-3.1-generate-001", prompt="x")))


def test_a_busy_elevenlabs_api_is_asked_again_later_and_a_forgotten_job_says_so(api):
    api(lambda _r: httpx.Response(429, json={"detail": {"message": "slow down"}}))
    with pytest.raises(VideoProviderBusyError):
        asyncio.run(ElevenLabsProvider(api_key="k").generate_video(VideoRequest(model="veo-3.1-generate-001", prompt="x")))

    methods: list[str] = []

    def forgotten(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(404, json={"detail": "not found"})

    api(forgotten)
    with pytest.raises(VideoGenerationError, match="no longer knows"):
        asyncio.run(
            ElevenLabsProvider(api_key="k").generate_video(
                VideoRequest(model="veo-3.1-generate-001", prompt="x"), resume_job="gen-old"
            )
        )
    assert methods == ["GET"]


@pytest.mark.parametrize(
    ("model", "ratio"),
    [("gpt-image-1", "3:2"), ("gpt-image-2", "16:9"), ("bytedance-seedream-5-lite", "16:9")],
    ids=["gpt-image-1", "gpt-image-2", "seedream"],
)
def test_elevenlabs_draws_at_the_ratio_its_model_offers(api, model, ratio):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == _STORAGE:
            seen["download"] = request
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        if request.method == "POST":
            seen["path"], seen["body"] = request.url.path, json.loads(request.content)
            return httpx.Response(200, json={"id": "img-1", "status": "pending"})
        return httpx.Response(200, json={"id": "img-1", "status": "completed", "content_url": _SIGNED})

    api(handler)
    provider = ElevenLabsProvider(api_key="sk_test")
    assert model in asyncio.run(provider.list_image_models())
    image = asyncio.run(provider.generate_image("A lighthouse", model=model, size="1792x1024"))
    assert image == PNG and seen["path"] == "/v1/flows/image"
    assert seen["body"] == {"model_id": model, "prompt": "A lighthouse", "aspect_ratio": ratio}
    _assert_downloaded_without_credentials(seen)


def test_a_refused_elevenlabs_image_says_why(api):
    api(lambda _r: httpx.Response(403, json={"detail": {"message": "missing permission image_video_generation"}}))
    with pytest.raises(ImageGenerationError, match="missing permission"):
        asyncio.run(ElevenLabsProvider(api_key="k").generate_image("x", model="gpt-image-2"))


# ── Kling AI ──────────────────────────────────────────────────────────────


def test_a_kling_key_is_checked_by_listing_its_tasks(api):
    seen: dict = {}

    def refused(request: httpx.Request) -> httpx.Response:
        seen["auth"], seen["query"] = request.headers["authorization"], dict(request.url.params)
        return httpx.Response(401, json={"code": 1002, "message": "Auth failed"})

    api(refused)
    with pytest.raises(VideoGenerationError, match="Kling AI refused the API key"):
        asyncio.run(KlingProvider(api_key="api-key-kling-x").list_video_models())
    assert seen["auth"] == "Bearer api-key-kling-x" and seen["query"] == {"pageNum": "1", "pageSize": "1"}

    api(lambda _r: httpx.Response(200, json={"code": 0, "message": "SUCCEED", "data": []}))
    assert asyncio.run(KlingProvider(api_key="k").list_video_models()) == [
        "kling-v3",
        "kling-v2-6",
        "kling-v2-5-turbo",
        "kling-3.0-omni",
    ]


def test_kling_makes_a_video_from_text(api):
    seen: dict = {"polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == _STORAGE:
            return _storage(seen, request)
        if request.method == "POST":
            seen["path"], seen["body"] = request.url.path, json.loads(request.content)
            return httpx.Response(200, json={"code": 0, "data": {"task_id": "t1", "task_status": "submitted"}})
        seen["polled"] = request.url.path
        seen["polls"] += 1
        if seen["polls"] == 1:
            return httpx.Response(200, json={"code": 0, "data": {"task_id": "t1", "task_status": "processing"}})
        videos = [{"id": "v1", "url": _SIGNED, "duration": "8"}]
        data = {"task_id": "t1", "task_status": "succeed", "task_result": {"videos": videos}}
        return httpx.Response(200, json={"code": 0, "data": data})

    api(handler)
    jobs, on_job = _collect()
    request = VideoRequest(model="kling-v3", prompt="A city at night", duration_seconds=8, negative_prompt="rain")
    result = asyncio.run(KlingProvider(api_key="k").generate_video(request, on_job=on_job))

    assert result.data == VIDEO and result.job_id == "text2video:t1" and jobs == ["text2video:t1"]
    assert seen["path"] == "/v1/videos/text2video" and seen["polled"] == "/v1/videos/text2video/t1"
    assert seen["body"] == {
        "model_name": "kling-v3",
        "prompt": "A city at night",
        "duration": "8",
        "mode": "pro",
        "negative_prompt": "rain",
        "sound": "on",
        "aspect_ratio": "16:9",
    }
    _assert_downloaded_without_credentials(seen)


def test_kling_starts_from_an_image_on_its_own_endpoint_and_resumes_there(api):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == _STORAGE:
            return _storage(seen, request)
        if request.method == "POST":
            seen["path"], seen["body"] = request.url.path, json.loads(request.content)
            return httpx.Response(200, json={"code": 0, "data": {"task_id": "t2"}})
        seen["polled"] = request.url.path
        data = {"task_id": "t2", "task_status": "succeed", "task_result": {"videos": [{"url": _SIGNED}]}}
        return httpx.Response(200, json={"code": 0, "data": data})

    api(handler)
    request = VideoRequest(
        model="kling-v2-5-turbo", prompt="x", resolution="720p", reference_image=PNG, reference_mime="image/png"
    )
    result = asyncio.run(KlingProvider(api_key="k").generate_video(request))
    assert seen["path"] == "/v1/videos/image2video" and result.job_id == "image2video:t2"
    assert seen["body"]["image"] == base64.b64encode(PNG).decode("ascii") and seen["body"]["mode"] == "std"
    assert "aspect_ratio" not in seen["body"] and "sound" not in seen["body"]

    seen.clear()
    asyncio.run(KlingProvider(api_key="k").generate_video(VideoRequest(model="kling-v3", prompt="x"), resume_job="image2video:t9"))
    assert seen["polled"] == "/v1/videos/image2video/t9" and "path" not in seen


def test_kling_drops_the_sound_it_refuses_and_says_so(api):
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == _STORAGE:
            return httpx.Response(200, content=VIDEO)
        if request.method == "POST":
            bodies.append(json.loads(request.content))
            if len(bodies) == 1:
                return httpx.Response(400, json={"code": 1201, "message": "sound is not supported in this mode"})
            return httpx.Response(200, json={"code": 0, "data": {"task_id": "t3"}})
        data = {"task_id": "t3", "task_status": "succeed", "task_result": {"videos": [{"url": _SIGNED}]}}
        return httpx.Response(200, json={"code": 0, "data": data})

    api(handler)
    result = asyncio.run(KlingProvider(api_key="k").generate_video(VideoRequest(model="kling-v2-6", prompt="x")))
    assert bodies[0]["sound"] == "on" and "sound" not in bodies[1] and bodies[1]["mode"] == "pro"
    assert any("silent" in note for note in result.notes)


def test_kling_refusals_about_the_account_are_never_asked_again(api):
    api(lambda _r: httpx.Response(429, json={"code": 1102, "message": "Account balance not enough"}))
    with pytest.raises(VideoGenerationError, match="balance not enough"):
        asyncio.run(KlingProvider(api_key="k").generate_video(VideoRequest(model="kling-v3", prompt="x")))

    api(lambda _r: httpx.Response(429, json={"code": 1302, "message": "Rate limit exceeded"}))
    with pytest.raises(VideoProviderBusyError):
        asyncio.run(KlingProvider(api_key="k").generate_video(VideoRequest(model="kling-v3", prompt="x")))

    # A refusal answered with an HTTP 200 is still a refusal.
    api(lambda _r: httpx.Response(200, json={"code": 1301, "message": "Content risk detected"}))
    with pytest.raises(VideoGenerationError, match="Content risk"):
        asyncio.run(KlingProvider(api_key="k").generate_video(VideoRequest(model="kling-v3", prompt="x")))


def test_a_failed_kling_task_says_why(api):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"code": 0, "data": {"task_id": "t4"}})
        data = {"task_id": "t4", "task_status": "failed", "task_status_msg": "Failure to pass the risk control system"}
        return httpx.Response(200, json={"code": 0, "data": data})

    api(handler)
    with pytest.raises(VideoGenerationError, match="risk control"):
        asyncio.run(KlingProvider(api_key="k").generate_video(VideoRequest(model="kling-v3", prompt="x")))


# ── Seedance (ModelArk) ───────────────────────────────────────────────────


def test_seedance_makes_a_video_within_its_models_limits(api):
    seen: dict = {"polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == _STORAGE:
            return _storage(seen, request)
        assert request.headers["authorization"] == "Bearer ark-key"
        if request.method == "POST":
            seen["path"], seen["body"] = request.url.path, json.loads(request.content)
            return httpx.Response(200, json={"id": "cgt-1"})
        seen["polled"] = request.url.path
        seen["polls"] += 1
        if seen["polls"] == 1:
            return httpx.Response(200, json={"id": "cgt-1", "status": "running"})
        return httpx.Response(200, json={"id": "cgt-1", "status": "succeeded", "content": {"video_url": _SIGNED}})

    api(handler)
    jobs, on_job = _collect()
    request = VideoRequest(model="seedance-1-0-pro-250528", prompt="Waves", duration_seconds=20, negative_prompt="boats")
    result = asyncio.run(SeedanceProvider(api_key="ark-key").generate_video(request, on_job=on_job))

    assert result.data == VIDEO and result.job_id == "cgt-1" and jobs == ["cgt-1"]
    assert seen["path"] == "/api/v3/contents/generations/tasks"
    assert seen["polled"] == "/api/v3/contents/generations/tasks/cgt-1"
    assert seen["body"] == {
        "model": "seedance-1-0-pro-250528",
        "content": [{"type": "text", "text": "Waves"}],
        "ratio": "16:9",
        "watermark": False,
        "duration": 12,
        "resolution": "1080p",
    }
    assert any("12 s" in note for note in result.notes) and any("negative prompt" in note for note in result.notes)
    _assert_downloaded_without_credentials(seen)


def test_seedance_takes_the_start_image_as_its_first_frame():
    notes: list[str] = []
    request = VideoRequest(
        model="dreamina-seedance-2-0-fast-260128", prompt="x", reference_image=PNG, reference_mime="image/png"
    )
    body = SeedanceProvider._video_body(request, notes)
    image = body["content"][1]
    assert image == {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{base64.b64encode(PNG).decode('ascii')}"},
        "role": "first_frame",
    }
    assert body["ratio"] == "adaptive" and body["resolution"] == "720p" and body["generate_audio"] is True


def test_a_failed_seedance_task_and_a_refused_key_say_why(api):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "cgt-2"})
        error = {"code": "OutputVideoSensitiveContentDetected", "message": "The output may contain sensitive content"}
        return httpx.Response(200, json={"id": "cgt-2", "status": "failed", "error": error})

    api(handler)
    with pytest.raises(VideoGenerationError, match="sensitive content"):
        asyncio.run(SeedanceProvider(api_key="k").generate_video(VideoRequest(model="dreamina-seedance-2-0-260128", prompt="x")))

    error = {"code": "AuthenticationError", "message": "The API key is invalid"}
    api(lambda _r: httpx.Response(401, json={"error": error}))
    with pytest.raises(VideoGenerationError, match="ModelArk refused the API key"):
        asyncio.run(SeedanceProvider(api_key="k").list_video_models())


def test_seedance_uses_the_endpoint_it_is_given(api):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"items": [], "total": 0})

    api(handler)
    provider = SeedanceProvider(api_key="ark-key||https://ark.cn-beijing.volces.com/api/v3/")
    assert "dreamina-seedance-2-0-260128" in asyncio.run(provider.list_video_models())
    assert seen["url"].startswith("https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks?")


# ── Runway ────────────────────────────────────────────────────────────────


def test_runway_offers_the_models_its_tier_may_run_once_the_key_is_read(api):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"], seen["headers"] = request.url.path, request.headers
        return httpx.Response(200, json={"creditBalance": 10, "tier": {"models": {"gen4.5": {}, "veo3.1_fast": {}}}})

    api(handler)
    assert asyncio.run(RunwayProvider(api_key="key_x").list_video_models()) == ["gen4.5", "veo3.1_fast"]
    assert seen["path"] == "/v1/organization"
    assert seen["headers"]["authorization"] == "Bearer key_x" and seen["headers"]["x-runway-version"] == "2024-11-06"

    api(lambda _r: httpx.Response(401, json={"error": "The provided API key is not valid."}))
    with pytest.raises(VideoGenerationError, match="Runway refused the API key"):
        asyncio.run(RunwayProvider(api_key="key_x").list_video_models())


def test_runway_makes_a_gen45_video_from_text(api):
    seen: dict = {"polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == _STORAGE:
            return _storage(seen, request)
        if request.method == "POST":
            seen["path"], seen["body"] = request.url.path, json.loads(request.content)
            return httpx.Response(200, json={"id": "task-1"})
        seen["polled"] = request.url.path
        seen["polls"] += 1
        if seen["polls"] == 1:
            return httpx.Response(200, json={"id": "task-1", "status": "RUNNING", "progress": 0.4})
        return httpx.Response(200, json={"id": "task-1", "status": "SUCCEEDED", "output": [_SIGNED]})

    api(handler)
    jobs, on_job = _collect()
    progress: list[int] = []

    async def report(value: int) -> None:
        progress.append(value)

    request = VideoRequest(model="gen4.5", prompt="A lighthouse at dusk", duration_seconds=8, negative_prompt="fog")
    result = asyncio.run(RunwayProvider(api_key="key_x").generate_video(request, progress=report, on_job=on_job))

    assert result.data == VIDEO and result.job_id == "task-1" and jobs == ["task-1"] and progress == [40]
    assert seen["path"] == "/v1/text_to_video" and seen["polled"] == "/v1/tasks/task-1"
    assert seen["body"] == {"model": "gen4.5", "promptText": "A lighthouse at dusk", "duration": 8, "ratio": "1280:720"}
    notes = " ".join(result.notes)
    assert "720p" in notes and "negative prompt" in notes
    _assert_downloaded_without_credentials(seen)


def test_runway_gen4_turbo_animates_a_start_image_and_veo_keeps_its_fields():
    with pytest.raises(VideoGenerationError, match="animates a start image"):
        RunwayProvider._video_body(VideoRequest(model="gen4_turbo", prompt="x"), [])

    request = VideoRequest(model="gen4_turbo", prompt="x", reference_image=PNG, reference_mime="image/png")
    path, body = RunwayProvider._video_body(request, [])
    assert path == "/v1/image_to_video"
    assert body["promptImage"] == f"data:image/png;base64,{base64.b64encode(PNG).decode('ascii')}"

    request = VideoRequest(model="veo3.1", prompt="x", aspect_ratio="9:16", duration_seconds=5, negative_prompt="text")
    path, body = RunwayProvider._video_body(request, [])
    assert path == "/v1/text_to_video" and body["ratio"] == "1080:1920" and body["duration"] == 4
    assert body["negativePrompt"] == "text" and body["audio"] is True


def test_runway_refusals_and_failed_tasks_say_why(api):
    issues = [{"path": ["ratio"], "message": "Invalid option"}]
    api(lambda _r: httpx.Response(400, json={"error": "Validation of body failed", "issues": issues}))
    with pytest.raises(VideoGenerationError, match="Validation of body failed: ratio - Invalid option"):
        asyncio.run(RunwayProvider(api_key="key_x").generate_video(VideoRequest(model="gen4.5", prompt="x")))

    api(lambda _r: httpx.Response(429, json={"error": "Too many requests"}))
    with pytest.raises(VideoProviderBusyError):
        asyncio.run(RunwayProvider(api_key="key_x").generate_video(VideoRequest(model="gen4.5", prompt="x")))

    def failed(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "task-2"})
        body = {"id": "task-2", "status": "FAILED", "failureCode": "SAFETY.INPUT.TEXT", "failure": "The prompt was flagged"}
        return httpx.Response(200, json=body)

    api(failed)
    with pytest.raises(VideoGenerationError, match=r"SAFETY\.INPUT\.TEXT - The prompt was flagged"):
        asyncio.run(RunwayProvider(api_key="key_x").generate_video(VideoRequest(model="gen4.5", prompt="x")))


def test_a_runway_prompt_is_cut_where_runway_counts():
    notes: list[str] = []
    _path, body = RunwayProvider._video_body(VideoRequest(model="gen4.5", prompt="\U0001f600" * 600), notes)
    assert len(body["promptText"].encode("utf-16-le")) // 2 <= 1000 and notes


# ── The signed download ───────────────────────────────────────────────────


def test_a_signed_url_on_a_private_address_is_never_fetched():
    with pytest.raises(VideoGenerationError, match="could not be downloaded"):
        asyncio.run(download_video("https://127.0.0.1/made.mp4", "veo-3.1-generate-001"))


def test_a_signed_download_follows_redirects_within_its_cap_and_rides_out_an_outage(api):
    def redirected(request: httpx.Request) -> httpx.Response:
        if request.url.host == _STORAGE:
            return httpx.Response(302, headers={"location": "https://cdn.example.com/v.mp4"})
        return httpx.Response(200, content=VIDEO, headers={"content-type": "video/mp4"})

    api(redirected)
    assert asyncio.run(fetch_signed_media(_SIGNED, max_bytes=1024)) == VIDEO
    with pytest.raises(VideoAPIError, match="maximum size") as capped:
        asyncio.run(fetch_signed_media(_SIGNED, max_bytes=4))
    assert capped.value.code == 400

    answers = [503, 503, 200]

    def flaky(_request: httpx.Request) -> httpx.Response:
        code = answers.pop(0)
        return httpx.Response(code, content=VIDEO if code == 200 else b"")

    api(flaky)
    assert asyncio.run(download_video(_SIGNED, "kling-v3")) == VIDEO and answers == []


# ── Images ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("size", "ratios", "expected"),
    [
        ("1024x1024", ("1:1", "3:2", "2:3"), "1:1"),
        ("1792x1024", ("1:1", "3:2", "2:3"), "3:2"),
        ("1024x1792", ("1:1", "16:9", "9:16"), "9:16"),
        ("auto", ("16:9",), "1:1"),
    ],
)
def test_a_size_is_drawn_at_the_nearest_ratio_offered(size, ratios, expected):
    assert nearest_aspect_ratio(size, ratios) == expected


def test_a_start_image_of_another_type_is_sent_as_png():
    from PIL import Image

    gif = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(gif, format="GIF")
    data, mime = accepted_image(gif.getvalue(), "image/gif", ("image/jpeg", "image/png"))
    assert mime == "image/png" and data.startswith(b"\x89PNG")
    assert accepted_image(PNG, "image/png", ("image/png",)) == (PNG, "image/png")
    with pytest.raises(ImageGenerationError):
        accepted_image(b"not an image", "image/svg+xml", ("image/png",))
