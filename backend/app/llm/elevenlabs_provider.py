# SPDX-License-Identifier: Apache-2.0

"""ElevenLabs: voices, music, videos and images, never a chat model.

Its models are the ones ``GET /v1/models`` says can speak
(``can_do_text_to_speech``), its voices the account's voice library
(``GET /v2/voices``: premade voices and the account's generated, cloned and
professional voices), and speech ``POST /v1/text-to-speech/{voice_id}``.
Music is Eleven Music, ``POST /v1/music``: a prompt composed at the exact
length asked, instrumental on request (``force_instrumental``). A clip to
continue is uploaded (``POST /v1/music/upload``) and replayed by a
composition plan whose generation chunks follow it, conditioned on its end
(``conditioning_ref``): ``music_v2`` and ``music_v2_5`` only. Videos and
images go through the Image & Video API (``POST /v1/flows/video`` and
``/v1/flows/image``, then ``GET`` until the generation completes and its
signed ``content_url`` is downloaded), which needs a Pro plan and the key's
"Image & Video Generation" permission. It serves no chat (``only_capability``:
the capability its credentials are validated for, its voices), like the
embedding server; its video models are offered once an administrator picks
them, never pinned on their own (no ``default_video_model``).

A pronunciation lexicon reaches it two ways: inline
``<phoneme alphabet="ipa">`` tags on the models that honour them
(:data:`PHONEME_MODELS`), and a pronunciation dictionary of alias rules
(``POST /v1/pronunciation-dictionaries/add-from-rules``) every model reads,
referred to per request by ``pronunciation_dictionary_locators``.

Credential: the API key, or ``api_key||base_url`` for a regional endpoint
(``https://api.us.elevenlabs.io``, ``https://api.eu.residency.elevenlabs.io``,
``https://api.in.residency.elevenlabs.io``,
``https://api.sg.residency.elevenlabs.io``).
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.llm.images import ImageGenerationError, accepted_image, nearest_aspect_ratio
from app.llm.music import MusicGenerationError, MusicRequest, MusicResult, clamp_seconds, status_error
from app.llm.speech import (
    PronunciationSupport,
    SpeechGenerationError,
    SpeechRequest,
    SpeechResult,
    Voice,
    clamp_speed,
    pick_voice,
)
from app.llm.video import (
    JobState,
    VideoAPIError,
    VideoCredits,
    VideoGenerationError,
    VideoProviderBusyError,
    VideoRequest,
    VideoResult,
    await_job,
    download_video,
    fetch_signed_media,
    is_transient,
    normalised,
    snap_duration,
    snap_resolution,
)
from app.llm.video import status_error as video_status_error
from app.tools.base import ToolDefinition

#: How long the voice library read is reused: a voice cloned meanwhile shows within minutes.
VOICES_TTL_SECONDS = 300
#: The pages of the voice library read, 100 voices each.
_VOICE_PAGES = 10
#: The account's own voices come first: they are why ElevenLabs is configured.
_CATEGORY_ORDER = {"cloned": 0, "professional": 1, "generated": 2}
#: The neighbouring narration sent for continuity, at most.
_CONTEXT_CHARS = 1000
#: The Eleven Music models, newest first: the API lists them in no model read.
_MUSIC_MODELS = ("music_v2_5", "music_v2", "music_v1")
#: The lengths ``POST /v1/music`` composes, in milliseconds.
_MUSIC_MIN_MS = 3000
_MUSIC_MAX_MS = 600_000
#: A composition of minutes is answered once it is made.
_MUSIC_TIMEOUT_SECONDS = 600
#: The Eleven Music models whose composition plans replay an uploaded clip and compose after it.
_CONTINUING_MUSIC_MODELS = frozenset({"music_v2_5", "music_v2"})
#: A composition plan's chunk lasts 3 s to 2 min; a generation chunk is conditioned on 30 s of a clip at most.
_PLAN_CHUNK_MIN_MS = 3000
_PLAN_CHUNK_MAX_MS = 120_000
_CONDITIONING_MAX_MS = 30_000
#: A plan chunk's styles: phrases of the prompt, at most this many.
_PLAN_STYLES = 50
#: The speech models that honour ``<phoneme>`` tags; the others skip the word, and read alias rules only.
PHONEME_MODELS = frozenset({"eleven_flash_v2", "eleven_turbo_v2", "eleven_monolingual_v1"})
#: The pronunciation dictionaries one request may refer to.
_MAX_DICTIONARIES = 3


@dataclass(frozen=True)
class _VideoModel:
    """What a video model of the Image & Video API makes (its request schema)."""

    durations: tuple[int, ...]
    resolutions: tuple[str, ...]
    negative_prompt: bool = False


#: The video models the API serves for text and a start frame, cheapest Veo first.
#: The ByteDance (Seedance) ones need ElevenLabs to approve the account first.
_VIDEO_MODELS: dict[str, _VideoModel] = {
    "veo-3.1-fast-generate-001": _VideoModel((4, 6, 8), ("720p", "1080p"), negative_prompt=True),
    "veo-3.1-generate-001": _VideoModel((4, 6, 8), ("720p", "1080p"), negative_prompt=True),
    "bytedance-seedance-v2.5": _VideoModel(tuple(range(4, 31)), ("720p", "1080p")),
    "bytedance-seedance-v2": _VideoModel(tuple(range(4, 16)), ("720p", "1080p")),
    "bytedance-seedance-v2-fast": _VideoModel(tuple(range(4, 16)), ("720p",)),
    "bytedance-seedance-v2-mini": _VideoModel(tuple(range(4, 16)), ("720p",)),
}
_GPT_IMAGE_2_RATIOS = ("1:1", "4:5", "5:4", "3:4", "4:3", "2:3", "3:2", "1:2", "2:1", "9:16", "16:9", "21:9", "1:3", "3:1")
_GEMINI_IMAGE_RATIOS = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")
#: The image models the API serves and the aspect ratios each draws at.
_IMAGE_MODELS: dict[str, tuple[str, ...]] = {
    "gpt-image-2.5-sunburst": _GPT_IMAGE_2_RATIOS,
    "gpt-image-2.5-flare": _GPT_IMAGE_2_RATIOS,
    "gpt-image-2": _GPT_IMAGE_2_RATIOS,
    "gemini-3.1-flash-image": (*_GEMINI_IMAGE_RATIOS, "1:4", "4:1", "1:8", "8:1"),
    "gemini-3-pro-image": _GEMINI_IMAGE_RATIOS,
    "gemini-3.1-flash-lite-image": _GEMINI_IMAGE_RATIOS,
    "gemini-2.5-flash-image": _GEMINI_IMAGE_RATIOS,
    "gpt-image-1.5": ("1:1", "3:2", "2:3"),
    "gpt-image-1": ("1:1", "3:2", "2:3"),
    "bytedance-seedream-5-pro": ("1:1", "3:4", "16:9", "4:3", "9:16"),
    "bytedance-seedream-5-lite": ("1:1", "3:4", "16:9", "4:3", "9:16"),
}
#: The start-frame types the API takes inline.
_INLINE_IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/heic", "image/heif")
#: An image is drawn within this, its generation asked about this often.
_IMAGE_TIMEOUT_SECONDS = 300
_IMAGE_POLL_SECONDS = 3.0
_MAX_IMAGE_BYTES = 64 * 1024 * 1024


class ElevenLabsProvider(BaseLLMProvider):
    """Speech and music provider for the ElevenLabs API."""

    provider_name = "elevenlabs"
    only_capability = "speech"
    default_speech_model = "eleven_multilingual_v2"
    default_music_model = _MUSIC_MODELS[0]
    base_url = "https://api.elevenlabs.io"

    def __init__(self, api_key: str, **kwargs):
        del kwargs
        parts = api_key.split("||")
        self._key = parts[0].strip() if parts else ""
        if len(parts) >= 2 and parts[1].strip():
            self.base_url = parts[1].strip().rstrip("/").removesuffix("/v1")
        self._voices: list[Voice] = []
        self._voices_read_at = 0.0

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, headers={"xi-api-key": self._key}, timeout=timeout)

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolDefinition] | None = None,
        **kwargs,
    ) -> ChatResponse:
        del messages, model, temperature, max_tokens, tools, kwargs
        raise NotImplementedError("ElevenLabs serves speech only.")

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        del text, model
        raise NotImplementedError("ElevenLabs serves speech only.")

    async def list_models(self) -> list[str]:
        """No chat model: the speech models are :meth:`list_speech_models`."""
        return []

    async def list_speech_models(self) -> list[str]:
        async with self._client(15) as http:
            response = await http.get("/v1/models")
        _raise_for(response)
        return [
            str(m["model_id"])
            for m in response.json() or []
            if isinstance(m, dict) and m.get("model_id") and m.get("can_do_text_to_speech")
        ]

    async def list_voices(self, model: str | None = None) -> list[Voice]:
        """The account's voice library, its own voices (cloned, professional, generated) first."""
        del model
        if self._voices and time.monotonic() - self._voices_read_at < VOICES_TTL_SECONDS:
            return list(self._voices)
        voices: list[Voice] = []
        token: str | None = None
        async with self._client(15) as http:
            for _ in range(_VOICE_PAGES):
                params: dict[str, str | int] = {"page_size": 100, "include_total_count": "false"}
                if token:
                    params["next_page_token"] = token
                response = await http.get("/v2/voices", params=params)
                _raise_for(response)
                data = response.json() or {}
                voices.extend(v for item in data.get("voices") or [] if (v := account_voice(item)))
                token = data.get("next_page_token")
                if not data.get("has_more") or not token:
                    break
        voices = own_voices_first(voices)
        self._voices, self._voices_read_at = voices, time.monotonic()
        return list(voices)

    def speech_takes_style(self, model: str) -> bool | None:
        """ElevenLabs takes no written delivery instruction: the voice carries the delivery."""
        return False

    def speech_pronunciation(self, model: str) -> PronunciationSupport:
        """Inline IPA phonemes on the models that honour them; a pronunciation dictionary on every model."""
        if model in PHONEME_MODELS:
            return PronunciationSupport(ssml="inline", phoneme=True, lexicon=True)
        return PronunciationSupport(lexicon=True)

    async def register_pronunciation_lexicon(self, name: str, rules) -> str:
        """A pronunciation dictionary of alias rules: ``<dictionary id>:<version id>``."""
        body = {
            "name": name[:100],
            "rules": [
                {
                    "type": "alias",
                    "string_to_replace": rule.text,
                    "alias": rule.alias,
                    "case_sensitive": rule.case_sensitive,
                    "word_boundaries": True,
                }
                for rule in rules
            ],
        }
        async with self._client(30) as http:
            response = await http.post("/v1/pronunciation-dictionaries/add-from-rules", json=body)
        _raise_for(response)
        data = response.json() or {}
        if not data.get("id") or not data.get("version_id"):
            raise SpeechGenerationError("ElevenLabs kept no pronunciation dictionary.")
        return f"{data['id']}:{data['version_id']}"

    async def synthesize_speech(self, request: SpeechRequest) -> SpeechResult:
        notes: list[str] = []
        voice = pick_voice(await self.list_voices(), request.voice, None, notes)
        body: dict = {"text": request.text, "model_id": request.model}
        speed = clamp_speed(request.speed)
        if abs(speed - 1.0) > 1e-3:
            body["voice_settings"] = {"speed": speed}
        if request.previous_text:
            body["previous_text"] = request.previous_text[-_CONTEXT_CHARS:]
        if request.next_text:
            body["next_text"] = request.next_text[:_CONTEXT_CHARS]
        if request.lexicons:
            body["pronunciation_dictionary_locators"] = [
                {"pronunciation_dictionary_id": dictionary, "version_id": version}
                for dictionary, _sep, version in (locator.partition(":") for locator in request.lexicons[:_MAX_DICTIONARIES])
            ]
        if request.style:
            notes.append("ElevenLabs takes no written delivery instruction: the voice carries the delivery.")
        # Every plan gets 44.1 kHz MP3 (WAV at that rate needs a Pro plan).
        async with self._client(180) as http:
            response = await http.post(
                f"/v1/text-to-speech/{quote(voice, safe='')}",
                params={"output_format": "mp3_44100_128"},
                json=body,
            )
        _raise_for(response)
        return SpeechResult(response.content, "audio/mpeg", voice, notes)

    async def list_music_models(self) -> list[str]:
        return list(_MUSIC_MODELS)

    def continues_music(self, model: str) -> bool:
        return model in _CONTINUING_MUSIC_MODELS

    async def generate_music(self, request: MusicRequest) -> MusicResult:
        """Compose with Eleven Music: the length asked, instrumental when asked (``force_instrumental``)."""
        if request.continue_from is not None:
            return await self._continue_music(request)
        prompt = request.prompt.strip()
        if request.negative_prompt:
            # The prompt mode takes no negative field: the model reads it in the prompt.
            prompt = f"{prompt}\nAvoid: {request.negative_prompt.strip()}"
        length_ms = round(clamp_seconds(request.duration_seconds) * 1000)
        body = {
            "prompt": prompt[:4000],
            "model_id": request.model,
            "music_length_ms": max(_MUSIC_MIN_MS, min(_MUSIC_MAX_MS, length_ms)),
            "force_instrumental": bool(request.instrumental),
        }
        try:
            async with self._client(_MUSIC_TIMEOUT_SECONDS) as http:
                response = await http.post("/v1/music", json=body)
        except httpx.HTTPError as exc:
            raise status_error(None, type(exc).__name__, vendor="ElevenLabs") from None
        if response.status_code >= 400:
            raise status_error(response.status_code, _reason(response), vendor="ElevenLabs")
        if not response.content:
            raise MusicGenerationError("ElevenLabs returned no music.")
        mime = (response.headers.get("content-type") or "audio/mpeg").split(";", 1)[0].strip()
        return MusicResult(response.content, mime if mime.startswith("audio/") else "audio/mpeg")

    async def _continue_music(self, request: MusicRequest) -> MusicResult:
        """The music after ``request.continue_from``: the clip uploaded, then a composition plan replaying it
        unchanged and composing ``duration_seconds`` after it - its generation chunks conditioned on the clip's end.

        The answer replays the clip first: :attr:`MusicResult.continues_at` is its length.
        """
        clip = request.continue_from
        if not self.continues_music(request.model):
            raise MusicGenerationError(
                f"{request.model} does not continue a clip: {' and '.join(sorted(_CONTINUING_MUSIC_MODELS))} do."
            )
        suffix = ".wav" if "wav" in clip.mime else ".mp3"
        try:
            async with self._client(_MUSIC_TIMEOUT_SECONDS) as http:
                uploaded = await http.post(
                    "/v1/music/upload", files={"file": (f"continue-from{suffix}", clip.audio, clip.mime)}
                )
                if uploaded.status_code >= 400:
                    raise status_error(uploaded.status_code, _reason(uploaded), vendor="ElevenLabs")
                try:
                    song_id = str((uploaded.json() or {}).get("song_id") or "")
                except (ValueError, AttributeError):
                    song_id = ""
                if not song_id:
                    raise MusicGenerationError("ElevenLabs kept no song for the clip to continue.")
                plan, replayed_ms = _continuation_plan(request, song_id)
                response = await http.post("/v1/music", json={"model_id": request.model, "composition_plan": plan})
        except httpx.HTTPError as exc:
            raise status_error(None, type(exc).__name__, vendor="ElevenLabs") from None
        if response.status_code >= 400:
            raise status_error(response.status_code, _reason(response), vendor="ElevenLabs")
        if not response.content:
            raise MusicGenerationError("ElevenLabs returned no music.")
        mime = (response.headers.get("content-type") or "audio/mpeg").split(";", 1)[0].strip()
        return MusicResult(
            response.content, mime if mime.startswith("audio/") else "audio/mpeg", continues_at=replayed_ms / 1000
        )

    # ── Image & Video API ─────────────────────────────────────────

    async def _flows(self, method: str, path: str, body: dict | None = None) -> dict:
        """One call to the Image & Video API, raising :class:`VideoAPIError`."""
        try:
            async with self._client(30) as http:
                response = await http.request(method, path, json=body)
        except httpx.HTTPError as exc:
            raise VideoAPIError(None, f"ElevenLabs could not be reached ({type(exc).__name__})") from None
        if response.status_code >= 400:
            raise VideoAPIError.of(response, _reason(response))
        try:
            data = response.json()
        except ValueError:
            data = None
        return data if isinstance(data, dict) else {}

    def _generation(self, kind: str, generation_id: str) -> Callable[[], Awaitable[JobState]]:
        """Reads the state of a ``video`` or ``image`` generation."""

        async def read() -> JobState:
            data = await self._flows("GET", f"/v1/flows/{kind}/{quote(generation_id, safe='')}")
            status = data.get("status")
            if status == "completed":
                return JobState(True, url=data.get("content_url") or None, error="ElevenLabs returned no file")
            if status == "failed":
                reason = " - ".join(str(part) for part in (data.get("failure_reason"), data.get("error_message")) if part)
                return JobState(True, error=reason or "ElevenLabs gave no reason")
            return JobState(False, state="queued" if status == "pending" else str(status or "generating"))

        return read

    async def list_video_models(self) -> list[str]:
        return list(_VIDEO_MODELS)

    async def _credits_left(self) -> int | None:
        """The account's credits left: its subscription's limit less what it used (``GET /v1/user/subscription``, the
        key's ``user_read`` permission); ``None`` when it cannot be read. One pool pays for music and video alike."""
        try:
            async with self._client(15) as http:
                response = await http.get("/v1/user/subscription")
            if response.status_code >= 400:
                return None
            data = response.json() or {}
            remaining = int(data["character_limit"]) - int(data["character_count"])
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
            return None
        return max(0, remaining)

    async def music_credits(self, model: str) -> int | None:
        """The account's credits left (:meth:`_credits_left`): what a composition costs is never published."""
        del model
        return await self._credits_left()

    async def video_credits(self, request: VideoRequest) -> VideoCredits | None:
        """The account's credits left (:meth:`_credits_left`). What a generation costs is never published:
        ``needed`` is ``None``, the cost a refusal named is the cooling's (``provider_cooling``)."""
        del request
        remaining = await self._credits_left()
        return VideoCredits(remaining=remaining) if remaining is not None else None

    def takes_negative_prompt(self, model: str) -> bool:
        """The models whose schema has ``negative_prompt``; a model this table does not know is sent one."""
        spec = _VIDEO_MODELS.get(model)
        return spec is None or spec.negative_prompt

    async def generate_video(
        self, request: VideoRequest, *, progress=None, on_job=None, resume_job=None, on_state=None
    ) -> VideoResult:
        """A video from the Image & Video API: started, polled until completed, downloaded."""
        notes: list[str] = []
        request = normalised(request, notes)
        if resume_job:
            read = self._generation("video", resume_job)
            url = await await_job(read, request.model, progress, resumed=True, on_state=on_state)
            return VideoResult(await download_video(url, request.model), str(resume_job), notes)
        body = self._video_body(request, notes)
        try:
            created = await self._flows("POST", "/v1/flows/video", body)
        except VideoAPIError as exc:
            # The account's credits cannot pay for it: a quota, waited for - never a refusal of the shot.
            raise _quota_refusal(exc, request.model) or video_status_error(exc.code, exc, request.model) from None
        generation_id = str(created.get("id") or "")
        if not generation_id:
            raise VideoGenerationError("ElevenLabs started no generation it can be asked about.")
        # The generation exists upstream from here: a later failure resumes it, never pays twice.
        if on_job is not None:
            await on_job(generation_id)
        url = await await_job(self._generation("video", generation_id), request.model, progress, on_state=on_state)
        return VideoResult(await download_video(url, request.model), generation_id, notes)

    @staticmethod
    def _video_body(request: VideoRequest, notes: list[str]) -> dict:
        """The request body: each model's lengths, resolutions and negative prompt applied, and said."""
        body: dict = {"model_id": request.model, "prompt": request.prompt, "aspect_ratio": request.aspect_ratio}
        spec = _VIDEO_MODELS.get(request.model)
        if spec is None:
            body.update(duration_secs=request.duration_seconds, resolution=request.resolution)
            if request.negative_prompt:
                body["negative_prompt"] = request.negative_prompt
        else:
            body["duration_secs"] = snap_duration(request.duration_seconds, spec.durations, notes)
            body["resolution"] = snap_resolution(request.resolution, spec.resolutions, notes)
            if request.negative_prompt and spec.negative_prompt:
                body["negative_prompt"] = request.negative_prompt
            elif request.negative_prompt:
                notes.append("The model takes no negative prompt: what to avoid belongs in the prompt.")
        if request.reference_image:
            try:
                data, mime = accepted_image(request.reference_image, request.reference_mime, _INLINE_IMAGE_TYPES)
            except ImageGenerationError as exc:
                raise VideoGenerationError(str(exc)) from None
            body["start_frame"] = {
                "type": "inline_base64",
                "content_base64": base64.b64encode(data).decode("ascii"),
                "mime_type": mime,
            }
        return body

    async def list_image_models(self) -> list[str]:
        return list(_IMAGE_MODELS)

    async def generate_image(self, prompt: str, model: str | None = None, size: str = "1024x1024") -> bytes:
        """An image from the Image & Video API, at the ratio of *size* the model offers nearest."""
        model = model or next(iter(_IMAGE_MODELS))
        body: dict = {"model_id": model, "prompt": prompt}
        if ratios := _IMAGE_MODELS.get(model):
            body["aspect_ratio"] = nearest_aspect_ratio(size, ratios)
        try:
            created = await self._flows("POST", "/v1/flows/image", body)
        except VideoAPIError as exc:
            raise ImageGenerationError(f"ElevenLabs refused the image (HTTP {exc.code or 'error'}): {exc.message}") from None
        generation_id = str(created.get("id") or "")
        if not generation_id:
            raise ImageGenerationError("ElevenLabs started no image generation it can be asked about.")
        read = self._generation("image", generation_id)
        deadline = time.monotonic() + _IMAGE_TIMEOUT_SECONDS
        while True:
            await asyncio.sleep(_IMAGE_POLL_SECONDS)
            try:
                state = await read()
            except VideoAPIError as exc:
                if not is_transient(exc.code) or time.monotonic() > deadline:
                    raise ImageGenerationError(f"ElevenLabs failed the image (HTTP {exc.code or 'error'}): {exc.message}") from None
                continue
            if state.finished:
                break
            if time.monotonic() > deadline:
                raise ImageGenerationError(f"ElevenLabs did not draw the image within {_IMAGE_TIMEOUT_SECONDS // 60} minutes.")
        if not state.url:
            raise ImageGenerationError(f"The image was not drawn: {state.error}.")
        try:
            data = await fetch_signed_media(state.url, max_bytes=_MAX_IMAGE_BYTES)
        except VideoAPIError as exc:
            raise ImageGenerationError(f"The drawn image could not be downloaded: {exc.message}.") from None
        if not data:
            raise ImageGenerationError("ElevenLabs returned an empty image.")
        return data


