"""Gemini Omni Flash makes the Gemini API's AI clips. Synthetic interactions, no network.

* Gemini lists ``gemini-omni-*`` under video and starts with it, Veo being
  served by Vertex AI alone; Omni also answers in text, so it stays a chat model;
* what Omni takes is its provider's to say: reference images beside a start
  frame, no negative prompt field;
* a clip is a background interaction: its input declares each image's role
  and writes the length, the single shot, the sound and what to avoid; its id
  is the shot's job, polled until completed, its video decoded from the output;
* a resumed shot polls the interaction it started and never creates another;
  a refusal, a 404 and an interaction that failed say so.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re

import httpx
import pytest

from app.llm import router, video
from app.llm.gemini_provider import GeminiProvider
from app.llm.video import VideoGenerationError, VideoModelNotServedError, VideoProviderBusyError, VideoRequest

_REAL_CLIENT = httpx.AsyncClient
_MP4 = b"\x00\x00\x00\x18ftypmp42omni"
OMNI = "gemini-omni-1.1-flash"


def _listing(ids) -> GeminiProvider:
    provider = GeminiProvider(api_key="g-key")

    async def listed():
        return list(ids)

    provider._list_all_model_ids = listed
    return provider


def test_gemini_lists_omni_under_video_and_starts_with_it():
    gemini = _listing(["gemini-3.5-flash", OMNI, "gemini-omni-flash-preview", "gemini-3.8-flash-tts", "veo-3.1-generate-001"])
    assert asyncio.run(gemini.list_video_models()) == ["veo-3.1-generate-001", "gemini-omni-flash-preview", OMNI]
    # Omni answers in text too (the catalog lists it as chat): it stays a chat model as well.
    assert asyncio.run(gemini.list_models()) == ["gemini-omni-flash-preview", OMNI, "gemini-3.5-flash"]
    assert router.VIDEO_DEFAULTS["gemini"] == OMNI
    assert GeminiProvider._FALLBACK_VIDEO_MODELS == (OMNI,)


def test_what_omni_takes_is_declared_by_its_provider():
    gemini = GeminiProvider(api_key="g-key")
    assert gemini.max_reference_images(OMNI) == 3
    assert gemini.reference_images_with_start_frame(OMNI) is True
    assert gemini.takes_negative_prompt(OMNI) is False
    assert gemini.holds_locked_camera(OMNI, start_frame=True) is None
    # Veo keeps its own answers.
    assert gemini.max_reference_images("veo-3.1-generate-preview") == 3
    assert gemini.reference_images_with_start_frame("veo-3.1-generate-preview") is False
    assert gemini.takes_negative_prompt("veo-3.1-generate-preview") is True


def test_the_input_declares_each_image_and_writes_what_omni_has_no_field_for():
    request = VideoRequest(
        model=OMNI,
        prompt="  The cow walks past the booth  ",
        duration_seconds=6,
        negative_prompt="text overlays",
        reference_image=b"START",
        reference_mime="image/jpeg",
        sound=False,
        reference_images=[(b"LOGO", "image/png"), (b"COW", "")],
    )
    blocks = GeminiProvider._omni_input(request)
    assert [(b["type"], b.get("mime_type")) for b in blocks] == [
        ("image", "image/jpeg"),
        ("image", "image/png"),
        ("image", "image/png"),
        ("text", None),
    ]
    assert [base64.b64decode(b["data"]) for b in blocks[:3]] == [b"START", b"LOGO", b"COW"]
    assert blocks[-1]["text"].splitlines() == [
        "[# Sources <FIRST_FRAME>@Image1] [# References <IMAGE_REF_0>@Image2 <IMAGE_REF_1>@Image3] "
        "The cow walks past the booth",
        "In a single continuous shot, no scene cuts, 6 seconds long.",
        "No audio: no dialogue, no music, no sound effects.",
        "Do not show: text overlays.",
        "Use Image1 as the starting frame.",
        "Use the given images as references for video generation. "
        "The images should not be used as literal initial frames.",
    ]


def test_a_prompt_alone_is_sent_with_its_shot_and_length():
    blocks = GeminiProvider._omni_input(VideoRequest(model=OMNI, prompt="A cow in a meadow", duration_seconds=8))
    assert blocks == [
        {"type": "text", "text": "A cow in a meadow\nIn a single continuous shot, no scene cuts, 8 seconds long."}
    ]


def _completed(data: bytes = _MP4) -> dict:
    return {
        "id": "v1_abc",
        "status": "completed",
        "steps": [
            {"type": "user_input", "content": [{"type": "text", "text": "..."}]},
            {"type": "thought", "content": [{"type": "thought", "text": "..."}]},
            {"type": "model_output", "content": [{"type": "video", "mime_type": "video/mp4", "data": base64.b64encode(data).decode()}]},
        ],
    }


@pytest.fixture
def api(monkeypatch):
    """Gemini's REST API, scripted: ``api.answers[(method, path)]`` is the list of answers given in turn."""

    class _Api:
        def __init__(self):
            self.answers: dict[tuple[str, str], list[httpx.Response]] = {}
            self.requests: list[httpx.Request] = []

        def handle(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            queue = self.answers[(request.method, request.url.path)]
            return queue.pop(0) if len(queue) > 1 else queue[0]

    scripted = _Api()

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(scripted.handle)
        return _REAL_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setattr(video, "POLL_SECONDS", 0)
    return scripted


def _generate(request: VideoRequest, **kwargs):
    return asyncio.run(GeminiProvider(api_key="g-key").generate_video(request, **kwargs))


def test_a_clip_is_a_background_interaction_polled_until_its_video_comes(api):
    api.answers[("POST", "/v1beta/interactions")] = [httpx.Response(200, json={"id": "v1_abc", "status": "in_progress"})]
    api.answers[("GET", "/v1beta/interactions/v1_abc")] = [
        httpx.Response(200, json={"id": "v1_abc", "status": "in_progress"}),
        httpx.Response(200, json=_completed()),
    ]
    jobs: list[str] = []
    states: list[str] = []

    async def on_job(job_id):
        jobs.append(job_id)

    async def on_state(state):
        states.append(state.state)

    request = VideoRequest(model=OMNI, prompt="A cow", aspect_ratio="9:16", resolution="720p", duration_seconds=4)
    result = _generate(request, on_job=on_job, on_state=on_state)

    created = api.requests[0]
    body = json.loads(created.content)
    assert created.headers["x-goog-api-key"] == "g-key"
    assert body["model"] == OMNI and body["background"] is True
    assert body["response_format"] == {"type": "video", "aspect_ratio": "9:16", "resolution": "720p"}
    assert body["input"][-1]["text"].startswith("A cow\n")
    assert (result.data, result.job_id, jobs) == (_MP4, "v1_abc", ["v1_abc"])
    assert states == ["queued", "in_progress", "completed"]
    assert result.notes == [f"{OMNI} has no length setting: 4 s was asked in its prompt."]


def test_a_resumed_shot_polls_its_interaction_and_never_starts_another(api):
    api.answers[("GET", "/v1beta/interactions/v1_old")] = [httpx.Response(200, json=_completed(b"KEPT"))]
    result = _generate(VideoRequest(model=OMNI, prompt="A cow"), resume_job="v1_old")
    assert (result.data, result.job_id) == (b"KEPT", "v1_old")
    assert [(r.method, r.url.path) for r in api.requests] == [("GET", "/v1beta/interactions/v1_old")]


def test_an_interaction_a_resumed_shot_started_that_is_gone_says_so(api):
    api.answers[("GET", "/v1beta/interactions/v1_old")] = [httpx.Response(404, json={"error": {"message": "not found"}})]
    with pytest.raises(VideoGenerationError, match="no longer knows the generation this shot started"):
        _generate(VideoRequest(model=OMNI, prompt="A cow"), resume_job="v1_old")


@pytest.mark.parametrize(
    ("status", "raised", "said"),
    [
        pytest.param(400, VideoGenerationError, "refused the request (HTTP 400): the prompt was blocked", id="refused"),
        pytest.param(404, VideoModelNotServedError, "no video generation for gemini-omni-1.1-flash", id="not-served"),
        pytest.param(429, VideoProviderBusyError, "unavailable (HTTP 429)", id="busy"),
    ],
)
def test_a_refused_interaction_says_why_before_anything_is_polled(api, status, raised, said):
    reason = "the prompt was blocked" if status == 400 else "slow down"
    api.answers[("POST", "/v1beta/interactions")] = [httpx.Response(status, json={"error": {"message": reason}})]
    with pytest.raises(raised, match=re.escape(said)):
        _generate(VideoRequest(model=OMNI, prompt="A cow"))
    assert [r.method for r in api.requests] == ["POST"]


@pytest.mark.parametrize(
    ("answer", "said"),
    [
        pytest.param(
            {"id": "v1_abc", "status": "failed", "error": {"message": "safety filter"}}, "not made: safety filter", id="failed"
        ),
        pytest.param({"id": "v1_abc", "status": "cancelled"}, "the interaction was cancelled", id="cancelled"),
        pytest.param({"id": "v1_abc", "status": "completed", "steps": []}, "returned no video", id="no-video"),
    ],
)
def test_an_interaction_that_ends_without_a_video_says_why(api, answer, said):
    api.answers[("POST", "/v1beta/interactions")] = [httpx.Response(200, json={"id": "v1_abc", "status": "in_progress"})]
    api.answers[("GET", "/v1beta/interactions/v1_abc")] = [httpx.Response(200, json=answer)]
    with pytest.raises(VideoGenerationError, match=re.escape(said)):
        _generate(VideoRequest(model=OMNI, prompt="A cow"))
