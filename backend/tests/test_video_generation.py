"""Video generation through the LLM providers: which models are video
models, the OpenAI Videos API shared by every OpenAI-shaped provider, Veo,
the provider resolution and the platform setting."""

import asyncio
import uuid
from types import SimpleNamespace

import httpx
import openai
import pytest

from app.llm import router, video
from app.llm.video import OpenAIVideosMixin, VideoGenerationError, VideoRequest


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr(video, "POLL_SECONDS", 0)


# ── Which models are video models ─────────────────────────────────────────


@pytest.mark.parametrize(
    "model",
    [
        "sora-2",
        "sora-2-pro",
        "openai/sora-2-pro-high-res",
        "veo-3.1-generate-preview",
        "gemini/veo-3.1-lite-generate-preview",
        "vertex_ai/veo-3.0-fast-generate-001",
        "runwayml/gen4_turbo",
        "gen4_aleph",
        "sora-3-preview",
    ],
)
def test_video_models_are_recognised_and_never_chat(model):
    assert router.is_video_model(model)
    assert not router.is_chat_capable_model(model)


@pytest.mark.parametrize("model", ["gpt-4o", "claude-opus-5-5", "gemini-3.8-flash", "gpt-image-1", None, ""])
def test_other_models_are_not_video_models(model):
    assert not router.is_video_model(model)


def test_the_built_in_video_models_are_declared_by_the_providers():
    from app.llm.base import BaseLLMProvider

    for name, model in router.VIDEO_DEFAULTS.items():
        cls = router.PROVIDER_MAP[name]
        assert cls.default_video_model == model
        assert cls.generate_video is not BaseLLMProvider.generate_video


def test_every_openai_shaped_provider_speaks_the_videos_api():
    from app.llm.azure_openai_provider import AzureOpenAIProvider
    from app.llm.custom_openai_provider import CustomOpenAIProvider
    from app.llm.gateway_provider import FiligranGatewayProvider
    from app.llm.litellm_provider import LiteLLMProvider
    from app.llm.openai_provider import OpenAIProvider

    for cls in (OpenAIProvider, AzureOpenAIProvider, CustomOpenAIProvider, LiteLLMProvider, FiligranGatewayProvider):
        assert issubclass(cls, OpenAIVideosMixin), cls


# ── The OpenAI Videos API ─────────────────────────────────────────────────


def _bad_request(message: str = "invalid size", param: str | None = None) -> openai.BadRequestError:
    response = httpx.Response(400, request=httpx.Request("POST", "https://api.example.com/v1/videos"))
    return openai.BadRequestError(message, response=response, body={"error": {"message": message, "param": param}})


def _status(code: int, message: str) -> openai.APIStatusError:
    response = httpx.Response(code, request=httpx.Request("POST", "https://api.example.com/v1/videos"))
    return openai.APIStatusError(message, response=response, body={"error": {"message": message}})


def _unreachable() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=httpx.Request("GET", "https://api.example.com/v1/videos/video_1"))


class _Videos:
    def __init__(
        self,
        *,
        refuse_sizes=(),
        refuse_seconds=False,
        refuse_defaults=None,
        final="completed",
        fail_with=None,
        poll_errors=(),
        download_errors=(),
    ):
        self.created: list[dict] = []
        self.asked: list[dict] = []
        self.refuse_sizes = set(refuse_sizes)
        self.refuse_seconds = refuse_seconds
        self.refuse_defaults = refuse_defaults
        self.final = final
        self.fail_with = fail_with
        self.poll_errors = list(poll_errors)
        self.download_errors = list(download_errors)
        self.polls = 0
        self.downloads = 0

    async def create(self, **kwargs):
        self.asked.append(kwargs)
        if self.fail_with is not None:
            raise self.fail_with
        if "size" in kwargs and (kwargs["size"] in self.refuse_sizes or "*" in self.refuse_sizes):
            raise _bad_request(f"size {kwargs['size']} is not supported")
        if self.refuse_seconds and "seconds" in kwargs:
            raise _bad_request("seconds is not a parameter of this model")
        if self.refuse_defaults and "size" not in kwargs and "seconds" not in kwargs:
            raise self.refuse_defaults
        self.created.append(kwargs)
        return SimpleNamespace(id="video_1", status="queued", progress=0)

    async def retrieve(self, job_id, **kwargs):
        if self.poll_errors:
            error = self.poll_errors.pop(0)
            if error is not None:
                raise error
        self.polls += 1
        status = "in_progress" if self.polls < 2 else self.final
        error = SimpleNamespace(message="moderation") if status == "failed" else None
        return SimpleNamespace(id=job_id, status=status, progress=50 * self.polls, error=error)

    async def download_content(self, job_id, **kwargs):
        self.downloads += 1
        if self.download_errors:
            raise self.download_errors.pop(0)
        return SimpleNamespace(read=lambda: b"MP4DATA")


class _Provider(OpenAIVideosMixin):
    def __init__(self, videos, models=()):
        self.client = SimpleNamespace(
            videos=videos,
            models=SimpleNamespace(list=self._list),
        )
        self._models = models

    async def _list(self):
        return SimpleNamespace(data=[SimpleNamespace(id=m) for m in self._models])


