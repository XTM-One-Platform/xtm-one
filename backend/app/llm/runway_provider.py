# SPDX-License-Identifier: Apache-2.0

"""Runway: video generation, never a chat model.

``POST /v1/text_to_video`` - ``image_to_video`` from a start image - creates a
task (``{"id": ...}``) whose body is a union keyed on ``model`` (each model its
own ratios, lengths and fields); ``GET /v1/tasks/{id}`` reads it until
``status`` is ``SUCCEEDED`` (the signed URLs in ``output``) or ``FAILED`` /
``CANCELLED`` (``failure``, ``failureCode``). ``THROTTLED`` is a task queued
behind the account's limits, not an error. Every request carries
``X-Runway-Version``; a refusal is ``{"error": ..., "issues": [...]}``.

Seedance 2 (``seedance2``) keeps the subjects of reference images
(``references: [{"type": "image", "uri": ...}]``) - a shot's
ingredients - and, like Runway's reference mode, not beside a literal first
frame: a shot with a start frame names them in its prompt instead
(``clips.ingredient_path``).

Credential: a Runway API key (``key_...``, Bearer), or ``api_key||base_url``.
It serves video only (``only_capability``); the key is validated by reading the
organization (free), whose tier names the models the account may run, so the
models offered are the ones it can use. Gen-4.5 is the default and Runway's
flagship.
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.llm.images import ImageGenerationError, accepted_image
from app.llm.video import (
    JobState,
    VideoAPIError,
    VideoGenerationError,
    VideoRequest,
    VideoResult,
    await_job,
    download_video,
    normalised,
    snap_duration,
    snap_resolution,
    status_error,
)
from app.tools.base import ToolDefinition

_DEFAULT_BASE_URL = "https://api.dev.runwayml.com"
_API_VERSION = "2024-11-06"


@dataclass(frozen=True)
class _RunwayModel:
    durations: tuple[int, ...]
    resolutions: tuple[str, ...]
    #: Makes a video from the prompt alone (``text_to_video``), not only from a start image.
    text: bool
    #: Takes ``negativePrompt`` and makes its own sound (``audio``).
    negative_prompt: bool = False
    audio: bool = False
    #: Reference images it keeps the subjects of (``references``), never beside a literal first frame.
    references: int = 0


#: The models offered, flagship first; each one's own lengths and resolutions.
_MODELS: dict[str, _RunwayModel] = {
    "gen4.5": _RunwayModel(tuple(range(2, 11)), ("720p",), text=True),
    "gen4_turbo": _RunwayModel(tuple(range(2, 11)), ("720p",), text=False),
    "veo3.1": _RunwayModel((4, 6, 8), ("720p", "1080p"), text=True, negative_prompt=True, audio=True),
    "veo3.1_fast": _RunwayModel((4, 6, 8), ("720p", "1080p"), text=True, negative_prompt=True, audio=True),
    "seedance2": _RunwayModel(tuple(range(4, 16)), ("720p",), text=True, audio=True, references=9),
}
#: The ``ratio`` of an aspect ratio at a resolution: Runway names the frame in pixels.
_RATIOS = {
    ("16:9", "720p"): "1280:720",
    ("9:16", "720p"): "720:1280",
    ("16:9", "1080p"): "1920:1080",
    ("9:16", "1080p"): "1080:1920",
}
#: The start-image types Runway takes in a data URI.
_IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp")
#: A prompt's limit, in UTF-16 code units (how Runway measures it).
_MAX_PROMPT_UNITS = 1000


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _clip_prompt(text: str, notes: list[str]) -> str:
    if _utf16_units(text) <= _MAX_PROMPT_UNITS:
        return text
    clipped = text[:_MAX_PROMPT_UNITS]
    while _utf16_units(clipped) > _MAX_PROMPT_UNITS:
        clipped = clipped[:-1]
    notes.append(f"The prompt was cut to the {_MAX_PROMPT_UNITS} characters Runway reads.")
    return clipped


class RunwayProvider(BaseLLMProvider):
    """Video provider for the Runway API."""

    provider_name = "runway"
    only_capability = "video"
    default_video_model = "gen4.5"
    base_url = _DEFAULT_BASE_URL

    def __init__(self, api_key: str, **kwargs):
        del kwargs
        parts = api_key.split("||")
        self._key = parts[0].strip() if parts else ""
        if len(parts) >= 2 and parts[1].strip():
            self.base_url = parts[1].strip().rstrip("/").removesuffix("/v1")

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
        raise NotImplementedError("Runway serves video generation only.")

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        del text, model
        raise NotImplementedError("Runway serves video generation only.")

    async def list_models(self) -> list[str]:
        """No chat model: the video models are :meth:`list_video_models`."""
        return []

    async def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        """One call to the Runway API, raising :class:`VideoAPIError` on a refusal."""
        headers = {"Authorization": f"Bearer {self._key}", "X-Runway-Version": _API_VERSION}
        try:
            async with httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=30) as http:
                response = await http.request(method, path, json=body)
        except httpx.HTTPError as exc:
            raise VideoAPIError(None, f"Runway could not be reached ({type(exc).__name__})") from None
        try:
            data = response.json()
        except ValueError:
            data = None
        answer = data if isinstance(data, dict) else {}
        if response.status_code < 400:
            return answer
        reason = str(answer.get("error") or "")
        issues = [issue for issue in answer.get("issues") or [] if isinstance(issue, dict)]
        if issues:
            path_named = ".".join(str(part) for part in issues[0].get("path") or [])
            detail = str(issues[0].get("message") or "")
            reason = f"{reason}: {path_named + ' - ' if path_named else ''}{detail}".strip(": ")
        raise VideoAPIError.of(response, reason[:400])

    def max_reference_images(self, model: str) -> int:
        spec = _MODELS.get(model)
        return spec.references if spec is not None else 0

    def reference_images_with_start_frame(self, model: str) -> bool:
        """Runway's reference mode and a positioned first frame exclude each other."""
        return False
    def takes_negative_prompt(self, model: str) -> bool:
        """The models whose schema has ``negativePrompt`` (an unknown model is sent none)."""
        spec = _MODELS.get(model)
        return spec is not None and spec.negative_prompt

    async def list_video_models(self) -> list[str]:
        """The models offered that the account's tier may run, once the key reads the organization."""
        try:
            organization = await self._call("GET", "/v1/organization")
        except VideoAPIError as exc:
            if exc.code == 401:
                raise VideoGenerationError("Runway refused the API key.") from None
            raise VideoGenerationError(f"Runway refused the request (HTTP {exc.code or 'error'}): {exc.message}") from None
        tier = organization.get("tier") if isinstance(organization.get("tier"), dict) else {}
        allowed = tier.get("models") if isinstance(tier.get("models"), dict) else None
        return [model for model in _MODELS if allowed is None or model in allowed]

    def _task(self, task_id: str) -> Callable[[], Awaitable[JobState]]:
        async def read() -> JobState:
            task = await self._call("GET", f"/v1/tasks/{quote(task_id, safe='')}")
            status = task.get("status")
            if status == "SUCCEEDED":
                output = [url for url in task.get("output") or [] if isinstance(url, str) and url]
                return JobState(True, url=output[0] if output else None, error="Runway returned no video")
            if status in ("FAILED", "CANCELLED"):
                reason = " - ".join(str(part) for part in (task.get("failureCode"), task.get("failure")) if part)
                return JobState(True, error=reason or f"the task was {status.lower()}")
            progress = task.get("progress")
            return JobState(
                False,
                progress=int(progress * 100) if isinstance(progress, int | float) else None,
                # A throttled task is queued behind the account's limits, not refused.
                state="queued" if status in ("PENDING", "THROTTLED") else str(status or "running").lower(),
            )

        return read

    async def generate_video(
        self, request: VideoRequest, *, progress=None, on_job=None, resume_job=None, on_state=None
    ) -> VideoResult:
        """A Runway video: the task created, polled until it ends, its video downloaded."""
        notes: list[str] = []
        request = normalised(request, notes)
        if resume_job:
            url = await await_job(self._task(resume_job), request.model, progress, resumed=True, on_state=on_state)
            return VideoResult(await download_video(url, request.model), str(resume_job), notes)
        path, body = self._video_body(request, notes)
        try:
            created = await self._call("POST", path, body)
        except VideoAPIError as exc:
            raise status_error(exc.code, exc, request.model) from None
        task_id = str(created.get("id") or "")
        if not task_id:
            raise VideoGenerationError("Runway started no task it can be asked about.")
        # The task exists upstream from here: a later failure resumes it, never pays twice.
        if on_job is not None:
            await on_job(task_id)
        url = await await_job(self._task(task_id), request.model, progress, on_state=on_state)
        return VideoResult(await download_video(url, request.model), task_id, notes)

    @staticmethod
    def _video_body(request: VideoRequest, notes: list[str]) -> tuple[str, dict]:
        """The endpoint and body: each model's lengths, frame and fields applied, and said."""
        spec = _MODELS.get(request.model)
        if spec is not None and not spec.text and not request.reference_image:
            raise VideoGenerationError(
                f"{request.model} animates a start image: give the shot one, or pick a model that starts from text."
            )
        resolution = request.resolution
        body: dict = {"model": request.model, "promptText": _clip_prompt(request.prompt, notes)}
        if spec is None:
            body["duration"] = request.duration_seconds
        else:
            body["duration"] = snap_duration(request.duration_seconds, spec.durations, notes)
            resolution = snap_resolution(request.resolution, spec.resolutions, notes)
            if request.negative_prompt and spec.negative_prompt:
                body["negativePrompt"] = _clip_prompt(request.negative_prompt, notes)
            elif request.negative_prompt:
                notes.append("The model takes no negative prompt: what to avoid belongs in the prompt.")
            if spec.audio:
                body["audio"] = request.sound is not False
        body["ratio"] = _RATIOS.get((request.aspect_ratio, resolution), "1280:720")
        if request.reference_images:
            if spec is None or not spec.references:
                raise VideoGenerationError(f"{request.model} takes no reference images: seedance2 does.")
            if request.reference_image:
                raise VideoGenerationError(f"{request.model} takes reference images or a start frame, not both.")
            body["references"] = [{"type": "image", "uri": _data_uri(data, mime)} for data, mime in request.reference_images]
        if not request.reference_image:
            return "/v1/text_to_video", body
        body["promptImage"] = _data_uri(request.reference_image, request.reference_mime)
        return "/v1/image_to_video", body


def _data_uri(data: bytes, mime: str | None) -> str:
    """A picture as the ``data:`` URI Runway reads, in a type it takes."""
    try:
        accepted, accepted_mime = accepted_image(data, mime, _IMAGE_TYPES)
    except ImageGenerationError as exc:
        raise VideoGenerationError(str(exc)) from None
    return f"data:{accepted_mime};base64,{base64.b64encode(accepted).decode('ascii')}"