def account_voice(item: Any) -> Voice | None:
    """A voice of the account's library: ``voice_id`` as ElevenLabs names it, ``id`` as the gateway's listing does."""
    voice_id = (item.get("voice_id") or item.get("id")) if isinstance(item, dict) else None
    if not voice_id:
        return None
    labels = item.get("labels") if isinstance(item.get("labels"), dict) else {}
    traits = [str(labels[k]) for k in ("accent", "gender", "age", "descriptive", "use_case") if labels.get(k)]
    return Voice(
        str(voice_id),
        str(item.get("name") or voice_id),
        str(item.get("category") or ""),
        ", ".join(traits) or str(item.get("description") or ""),
    )


def own_voices_first(voices: list[Voice]) -> list[Voice]:
    """The account's own voices (cloned, professional, generated) first, each group in the listed order."""
    return sorted(voices, key=lambda v: _CATEGORY_ORDER.get(v.category, len(_CATEGORY_ORDER)))


def _styles(text: str | None) -> list[str]:
    """A prompt as a plan chunk's styles: its phrases, split where it puts a comma, a semicolon or a line."""
    phrases = (part.strip(" .") for part in (text or "").replace(";", ",").replace("\n", ",").split(","))
    return [phrase[:200] for phrase in phrases if phrase][:_PLAN_STYLES]