def test_a_video_is_created_polled_and_downloaded():
    videos = _Videos()
    progress: list[int] = []

    async def report(value):
        progress.append(value)

    result = asyncio.run(
        _Provider(videos).generate_video(
            VideoRequest(model="sora-2", prompt="A drone shot", duration_seconds=7, resolution="720p"),
            progress=report,
        )
    )
    assert result.data == b"MP4DATA" and result.job_id == "video_1"
    assert videos.created[0]["size"] == "1280x720"
    assert videos.created[0]["seconds"] == "8"
    assert any("snapped to 8 s" in note for note in result.notes)
    assert progress and max(progress) >= 50


def test_portrait_uses_a_portrait_size():
    videos = _Videos()
    asyncio.run(
        _Provider(videos).generate_video(
            VideoRequest(model="sora-2", prompt="x", aspect_ratio="9:16", resolution="720p")
        )
    )
    assert videos.created[0]["size"] == "720x1280"


def test_a_size_the_model_refuses_falls_back_to_720p_and_says_so():
    videos = _Videos(refuse_sizes={"1792x1024", "1920x1080"})
    result = asyncio.run(_Provider(videos).generate_video(VideoRequest(model="sora-2", prompt="x", resolution="1080p")))
    assert [asked["size"] for asked in videos.asked] == ["1792x1024", "1920x1080", "1280x720"]
    assert videos.created[0]["size"] == "1280x720"
    assert any("does not make 1792x1024" in note for note in result.notes)


def test_a_model_that_takes_no_size_nor_length_is_asked_with_its_own():
    """A gateway in front of another vendor's model may refuse both fields: the model's defaults are asked last."""
    videos = _Videos(refuse_sizes={"*"}, refuse_seconds=True)
    result = asyncio.run(_Provider(videos).generate_video(VideoRequest(model="veo", prompt="x", resolution="1080p")))
    made = videos.created[0]
    assert "size" not in made and "seconds" not in made
    assert any("takes no size" in note for note in result.notes)
    assert any("takes no length" in note for note in result.notes)


def test_a_refusal_about_the_prompt_is_the_answer_and_never_asked_again():
    videos = _Videos(fail_with=_bad_request("The prompt was flagged by the safety system"))
    with pytest.raises(VideoGenerationError, match="flagged by the safety system"):
        asyncio.run(_Provider(videos).generate_video(VideoRequest(model="sora-2", prompt="x")))
    # No other size or length changes a blocked prompt.
    assert len(videos.asked) == 1


def test_a_refusal_naming_a_field_in_its_param_is_relaxed():
    videos = _Videos(fail_with=_bad_request("Invalid value", param="size"))
    with pytest.raises(VideoGenerationError, match="Invalid value"):
        asyncio.run(_Provider(videos).generate_video(VideoRequest(model="sora-2", prompt="x", resolution="720p")))
    assert len(videos.asked) == 3  # 720p, the model's own size, then its own length too
    blocked = _Videos(fail_with=_bad_request("The size of this prompt is too large", param="prompt"))
    with pytest.raises(VideoGenerationError):
        asyncio.run(_Provider(blocked).generate_video(VideoRequest(model="sora-2", prompt="x")))
    # The structured param decides over the words of the message.
    assert len(blocked.asked) == 1


def test_the_last_refusal_is_the_one_reported():
    videos = _Videos(
        refuse_sizes={"*"}, refuse_seconds=True, refuse_defaults=_bad_request("duration is required by this model")
    )
    with pytest.raises(VideoGenerationError, match="duration is required") as raised:
        asyncio.run(_Provider(videos).generate_video(VideoRequest(model="veo", prompt="x", resolution="720p")))
    assert "size 1280x720" not in str(raised.value)


def test_a_job_that_exists_is_polled_through_transient_failures_never_made_twice():
    videos = _Videos(poll_errors=[_status(503, "overloaded"), _unreachable(), _status(429, "slow down")])
    result = asyncio.run(_Provider(videos).generate_video(VideoRequest(model="sora-2", prompt="x")))
    assert result.data == b"MP4DATA"
    assert len(videos.asked) == 1


def test_a_job_whose_status_keeps_failing_past_the_budget_ends_without_a_second_generation(monkeypatch):
    monkeypatch.setattr(video, "TRANSIENT_BUDGET_SECONDS", 0)
    videos = _Videos(poll_errors=[_status(502, "bad gateway")] * video.MIN_TRANSIENT_ATTEMPTS)
    with pytest.raises(VideoGenerationError, match="kept failing") as raised:
        asyncio.run(_Provider(videos).generate_video(VideoRequest(model="sora-2", prompt="x")))
    # A plain error, not the busy one the job retries: retrying would pay for a second video.
    assert not isinstance(raised.value, video.VideoProviderBusyError)
    assert len(videos.asked) == 1


