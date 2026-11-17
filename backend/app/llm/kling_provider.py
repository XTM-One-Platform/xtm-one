# SPDX-License-Identifier: Apache-2.0

"""Kling AI (Kuaishou): video generation, never a chat model.

``POST /v1/videos/text2video`` - ``image2video`` from a start image - creates
a task answered ``{"code": 0, "data": {"task_id": ...}}``; ``GET`` on the same
path plus the task id reads it until ``task_status`` is ``succeed`` (the signed
URL in ``task_result.videos[0].url``) or ``failed`` (``task_status_msg``).
Every answer carries a Kling ``code``: ``0`` is success, any other a refusal
whose ``message`` says why, sometimes with an HTTP 200. A task is read on the
path that created it, so the id handed to ``on_job`` names it:
``image2video:<task id>``.

Kling 3.0 Omni (``kling-3.0-omni``) is made through its own route:
``POST /omni-video/kling-3.0-omni`` with ``contents`` - the prompt, a
``first_frame``, and up to three ``refer_image`` pictures whose subjects the
shot keeps - answered ``{"data": {"id": ...}}`` and read through
``GET /tasks?task_ids=<id>`` (``status`` ``succeeded`` with the video in
``outputs``, or ``failed`` with its ``message``): ``omni:<task id>``.

Credential: the key of the Kling API platform (``api-key-kling-...``, sent as
a Bearer token), or ``api_key||base_url`` for another endpoint
(``https://api.klingai.com``). It serves video only (``only_capability``),
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
    ReferenceImageLimits,
    VideoAPIError,
    VideoGenerationError,
    VideoRequest,
    VideoRequestRefusedError,
    VideoResult,
    await_job,
    download_video,
    names_a_field,
    normalised,
    snap_duration,
    status_error,
)
from app.tools.base import ToolDefinition

_DEFAULT_BASE_URL = "https://api-singapore.klingai.com"


@dataclass(frozen=True)
class _KlingModel:
    #: Makes the clip's own sound (``sound: on``), Kling 2.6 and later.
    sound: bool
    #: Reference images it takes beside a first frame: only the Omni route has them.
    references: int = 0
    #: Made through the Omni route (``POST /omni-video/<model>``, read through ``GET /tasks``).
    omni: bool = False


#: The models Kling serves for text and a start image (the API refuses the retired ones),
#: and Kling 3.0 Omni, which also keeps the subjects of reference images (``refer_image``).
_MODELS: dict[str, _KlingModel] = {
    "kling-v3": _KlingModel(sound=True),
    "kling-v2-6": _KlingModel(sound=True),
    "kling-v2-5-turbo": _KlingModel(sound=False),
    "kling-3.0-omni": _KlingModel(sound=True, references=3, omni=True),
}
#: What the Omni route takes as a ``refer_image``, as Kling documents its image inputs: width / height
#: from 1:2.5 to 2.5:1, each side 300 px or more, PNG or JPEG. Outside it the task is refused before anything is
#: made ("Image aspect ratio is invalid").
_OMNI_REFERENCE_LIMITS = ReferenceImageLimits(
    min_ratio=1 / 2.5, max_ratio=2.5, min_side=300, max_side=None, formats=("image/png", "image/jpeg"), max_count=3
)
#: The job id prefix of a task made through the Omni route.
_OMNI = "omni"
#: The formats the Omni route makes when no first frame decides it.
_OMNI_RATIOS = ("16:9", "9:16", "1:1")
_OMNI_RELAXABLE_FIELDS = ("audio", "resolution")
#: The lengths every current model makes, in whole seconds.
_DURATIONS = tuple(range(3, 16))
#: The mode a resolution is made in.
_MODES = {"720p": "std", "1080p": "pro"}
#: The start-image types Kling takes, as base64 with no ``data:`` prefix.
_IMAGE_TYPES = ("image/jpeg", "image/png")
_MAX_PROMPT_CHARS = 2500
#: Refusals about the account (arrears, an exhausted resource pack, no access), whatever
#: HTTP status they come with: asking again changes nothing.
_ACCOUNT_CODES = frozenset({1100, 1101, 1102, 1103})
#: The request fields the attempts relax, as a refusal names them.
_RELAXABLE_FIELDS = ("sound", "mode")


class KlingProvider(BaseLLMProvider):
    """Video provider for the Kling AI API."""

    provider_name = "kling"
    only_capability = "video"
    default_video_model = "kling-v3"
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
        raise NotImplementedError("Kling AI serves video generation only.")

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        del text, model
        raise NotImplementedError("Kling AI serves video generation only.")

    async def list_models(self) -> list[str]:
        """No chat model: the video models are :meth:`list_video_models`."""
        return []

    async def _call(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
        """One call to the Kling API, raising :class:`VideoAPIError` on any refusal."""
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, headers={"Authorization": f"Bearer {self._key}"}, timeout=30
            ) as http:
                response = await http.request(method, path, json=body, params=params)
        except httpx.HTTPError as exc:
            raise VideoAPIError(None, f"Kling AI could not be reached ({type(exc).__name__})") from None
        try:
            data = response.json()
        except ValueError:
            data = None
        answer = data if isinstance(data, dict) else {}
        code = answer.get("code")
        if response.status_code < 400 and code in (0, None):
            return answer
        message = str(answer.get("message") or f"HTTP {response.status_code}")[:400]
        error = VideoAPIError.of(response, f"{message} (Kling code {code})" if code is not None else message)
        if response.status_code < 400:
            error.code = 400
        if code in _ACCOUNT_CODES:
            error.code = 402
        raise error

    def max_reference_images(self, model: str) -> int:
        spec = _MODELS.get(model)
        return spec.references if spec is not None else 0

    def reference_image_limits(self, model: str) -> ReferenceImageLimits | None:
        spec = _MODELS.get(model)
        return _OMNI_REFERENCE_LIMITS if spec is not None and spec.references else None

    def takes_negative_prompt(self, model: str) -> bool:
        """Every Kling route reads one: a field of the text and image routes, the prompt's ``Avoid:`` on Omni."""
        return True

    def holds_locked_camera(self, model: str, *, start_frame: bool) -> bool | None:
        """A Kling model keeps a camera asked locked off still, from a prompt alone too (measured at 0.02 of motion)."""
        del start_frame
        return True if model in _MODELS else None

    async def list_video_models(self) -> list[str]:
        """The current models, once the key reads the account's tasks."""
        try:
            await self._call("GET", "/v1/videos/text2video", params={"pageNum": 1, "pageSize": 1})
        except VideoAPIError as exc:
            if exc.code == 401:
                raise VideoGenerationError("Kling AI refused the API key.") from None
            raise VideoGenerationError(f"Kling AI refused the request (HTTP {exc.code or 'error'}): {exc.message}") from None
        return list(_MODELS)

    def _task(self, endpoint: str, task_id: str) -> Callable[[], Awaitable[JobState]]:
        async def read() -> JobState:
            data = await self._call("GET", f"/v1/videos/{endpoint}/{quote(task_id, safe='')}")
            task = data.get("data") if isinstance(data.get("data"), dict) else {}
            status = task.get("task_status")
            if status == "succeed":
                videos = (task.get("task_result") or {}).get("videos") or []
                url = next((v.get("url") for v in videos if isinstance(v, dict) and v.get("url")), None)
                return JobState(True, url=url, error="Kling AI returned no video")
            if status == "failed":
                return JobState(True, error=str(task.get("task_status_msg") or "Kling AI gave no reason"))
            return JobState(False, state="queued" if status == "submitted" else str(status or "processing"))

        return read

    def _omni_task(self, task_id: str) -> Callable[[], Awaitable[JobState]]:
        """A task of the Omni route, read through ``GET /tasks`` (its status, its outputs)."""

        async def read() -> JobState:
            data = await self._call("GET", "/tasks", params={"task_ids": task_id})
            tasks = data.get("data") if isinstance(data.get("data"), list) else []
            task = next((t for t in tasks if isinstance(t, dict) and str(t.get("id")) == task_id), {})
            status = task.get("status")
            if status == "succeeded":
                outputs = task.get("outputs") or []
                url = next(
                    (o.get("url") for o in outputs if isinstance(o, dict) and o.get("type") == "video" and o.get("url")),
                    None,
                )
                return JobState(True, url=url, error="Kling AI returned no video")
            if status == "failed":
                return JobState(True, error=str(task.get("message") or "Kling AI gave no reason"))
            return JobState(False, state="queued" if status in ("submitted", None) else str(status))

        return read

    def _reader(self, job: str) -> Callable[[], Awaitable[JobState]]:
        endpoint, _sep, task_id = job.rpartition(":")
        if endpoint == _OMNI:
            return self._omni_task(task_id)
        return self._task(endpoint or "text2video", task_id)

    async def generate_video(
        self, request: VideoRequest, *, progress=None, on_job=None, resume_job=None, on_state=None
    ) -> VideoResult:
        """A Kling video: the task created, polled until it ends, its video downloaded."""
        notes: list[str] = []
        request = normalised(request, notes)
        if resume_job:
            url = await await_job(self._reader(resume_job), request.model, progress, resumed=True, on_state=on_state)
            return VideoResult(await download_video(url, request.model), str(resume_job), notes)
        spec = _MODELS.get(request.model)
        if spec is not None and spec.omni:
            task_id = await self._create_omni(request, notes)
            job = f"{_OMNI}:{task_id}"
            if on_job is not None:
                await on_job(job)
            url = await await_job(self._omni_task(task_id), request.model, progress, on_state=on_state)
            return VideoResult(await download_video(url, request.model), job, notes)
        if request.reference_images:
            raise VideoGenerationError(f"{request.model} takes no reference images: Kling 3.0 Omni does.")
        endpoint, body = self._video_body(request, notes)
        task_id = await self._create(endpoint, body, request.model, notes)
        job = f"{endpoint}:{task_id}"
        # The task exists upstream from here: a later failure resumes it, never pays twice.
        if on_job is not None:
            await on_job(job)
        url = await await_job(self._task(endpoint, task_id), request.model, progress, on_state=on_state)
        return VideoResult(await download_video(url, request.model), job, notes)

    @staticmethod
    def _video_body(request: VideoRequest, notes: list[str]) -> tuple[str, dict]:
        """The endpoint and body: the length snapped, the mode of the resolution, sound where the model makes it."""
        prompt = request.prompt
        if len(prompt) > _MAX_PROMPT_CHARS:
            prompt = prompt[:_MAX_PROMPT_CHARS]
            notes.append(f"The prompt was cut to the {_MAX_PROMPT_CHARS} characters Kling AI reads.")
        body: dict = {
            "model_name": request.model,
            "prompt": prompt,
            "duration": str(snap_duration(request.duration_seconds, _DURATIONS, notes)),
            "mode": _MODES.get(request.resolution, "pro"),
        }
        if request.negative_prompt:
            body["negative_prompt"] = request.negative_prompt[:_MAX_PROMPT_CHARS]
        spec = _MODELS.get(request.model)
        if (spec is None or spec.sound) and request.sound is not False:
            body["sound"] = "on"
        if not request.reference_image:
            body["aspect_ratio"] = request.aspect_ratio
            return "text2video", body
        try:
            data, _mime = accepted_image(request.reference_image, request.reference_mime, _IMAGE_TYPES)
        except ImageGenerationError as exc:
            raise VideoGenerationError(str(exc)) from None
        body["image"] = base64.b64encode(data).decode("ascii")
        notes.append("The start image decides the clip's format.")
        return "image2video", body

    @staticmethod
    def _image(data: bytes, mime: str | None) -> str:
        try:
            accepted, _mime = accepted_image(data, mime, _IMAGE_TYPES)
        except ImageGenerationError as exc:
            raise VideoGenerationError(str(exc)) from None
        return base64.b64encode(accepted).decode("ascii")

    def _omni_body(self, request: VideoRequest, notes: list[str]) -> dict:
        """The Omni route's body: the prompt, the first frame, the reference images, the settings."""
        prompt = request.prompt
        if len(prompt) > _MAX_PROMPT_CHARS:
            prompt = prompt[:_MAX_PROMPT_CHARS]
            notes.append(f"The prompt was cut to the {_MAX_PROMPT_CHARS} characters Kling AI reads.")
        if request.negative_prompt:
            prompt += f"\nAvoid: {request.negative_prompt[:500]}"
        contents: list[dict] = [{"type": "prompt", "text": prompt}]
        if request.reference_image:
            contents.append({"type": "first_frame", "url": self._image(request.reference_image, request.reference_mime)})
        for index, (data, mime) in enumerate(request.reference_images, start=1):
            contents.append({"type": "refer_image", "url": self._image(data, mime), "id": f"reference_{index}"})
        settings: dict = {
            "duration": snap_duration(request.duration_seconds, _DURATIONS, notes),
            "resolution": request.resolution if request.resolution in _MODES else "1080p",
            "audio": "off" if request.sound is False else "native",
            "multi_shot": False,
        }
        if request.reference_image:
            notes.append("The start image decides the clip's format.")
        else:
            settings["aspect_ratio"] = request.aspect_ratio if request.aspect_ratio in _OMNI_RATIOS else "16:9"
        return {"contents": contents, "settings": settings}

    async def _create_omni(self, request: VideoRequest, notes: list[str]) -> str:
        """The Omni task id, its audio then its resolution relaxed in turn while Kling refuses them."""
        body = self._omni_body(request, notes)
        settings = body["settings"]
        attempts = [settings, {**settings, "audio": "off"}, {**settings, "audio": "off", "resolution": "720p"}]
        refused: VideoAPIError | None = None
        for attempt in [dict(a) for a in dict.fromkeys(tuple(sorted(a.items())) for a in attempts)]:
            try:
                created = await self._call("POST", f"/omni-video/{request.model}", {**body, "settings": attempt})
            except VideoAPIError as exc:
                if exc.code == 400 and names_a_field(exc, _OMNI_RELAXABLE_FIELDS):
                    refused = exc
                    continue
                raise status_error(exc.code, exc, request.model) from None
            task = created.get("data") if isinstance(created.get("data"), dict) else {}
            task_id = str(task.get("id") or task.get("task_id") or "")
            if not task_id:
                raise VideoGenerationError("Kling AI started no task it can be asked about.")
            if attempt.get("audio") != settings.get("audio"):
                notes.append("The model refused to make sound with these settings: the clip is silent.")
            if attempt.get("resolution") != settings.get("resolution"):
                notes.append("The model refused 1080p with these settings: generated at 720p.")
            return task_id
        raise VideoRequestRefusedError(
            f"The video model refused the request: {refused.message if refused else 'no settings accepted'}",
            code=refused.code if refused else None,
        )

    async def _create(self, endpoint: str, body: dict, model: str, notes: list[str]) -> str:
        """The task id, its settings relaxed in turn while Kling refuses the sound or the mode."""
        attempts = [dict(body)]
        if "sound" in body:
            attempts.append({k: v for k, v in body.items() if k != "sound"})
        if body.get("mode") != "std":
            attempts.append({**{k: v for k, v in body.items() if k != "sound"}, "mode": "std"})
        refused: VideoAPIError | None = None
        for attempt in attempts:
            try:
                created = await self._call("POST", f"/v1/videos/{endpoint}", attempt)
            except VideoAPIError as exc:
                if exc.code == 400 and names_a_field(exc, _RELAXABLE_FIELDS):
                    refused = exc
                    continue
                raise status_error(exc.code, exc, model) from None
            task = created.get("data") if isinstance(created.get("data"), dict) else {}
            task_id = str(task.get("task_id") or "")
            if not task_id:
                raise VideoGenerationError("Kling AI started no task it can be asked about.")
            if "sound" in body and "sound" not in attempt:
                notes.append("The model refused to make sound with these settings: the clip is silent.")
            if attempt.get("mode") != body.get("mode"):
                notes.append("The model refused 1080p with these settings: generated at 720p.")
            return task_id
        raise VideoRequestRefusedError(
            f"The video model refused the request: {refused.message if refused else 'no settings accepted'}",
            code=refused.code if refused else None,
        )