def _continuation_plan(request: MusicRequest, song_id: str) -> tuple[dict, int]:
    """``(composition plan, milliseconds it replays)`` continuing the uploaded clip *song_id*.

    The clip's last 2 minutes at most replayed unchanged, then ``duration_seconds`` composed after it in chunks of
    2 minutes at most, the first conditioned on the clip's last 30 s: the last chunk is the outro.
    """
    clip_ms = round(request.continue_from.seconds * 1000)
    replayed = (max(0, clip_ms - _PLAN_CHUNK_MAX_MS), clip_ms)
    total = max(_PLAN_CHUNK_MIN_MS, round(clamp_seconds(request.duration_seconds) * 1000))
    count = -(-total // _PLAN_CHUNK_MAX_MS)
    positive = _styles(request.prompt) or ["instrumental bed"]
    negative = _styles(request.negative_prompt)
    if request.instrumental:
        positive, negative = [*positive, "instrumental"][:_PLAN_STYLES], [*negative, "vocals"][:_PLAN_STYLES]
    chunks: list[dict] = [{"song_id": song_id, "range": {"start_ms": replayed[0], "end_ms": replayed[1]}}]
    for index in range(count):
        length = total // count + (total % count if index == count - 1 else 0)
        chunk = {
            "text": "[Outro]" if index == count - 1 else "[Continuation]",
            "duration_ms": max(_PLAN_CHUNK_MIN_MS, length),
            "positive_styles": positive,
            "negative_styles": negative,
            "context_adherence": "high",
        }
        if index == 0:
            chunk["conditioning_ref"] = {
                "song_id": song_id,
                "range": {"start_ms": max(replayed[0], clip_ms - _CONDITIONING_MAX_MS), "end_ms": clip_ms},
            }
            chunk["condition_strength"] = "high"
        chunks.append(chunk)
    return {"chunks": chunks}, replayed[1] - replayed[0]


def _reason(response: httpx.Response) -> str:
    """ElevenLabs' reason for a refusal (its ``detail``), never the key."""
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, dict):
        reason = detail.get("message") or detail.get("status") or ""
    elif isinstance(detail, list) and detail and isinstance(detail[0], dict):
        reason = detail[0].get("msg") or ""
    else:
        reason = detail or ""
    return str(reason)[:300]