def test_trouble_within_the_budget_is_ridden_out_and_a_good_answer_starts_it_again(monkeypatch):
    many = [_status(503, "x")] * (video.MIN_TRANSIENT_ATTEMPTS + 4)
    videos = _Videos(poll_errors=many)
    assert asyncio.run(_Provider(videos).generate_video(VideoRequest(model="sora-2", prompt="x"))).data == b"MP4DATA"
    monkeypatch.setattr(video, "TRANSIENT_BUDGET_SECONDS", 0)
    below = video.MIN_TRANSIENT_ATTEMPTS - 1
    videos = _Videos(poll_errors=[_status(503, "x")] * below + [None] + [_status(503, "x")] * below)
    result = asyncio.run(_Provider(videos).generate_video(VideoRequest(model="sora-2", prompt="x")))
    assert result.data == b"MP4DATA"


def test_the_back_off_grows_and_honours_the_providers_retry_after():
    failures = video.TransientFailures("sora-2", budget=3600)
    waits = []
    for _ in range(5):
        failures.failed(_status(503, "x"), 503)
        waits.append(failures.wait)
    assert waits == sorted(waits) and waits[-1] <= video.MAX_BACKOFF_SECONDS
    response = httpx.Response(
        429, headers={"retry-after": "7"}, request=httpx.Request("GET", "https://api.example.com/v1/videos/v")
    )
    failures.failed(openai.RateLimitError("slow down", response=response, body=None), 429)
    assert failures.wait == 7.0


def test_a_cut_body_is_downloaded_again_and_an_empty_video_is_an_error():
    class _Cut(_Videos):
        async def download_content(self, job_id, **kwargs):
            self.downloads += 1

            def read():
                if self.downloads == 1:
                    raise httpx.ReadError("connection reset mid-body")
                return b"MP4DATA"

            return SimpleNamespace(read=read)

    cut = _Cut()
    assert asyncio.run(_Provider(cut).generate_video(VideoRequest(model="sora-2", prompt="x"))).data == b"MP4DATA"
    assert cut.downloads == 2 and len(cut.asked) == 1

    class _Empty(_Videos):
        async def download_content(self, job_id, **kwargs):
            return SimpleNamespace(read=lambda: b"")

    with pytest.raises(VideoGenerationError, match="empty video"):
        asyncio.run(_Provider(_Empty()).generate_video(VideoRequest(model="sora-2", prompt="x")))


def test_a_known_job_is_resumed_and_its_id_handed_out_as_soon_as_it_exists():
    videos = _Videos()
    told: list[str] = []

    async def remember(job_id):
        told.append(job_id)

    asyncio.run(_Provider(videos).generate_video(VideoRequest(model="sora-2", prompt="x"), on_job=remember))
    assert told == ["video_1"]
    resumed = _Videos()
    result = asyncio.run(
        _Provider(resumed).generate_video(VideoRequest(model="sora-2", prompt="x"), resume_job="video_1")
    )
    assert result.data == b"MP4DATA" and result.job_id == "video_1" and resumed.asked == []

    class _Forgotten(_Videos):
        async def retrieve(self, job_id, **kwargs):
            response = httpx.Response(404, request=httpx.Request("GET", "https://api.example.com/v1/videos/v"))
            raise openai.NotFoundError("gone", response=response, body=None)

    with pytest.raises(VideoGenerationError, match="no longer knows the generation"):
        asyncio.run(_Provider(_Forgotten()).generate_video(VideoRequest(model="sora-2", prompt="x"), resume_job="old"))


def test_the_made_video_is_downloaded_again_on_a_transient_failure():
    videos = _Videos(download_errors=[_status(502, "bad gateway"), _unreachable()])
    result = asyncio.run(_Provider(videos).generate_video(VideoRequest(model="sora-2", prompt="x")))
    assert result.data == b"MP4DATA" and videos.downloads == 3 and len(videos.asked) == 1
    gone = _Videos(download_errors=[_status(502, "bad gateway")] * video.DOWNLOAD_ATTEMPTS)
    with pytest.raises(VideoGenerationError, match="kept failing"):
        asyncio.run(_Provider(gone).generate_video(VideoRequest(model="sora-2", prompt="x")))
    refused = _Videos(download_errors=[_status(403, "expired")])
    with pytest.raises(VideoGenerationError, match="expired"):
        asyncio.run(_Provider(refused).generate_video(VideoRequest(model="sora-2", prompt="x")))
    assert refused.downloads == 1


@pytest.mark.parametrize(
    ("code", "error", "words"),
    [
        (404, VideoGenerationError, "no video generation for sora-2"),
        (403, VideoGenerationError, "refused the request for sora-2"),
        (429, video.VideoProviderBusyError, "unavailable"),
        (503, video.VideoProviderBusyError, "unavailable"),
    ],
)
def test_an_http_failure_is_readable_and_only_a_transient_one_is_asked_again(code, error, words):
    videos = _Videos(fail_with=_status(code, "upstream says no"))
    with pytest.raises(error, match=words) as raised:
        asyncio.run(_Provider(videos).generate_video(VideoRequest(model="sora-2", prompt="x")))
    assert "upstream says no" in str(raised.value)
    # The refusal was not retried in other shapes: it is not about the request.
    assert len(videos.asked) == 1


