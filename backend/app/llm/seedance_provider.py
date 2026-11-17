# SPDX-License-Identifier: Apache-2.0

"""Seedance (ByteDance) on BytePlus ModelArk: video generation, never a chat model.

``POST /contents/generations/tasks`` creates a task (``{"id": "cgt-..."}``)
from a ``content`` list - the prompt, and a start image as a ``first_frame``
``image_url`` holding a ``data:`` URL - and ``GET
/contents/generations/tasks/{id}`` reads it until ``status`` is ``succeeded``
(the signed ``content.video_url``) or ``failed`` / ``expired`` / ``cancelled``
(``error.message``). Errors are OpenAI-shaped: ``{"error": {"code", "message"}}``.

Credential: a ModelArk API key (Bearer), or ``api_key||base_url`` for another
endpoint - Volcengine Ark serves the same API in China
(``https://ark.cn-beijing.volces.com/api/v3``). Each model is activated in the
ModelArk console before it answers. It serves video only (``only_capability``),
validated by listing the account's tasks, which costs nothing.
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

_DEFAULT_BASE_URL = "https://ark.ap-southeast.bytepluses.com/api/v3"


@dataclass(frozen=True)
class _SeedanceModel:
    durations: tuple[int, ...]
    resolutions: tuple[str, ...]
    #: Makes the clip's own sound (``generate_audio``).
    audio: bool


#: The Seedance models ModelArk serves and what each makes.
_MODELS: dict[str, _SeedanceModel] = {
    "dreamina-seedance-2-0-260128": _SeedanceModel(tuple(range(4, 16)), ("720p", "1080p"), audio=True),
    "dreamina-seedance-2-0-fast-260128": _SeedanceModel(tuple(range(4, 16)), ("720p",), audio=True),
    "dreamina-seedance-2-0-mini-260615": _SeedanceModel(tuple(range(4, 16)), ("720p",), audio=True),
    "dreamina-seedance-2-5-260628": _SeedanceModel(tuple(range(4, 31)), ("720p", "1080p"), audio=True),
    "seedance-1-5-pro-251215": _SeedanceModel(tuple(range(4, 13)), ("720p", "1080p"), audio=True),
    "seedance-1-0-pro-250528": _SeedanceModel(tuple(range(2, 13)), ("720p", "1080p"), audio=False),
    "seedance-1-0-pro-fast-251015": _SeedanceModel(tuple(range(2, 13)), ("720p", "1080p"), audio=False),
}
#: The start-image types every model takes.
_IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/bmp", "image/tiff", "image/gif")
#: The task states that end it without a video.
_ENDED = frozenset({"failed", "expired", "cancelled"})


class SeedanceProvider(BaseLLMProvider):
    """Video provider for Seedance on BytePlus ModelArk (or Volcengine Ark)."""

    provider_name = "seedance"
    only_capability = "video"
    default_video_model = "dreamina-seedance-2-0-260128"
    base_url = _DEFAULT_BASE_URL

    def __init__(self, api_key: str, **kwargs):
        del kwargs
        parts = api_key.split("||")
        self._key = parts[0].strip() if parts else ""
        if len(parts) >= 2 and parts[1].strip():
            self.base_url = parts[1].strip().rstrip("/")

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
        raise NotImplementedError("Seedance serves video generation only.")

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        del text, model
        raise NotImplementedError("Seedance serves video generation only.")

    async def list_models(self) -> list[str]:
        """No chat model: the video models are :meth:`list_video_models`."""
        return []

    async def _call(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
        """One call to the ModelArk API, raising :class:`VideoAPIError` on a refusal."""
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, headers={"Authorization": f"Bearer {self._key}"}, timeout=30
            ) as http:
                response = await http.request(method, path, json=body, params=params)
        except httpx.HTTPError as exc:
            raise VideoAPIError(None, f"ModelArk could not be reached ({type(exc).__name__})") from None
        try:
            data = response.json()
        except ValueError:
            data = None
        answer = data if isinstance(data, dict) else {}
        if response.status_code < 400:
            return answer
        error = answer.get("error") if isinstance(answer.get("error"), dict) else {}
        reason = str(error.get("message") or "")[:400]
        if error.get("code"):
            reason = f"{reason} ({error['code']})" if reason else str(error["code"])
        raise VideoAPIError.of(response, reason)

    async def list_video_models(self) -> list[str]:
        """The Seedance models, once the key reads the account's tasks."""
        try:
            await self._call("GET", "/contents/generations/tasks", params={"page_num": 1, "page_size": 1})
        except VideoAPIError as exc:
            if exc.code == 401:
                raise VideoGenerationError("ModelArk refused the API key.") from None
            raise VideoGenerationError(f"ModelArk refused the request (HTTP {exc.code or 'error'}): {exc.message}") from None
        return list(_MODELS)

    def _task(self, task_id: str) -> Callable[[], Awaitable[JobState]]:
        async def read() -> JobState:
            task = await self._call("GET", f"/contents/generations/tasks/{quote(task_id, safe='')}")
            status = task.get("status")
            if status == "succeeded":
                content = task.get("content") if isinstance(task.get("content"), dict) else {}
                return JobState(True, url=content.get("video_url") or None, error="ModelArk returned no video")
            if status in _ENDED:
                error = task.get("error") if isinstance(task.get("error"), dict) else {}
                return JobState(True, error=str(error.get("message") or f"the task {status}"))
            return JobState(False, state=str(status or "running"))

        return read

    async def generate_video(
        self, request: VideoRequest, *, progress=None, on_job=None, resume_job=None, on_state=None
    ) -> VideoResult:
        """A Seedance video: the task created, polled until it ends, its video downloaded."""
        notes: list[str] = []
        request = normalised(request, notes)
        if resume_job:
            url = await await_job(self._task(resume_job), request.model, progress, resumed=True, on_state=on_state)
            return VideoResult(await download_video(url, request.model), str(resume_job), notes)
        body = self._video_body(request, notes)
        try:
            created = await self._call("POST", "/contents/generations/tasks", body)
        except VideoAPIError as exc:
            raise status_error(exc.code, exc, request.model) from None
        task_id = str(created.get("id") or "")
        if not task_id:
            raise VideoGenerationError("ModelArk started no task it can be asked about.")
        # The task exists upstream from here: a later failure resumes it, never pays twice.
        if on_job is not None:
            await on_job(task_id)
        url = await await_job(self._task(task_id), request.model, progress, on_state=on_state)
        return VideoResult(await download_video(url, request.model), task_id, notes)

    @staticmethod
    def _video_body(request: VideoRequest, notes: list[str]) -> dict:
        """The task: the length and resolution the model makes, its sound, the start image as first frame."""
        content: list[dict] = [{"type": "text", "text": request.prompt}]
        body: dict = {"model": request.model, "content": content, "ratio": request.aspect_ratio, "watermark": False}
        spec = _MODELS.get(request.model)
        if spec is None:
            body.update(duration=request.duration_seconds, resolution=request.resolution)
        else:
            body["duration"] = snap_duration(request.duration_seconds, spec.durations, notes)
            body["resolution"] = snap_resolution(request.resolution, spec.resolutions, notes)
            if spec.audio:
                body["generate_audio"] = request.sound is not False
        if request.negative_prompt:
            notes.append("Seedance takes no negative prompt: what to avoid belongs in the prompt.")
        if request.reference_image:
            try:
                data, mime = accepted_image(request.reference_image, request.reference_mime, _IMAGE_TYPES)
            except ImageGenerationError as exc:
                raise VideoGenerationError(str(exc)) from None
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"},
                    "role": "first_frame",
                }
            )
            # A first-frame task keeps the image's own format (Seedance 2.5 takes nothing else).
            body["ratio"] = "adaptive"
            notes.append("The start image decides the clip's format.")
        return body