#: The credits an ElevenLabs quota refusal says the account has left and the request needs, in its ``detail.message``:
#: "You have 3448 credits remaining, while 7272 credits are required for this request."
_CREDITS_LEFT = re.compile(r"([\d,]+) credits remaining")
_CREDITS_NEEDED = re.compile(r"([\d,]+) credits are required")


def _credits_in(message: str, pattern: re.Pattern[str]) -> int | None:
    found = pattern.search(message)
    return int(found.group(1).replace(",", "")) if found else None


def _quota_refusal(exc: VideoAPIError, model: str) -> VideoProviderBusyError | None:
    """ElevenLabs' refusal of a request its account's credits cannot pay for (``detail.status: quota_exceeded``, on a
    401), as a quota the provider cools for: the credits it says are left and needed carried. ``None`` for any
    other refusal."""
    detail = exc.payload.get("detail") if isinstance(exc.payload, dict) else None
    if not isinstance(detail, dict) or detail.get("status") != "quota_exceeded":
        return None
    said = str(detail.get("message") or "")
    return VideoProviderBusyError(
        f"ElevenLabs refused {model} for its quota (HTTP {exc.code}): {exc.message}",
        status_code=exc.code,
        retry_after=exc.retry_after,
        quota=True,
        remaining=_credits_in(said, _CREDITS_LEFT),
        needed=_credits_in(said, _CREDITS_NEEDED),
    )


def _raise_for(response: httpx.Response) -> None:
    """A refused call as a ``SpeechGenerationError`` naming ElevenLabs' reason, never the key."""
    if response.status_code < 400:
        return
    if response.status_code == 401:
        raise SpeechGenerationError("ElevenLabs refused the API key.")
    reason = _reason(response)
    suffix = f": {reason}" if reason else ""
    raise SpeechGenerationError(f"ElevenLabs refused the request (HTTP {response.status_code}){suffix}")