def test_a_busy_provider_is_retried_by_the_job_and_a_refusal_is_not():
    from app.services.video_studio import clips

    class _Busy:
        provider_name = "x"

        async def generate_video(self, request, *, progress=None, on_job=None, resume_job=None, on_state=None):
            raise video.VideoProviderBusyError("rate limited")

    class _Refused(_Busy):
        async def generate_video(self, request, *, progress=None, on_job=None, resume_job=None, on_state=None):
            raise VideoGenerationError("filtered")

    with pytest.raises(video.VideoProviderBusyError):
        asyncio.run(clips.generate(_Busy(), VideoRequest(model="m", prompt="x")))
    with pytest.raises(clips.ClipError, match="filtered"):
        asyncio.run(clips.generate(_Refused(), VideoRequest(model="m", prompt="x")))


def test_an_unknown_aspect_ratio_is_made_at_16_9():
    videos = _Videos()
    result = asyncio.run(_Provider(videos).generate_video(VideoRequest(model="sora-2", prompt="x", aspect_ratio="1:1")))
    assert videos.created[0]["size"] in ("1792x1024", "1280x720")
    assert any("16:9" in note for note in result.notes)


def test_a_failed_job_is_an_error_with_its_reason():
    with pytest.raises(VideoGenerationError, match="moderation"):
        asyncio.run(_Provider(_Videos(final="failed")).generate_video(VideoRequest(model="sora-2", prompt="x")))


def test_a_reference_image_is_sent_as_the_first_frame():
    videos = _Videos()
    asyncio.run(
        _Provider(videos).generate_video(
            VideoRequest(model="sora-2", prompt="x", reference_image=b"PNG", reference_mime="image/png")
        )
    )
    assert videos.created[0]["input_reference"] == ("reference.png", b"PNG", "image/png")


def test_listing_keeps_the_video_models_only():
    provider = _Provider(_Videos(), models=["gpt-4o", "sora-2", "sora-2-pro", "text-embedding-3-large"])
    assert asyncio.run(provider.list_video_models()) == ["sora-2", "sora-2-pro"]


def test_a_gateway_pins_the_upstream_on_every_video_call(monkeypatch):
    videos = _Videos()

    class _Pinned(_Provider):
        async def _video_headers(self, model):
            return {"X-Target-Provider": "openai"}

    asyncio.run(_Pinned(videos).generate_video(VideoRequest(model="sora-2", prompt="x")))
    assert videos.created[0]["extra_headers"] == {"X-Target-Provider": "openai"}


def test_azure_serves_videos_on_its_v1_endpoint():
    from app.llm.azure_openai_provider import AzureOpenAIProvider

    provider = AzureOpenAIProvider(api_key="https://acme.openai.azure.com||secret||2024-10-21")
    client = provider._videos_client()
    assert str(client.base_url).rstrip("/") == "https://acme.openai.azure.com/openai/v1"
    assert provider._videos_client() is client


def test_litellm_reads_the_mode_its_proxy_declares(monkeypatch):
    from app.llm.litellm_provider import LiteLLMProvider

    def _proxy(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/model/info":
            assert request.headers["authorization"] == "Bearer sk"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"model_name": "veo", "model_info": {"mode": "video_generation"}},
                        {"model_name": "fast-chat", "model_info": {"mode": "chat"}},
                    ]
                },
            )
        assert request.url.path == "/v1/models"
        return httpx.Response(
            200, json={"object": "list", "data": [{"id": "veo", "object": "model"}, {"id": "runwayml/gen4_turbo", "object": "model"}]}
        )

    provider = LiteLLMProvider(api_key="sk||http://proxy:4000")
    provider.client = openai.AsyncOpenAI(
        api_key="sk", base_url=provider.base_url, http_client=httpx.AsyncClient(transport=httpx.MockTransport(_proxy))
    )
    assert asyncio.run(provider.list_video_models()) == ["runwayml/gen4_turbo", "veo"]


def test_a_litellm_proxy_without_model_info_still_lists_by_id():
    from app.llm.litellm_provider import LiteLLMProvider

    def _proxy(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/model/info":
            return httpx.Response(404, json={"error": "not here"})
        return httpx.Response(200, json={"object": "list", "data": [{"id": "sora-2", "object": "model"}]})

    provider = LiteLLMProvider(api_key="sk||http://proxy:4000")
    provider.client = openai.AsyncOpenAI(
        api_key="sk", base_url=provider.base_url, http_client=httpx.AsyncClient(transport=httpx.MockTransport(_proxy))
    )
    assert asyncio.run(provider.list_video_models()) == ["sora-2"]


# ── Veo ───────────────────────────────────────────────────────────────────


def test_veo_runs_as_a_long_operation(monkeypatch):
    from app.llm.gemini_provider import GeminiProvider

    calls: dict = {}
    done = SimpleNamespace(
        done=True,
        error=None,
        name="operations/1",
        response=SimpleNamespace(generated_videos=[SimpleNamespace(video=SimpleNamespace(video_bytes=b"VEO"))]),
    )

    async def generate_videos(**kwargs):
        calls.update(kwargs)
        return SimpleNamespace(done=False, name="operations/1")

    async def get(operation):
        return done

    provider = GeminiProvider.__new__(GeminiProvider)
    provider.client = SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(generate_videos=generate_videos), operations=SimpleNamespace(get=get)
        )
    )
    result = asyncio.run(
        provider.generate_video(VideoRequest(model="veo-3.1-generate-preview", prompt="x", duration_seconds=12))
    )
    assert result.data == b"VEO" and result.job_id == "operations/1"
    assert calls["config"].duration_seconds == 8
    assert any("snapped to 8 s" in note for note in result.notes)


def test_veo_settings_a_model_refuses_are_relaxed_in_turn_and_said():
    from google.genai import errors as genai_errors

    from app.llm.gemini_provider import GeminiProvider

    asked: list = []
    done = SimpleNamespace(
        done=True,
        error=None,
        name="operations/2",
        response=SimpleNamespace(generated_videos=[SimpleNamespace(video=SimpleNamespace(video_bytes=b"VEO"))]),
    )

    async def generate_videos(**kwargs):
        config = kwargs["config"]
        asked.append((config.resolution, config.duration_seconds))
        if config.resolution is not None:
            raise genai_errors.ClientError(400, {"error": {"message": "resolution is not supported"}})
        return done

    provider = GeminiProvider.__new__(GeminiProvider)
    provider.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_videos=generate_videos)))
    result = asyncio.run(
        provider.generate_video(VideoRequest(model="veo-2.0-generate-001", prompt="x", duration_seconds=6))
    )
    assert result.data == b"VEO"
    assert asked == [("1080p", 6), ("720p", 6), (None, 8)]
    assert any("refused 1080p for 6 s" in note for note in result.notes)


def test_veo_refusing_every_setting_says_why_and_an_outage_is_retried():
    from google.genai import errors as genai_errors

    from app.llm.gemini_provider import GeminiProvider

    async def refuse(**_kwargs):
        raise genai_errors.ClientError(400, {"error": {"message": "The prompt was blocked"}})

    async def outage(**_kwargs):
        raise genai_errors.ServerError(503, {"error": {"message": "overloaded"}})

    provider = GeminiProvider.__new__(GeminiProvider)
    provider.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_videos=refuse)))
    with pytest.raises(VideoGenerationError, match="The prompt was blocked"):
        asyncio.run(provider.generate_video(VideoRequest(model="veo-3.1-generate-preview", prompt="x")))
    provider.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_videos=outage)))
    with pytest.raises(video.VideoProviderBusyError, match="overloaded"):
        asyncio.run(provider.generate_video(VideoRequest(model="veo-3.1-generate-preview", prompt="x")))


def test_veo_stops_relaxing_at_a_refusal_about_something_else():
    from google.genai import errors as genai_errors

    from app.llm.gemini_provider import GeminiProvider

    asked: list = []

    async def refuse(**kwargs):
        asked.append(kwargs["config"].resolution)
        raise genai_errors.ClientError(400, {"error": {"message": "The prompt was blocked"}})

    provider = GeminiProvider.__new__(GeminiProvider)
    provider.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_videos=refuse)))
    with pytest.raises(VideoGenerationError, match="The prompt was blocked"):
        asyncio.run(provider.generate_video(VideoRequest(model="veo-3.1-generate-preview", prompt="x")))
    assert asked == ["1080p"]


def test_veo_polls_and_downloads_through_transient_failures_never_made_twice():
    from google.genai import errors as genai_errors

    from app.llm.gemini_provider import GeminiProvider

    made: list = []
    stored = SimpleNamespace(video_bytes=None)
    done = SimpleNamespace(
        done=True,
        error=None,
        name="operations/3",
        response=SimpleNamespace(generated_videos=[SimpleNamespace(video=stored)]),
    )
    polls = [genai_errors.ServerError(503, {"error": {"message": "overloaded"}}), done]
    downloads = [genai_errors.ServerError(500, {"error": {"message": "internal"}}), b"VEO"]

    async def generate_videos(**kwargs):
        made.append(kwargs)
        return SimpleNamespace(done=False, name="operations/3")

    async def answer(queue):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def get(_operation):
        return await answer(polls)

    async def download(*, file):
        assert file is stored
        return await answer(downloads)

    provider = GeminiProvider.__new__(GeminiProvider)
    provider.client = SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(generate_videos=generate_videos),
            operations=SimpleNamespace(get=get),
            files=SimpleNamespace(download=download),
        )
    )
    result = asyncio.run(provider.generate_video(VideoRequest(model="veo-3.1-generate-preview", prompt="x")))
    assert result.data == b"VEO" and len(made) == 1 and not polls and not downloads


def _veo(*, polls=(), downloads=(), stored_bytes=None):
    """A Veo client double: generations made, operations asked for, files downloaded."""
    from app.llm.gemini_provider import GeminiProvider

    stored = SimpleNamespace(video_bytes=stored_bytes)
    done = SimpleNamespace(
        done=True, error=None, name="operations/9", response=SimpleNamespace(generated_videos=[SimpleNamespace(video=stored)])
    )
    queue = {"polls": [*polls, done], "downloads": list(downloads)}
    made: list = []

    async def generate_videos(**kwargs):
        made.append(kwargs)
        return SimpleNamespace(done=False, name="operations/9")

    async def take(name):
        item = queue[name].pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def get(_operation):
        return await take("polls")

    async def download(*, file):
        return await take("downloads")

    provider = GeminiProvider.__new__(GeminiProvider)
    provider.client = SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(generate_videos=generate_videos),
            operations=SimpleNamespace(get=get),
            files=SimpleNamespace(download=download),
        )
    )
    return provider, made


def test_veo_rides_out_a_connection_the_sdk_does_not_wrap_and_never_starts_twice():
    request = httpx.Request("GET", "https://generativelanguage.googleapis.com/v1beta/operations/9")
    provider, made = _veo(polls=[httpx.ConnectError("reset", request=request), TimeoutError()], stored_bytes=b"VEO")
    result = asyncio.run(provider.generate_video(VideoRequest(model="veo-3.1-generate-preview", prompt="x")))
    assert result.data == b"VEO" and len(made) == 1


def test_veo_says_so_when_it_returns_an_empty_video():
    provider, _made = _veo(downloads=[b""])
    with pytest.raises(VideoGenerationError, match="empty video"):
        asyncio.run(provider.generate_video(VideoRequest(model="veo-3.1-generate-preview", prompt="x")))


def test_veo_resumes_a_known_operation_and_says_when_the_provider_forgot_it():
    from google.genai import errors as genai_errors

    told: list[str] = []

    async def remember(name):
        told.append(name)

    provider, made = _veo(stored_bytes=b"VEO")
    asyncio.run(provider.generate_video(VideoRequest(model="veo-3.1-generate-preview", prompt="x"), on_job=remember))
    assert told == ["operations/9"]
    provider, made = _veo(stored_bytes=b"VEO")
    result = asyncio.run(
        provider.generate_video(VideoRequest(model="veo-3.1-generate-preview", prompt="x"), resume_job="operations/9")
    )
    assert result.data == b"VEO" and made == []
    gone = genai_errors.ClientError(404, {"error": {"message": "Operation not found"}})
    provider, made = _veo(polls=[gone])
    with pytest.raises(VideoGenerationError, match="no longer knows the generation"):
        asyncio.run(provider.generate_video(VideoRequest(model="veo-3.1-generate-preview", prompt="x"), resume_job="op/1"))


def test_a_refusal_without_param_is_read_from_its_words_and_google_errors_from_their_details():
    from google.genai import errors as genai_errors

    words = _bad_request("The requested duration is not supported by this model")
    assert video.names_a_field(words, ("duration",)) is True
    assert video.names_a_field(_bad_request("The prompt was flagged"), ("duration", "size")) is False
    google = genai_errors.ClientError(400, {"error": {"message": "resolution 1080p is not supported", "param": "resolution"}})
    assert video.api_error_message(google) == "resolution 1080p is not supported"
    assert video.names_a_field(google, ("resolution",)) is True


# ── Which provider and model a clip uses ──────────────────────────────────


def _config(provider: str) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), provider=provider, display_name=None)


class _Db:
    def __init__(self, configs):
        self.configs = configs

    async def execute(self, _statement):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(self.configs)))


@pytest.fixture
def _resolution(monkeypatch):
    state = {"ps": SimpleNamespace(video_provider_id=None, video_model=None)}

    async def _ps(_db):
        return state["ps"]

    async def _cached(config):
        return f"provider:{config.provider}"

    async def _allowed(*_args, **_kwargs):
        return None

    monkeypatch.setattr(router, "_get_platform_settings", _ps)
    monkeypatch.setattr(router, "_cached_provider", _cached)
    monkeypatch.setattr(router, "ensure_not_excluded", _allowed)
    return state


def test_the_admins_choice_is_used(_resolution):
    gateway, openai_config = _config("litellm"), _config("openai")
    _resolution["ps"] = SimpleNamespace(video_provider_id=gateway.id, video_model="runwayml/gen4_turbo")
    provider, model, config = asyncio.run(router.get_video_provider(_Db([openai_config, gateway])))
    assert (provider, model, config) == ("provider:litellm", "runwayml/gen4_turbo", gateway)


def test_without_a_choice_the_first_provider_with_a_built_in_model(_resolution):
    gateway, gemini = _config("litellm"), _config("gemini")
    provider, model, config = asyncio.run(router.get_video_provider(_Db([gateway, gemini])))
    assert (provider, model, config) == ("provider:gemini", router.VIDEO_DEFAULTS["gemini"], gemini)


def test_the_callers_choice_wins_and_a_bare_gateway_needs_a_model(_resolution):
    gateway = _config("litellm")
    _p, model, config = asyncio.run(router.get_video_provider(_Db([gateway]), provider_id=gateway.id, model="veo"))
    assert (model, config) == ("veo", gateway)
    with pytest.raises(ValueError, match="Name the video model"):
        asyncio.run(router.get_video_provider(_Db([gateway]), provider_id=gateway.id))
    with pytest.raises(ValueError, match="No video provider with this id"):
        asyncio.run(router.get_video_provider(_Db([gateway]), provider_id=uuid.uuid4()))


def test_no_video_provider_says_where_to_configure_one(_resolution):
    with pytest.raises(ValueError, match="Video generation"):
        asyncio.run(router.get_video_provider(_Db([_config("anthropic")])))


def test_an_excluded_person_gets_no_video_provider(monkeypatch, _resolution):
    from app.llm.provider_access import ProviderNotUsableError

    async def _refuse(*_args, **_kwargs):
        raise ProviderNotUsableError("not for you")

    monkeypatch.setattr(router, "ensure_not_excluded", _refuse)
    with pytest.raises(ProviderNotUsableError):
        asyncio.run(router.get_video_provider(_Db([_config("openai")]), user_id=uuid.uuid4()))


# ── Models their provider retired ─────────────────────────────────


def test_a_model_is_retired_once_its_shutdown_date_has_passed():
    assert video.retired_since(SimpleNamespace(id="sora-2", shutdown_date="2020-01-01")) == "2020-01-01"
    assert video.retired_since({"id": "sora-2", "shutdown_date": "2020-01-01T00:00:00Z"}) == "2020-01-01"
    # Scheduled, not yet retired; not scheduled; not a date: all still served.
    assert video.retired_since(SimpleNamespace(id="x", shutdown_date="2999-01-01")) is None
    assert video.retired_since(SimpleNamespace(id="x")) is None
    assert video.retired_since(SimpleNamespace(id="x", shutdown_date="soon")) is None


class _Catalog(OpenAIVideosMixin):
    """An OpenAI-shaped endpoint whose model records carry their lifecycle."""

    def __init__(self, records, *, records_published=True):
        self.retrieved: list[str] = []

        async def listing():
            return SimpleNamespace(data=records)

        async def retrieve(model, **_kwargs):
            self.retrieved.append(model)
            if not records_published:
                raise _status(404, "no model records here")
            return next(r for r in records if r.id == model)

        # Its own base URL: the lifecycle cache is keyed by endpoint.
        self.client = SimpleNamespace(
            models=SimpleNamespace(list=listing, retrieve=retrieve), base_url=f"https://videos.test/{uuid.uuid4()}/"
        )


_RETIRED = SimpleNamespace(id="sora-2", shutdown_date="2020-01-01")
_LIVE = SimpleNamespace(id="veo-3.1-generate-preview", shutdown_date=None)


def test_a_retired_model_is_never_listed():
    catalog = _Catalog([_RETIRED, _LIVE, SimpleNamespace(id="gpt-4o")])
    assert asyncio.run(catalog.list_video_models()) == ["veo-3.1-generate-preview"]


def test_a_retired_model_says_since_when_and_the_answer_is_kept():
    catalog = _Catalog([_RETIRED, _LIVE])
    reason = asyncio.run(catalog.video_model_unavailable("sora-2"))
    assert reason == "sora-2 was retired by its provider on 2020-01-01"
    assert asyncio.run(catalog.video_model_unavailable("sora-2")) == reason
    assert catalog.retrieved == ["sora-2"]
    assert asyncio.run(catalog.video_model_unavailable("veo-3.1-generate-preview")) is None


def test_an_endpoint_that_publishes_no_model_records_is_not_known_to_retire_anything():
    assert asyncio.run(_Catalog([_RETIRED], records_published=False).video_model_unavailable("sora-2")) is None


def test_an_endpoint_that_no_longer_serves_videos_says_so():
    # What the SDK raises for a bare 404 (the retired Videos API answers with an empty body).
    response = httpx.Response(404, request=httpx.Request("POST", "https://api.example.com/v1/videos"))
    bare = openai.APIStatusError("Error code: 404", response=response, body=None)
    with pytest.raises(VideoGenerationError, match="no longer serves video generation"):
        asyncio.run(_Provider(_Videos(fail_with=bare)).generate_video(VideoRequest(model="sora-2", prompt="x")))


class _Stub:
    """A provider whose retired models are named."""

    def __init__(self, name, retired=(), listed=()):
        self.name, self.retired, self.listed = name, set(retired), list(listed)

    async def video_model_unavailable(self, model):
        return f"{model} was retired by its provider on 2026-09-24" if model in self.retired else None

    async def list_video_models(self):
        return self.listed


def _stubs(monkeypatch, stubs: dict):
    async def cached(config):
        return stubs[config.provider]

    monkeypatch.setattr(router, "_cached_provider", cached)


def test_a_retired_built_in_model_gives_way_to_the_next_live_provider(monkeypatch, _resolution):
    openai_config, gemini = _config("openai"), _config("gemini")
    _stubs(monkeypatch, {"openai": _Stub("openai", retired={router.VIDEO_DEFAULTS["openai"]}), "gemini": _Stub("gemini")})
    _p, model, config = asyncio.run(router.get_video_provider(_Db([openai_config, gemini])))
    assert (model, config) == (router.VIDEO_DEFAULTS["gemini"], gemini)


def test_a_retired_platform_choice_or_a_retired_named_model_is_refused_never_replaced(monkeypatch, _resolution):
    # One model per round: the platform's choice is never handed to another provider's live model.
    openai_config, gemini = _config("openai"), _config("gemini")
    _stubs(monkeypatch, {"openai": _Stub("openai", retired={"sora-2-pro", "sora-2"}), "gemini": _Stub("gemini")})
    _resolution["ps"] = SimpleNamespace(video_provider_id=openai_config.id, video_model="sora-2-pro")
    with pytest.raises(router.VideoModelUnusableError) as raised:
        asyncio.run(router.get_video_provider(_Db([openai_config, gemini])))
    assert str(raised.value) == (
        "sora-2-pro was retired by its provider on 2026-09-24 (the platform's choice in Settings > AI and models > Media > Video "
        "generation, on openai): pick another video model."
    )
    with pytest.raises(ValueError, match="sora-2-pro was retired by its provider on 2026-09-24: pick another"):
        asyncio.run(router.get_video_provider(_Db([openai_config, gemini]), provider_id=openai_config.id))
    # Its provider removed: said too, never another provider's.
    with pytest.raises(router.VideoModelUnusableError, match="The provider sora-2-pro was chosen on is no longer configured"):
        asyncio.run(router.get_video_provider(_Db([gemini])))


def test_with_every_built_in_model_retired_the_error_says_why_each_was_skipped(monkeypatch, _resolution):
    openai_config = _config("openai")
    _stubs(monkeypatch, {"openai": _Stub("openai", retired={router.VIDEO_DEFAULTS["openai"]})})
    with pytest.raises(ValueError) as raised:
        asyncio.run(router.get_video_provider(_Db([openai_config])))
    text = str(raised.value)
    assert f"{router.VIDEO_DEFAULTS['openai']} was retired" in text and "Video generation" in text


def test_settings_say_why_the_chosen_video_model_cannot_be_used(monkeypatch, _resolution):
    """Settings showed a retired model as active, with no way to change it."""
    openai_config, gemini = _config("openai"), _config("gemini")
    _stubs(monkeypatch, {"openai": _Stub("openai", retired={"sora-2-pro"}), "gemini": _Stub("gemini")})
    _resolution["ps"] = SimpleNamespace(video_provider_id=openai_config.id, video_model="sora-2-pro")
    problem = asyncio.run(router.platform_video_model_problem(_Db([openai_config, gemini])))
    assert problem == "sora-2-pro was retired by its provider on 2026-09-24"
    _resolution["ps"] = SimpleNamespace(video_provider_id=gemini.id, video_model="veo-3.0-generate-001")
    assert asyncio.run(router.platform_video_model_problem(_Db([openai_config, gemini]))) is None
    assert "no longer configured" in asyncio.run(router.platform_video_model_problem(_Db([openai_config])))
    _resolution["ps"] = SimpleNamespace(video_provider_id=None, video_model=None)
    assert asyncio.run(router.platform_video_model_problem(_Db([openai_config]))) is None


def test_a_gateway_listing_a_live_video_model_is_used_when_no_built_in_one_is(monkeypatch, _resolution):
    openai_config, gateway = _config("openai"), _config("litellm")
    _stubs(
        monkeypatch,
        {
            "openai": _Stub("openai", retired={router.VIDEO_DEFAULTS["openai"]}),
            "litellm": _Stub("litellm", listed=["vertex_ai/veo-3.1-generate-001"]),
        },
    )
    _p, model, config = asyncio.run(router.get_video_provider(_Db([openai_config, gateway])))
    assert (model, config) == ("vertex_ai/veo-3.1-generate-001", gateway)


# ── The platform setting ──────────────────────────────────────────────────


def test_the_setting_is_a_provider_reference_like_the_image_one():
    from app.api.v1 import settings as settings_api
    from app.api.v1.schemas.settings import GeneralAssistantConfigRequest, GeneralAssistantConfigResponse
    from app.models.platform_settings import PlatformSettings

    for column in ("video_provider", "video_provider_id", "video_model"):
        assert hasattr(PlatformSettings, column)
        assert column in GeneralAssistantConfigResponse.model_fields
        assert column in GeneralAssistantConfigRequest.model_fields
        assert column in settings_api._GA_PROVIDER_REFERENCE_FIELDS
    assert ("video_provider", "video_provider_id") in settings_api._GA_ALIGNED_PROVIDER_PAIRS
    assert any(field == "video_provider_id" for field, _scope, _label in settings_api._GA_PROVIDER_ID_FIELDS)
    paths = {route.path for route in settings_api.router.routes}
    assert any(path.endswith("/available-video-models") for path in paths)
