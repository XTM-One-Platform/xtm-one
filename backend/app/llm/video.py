# SPDX-License-Identifier: Apache-2.0

"""Video generation through the LLM providers.

The contract every provider answers (``BaseLLMProvider.list_video_models`` /
``generate_video``), the request and result types, and the one implementation
of the OpenAI Videos API (``POST /videos``, ``GET /videos/{id}``,
``GET /videos/{id}/content``) shared by every OpenAI-shaped provider:
OpenAI itself, Azure OpenAI's v1 endpoint, and the OpenAI-compatible
gateways (LiteLLM, a custom OpenAI-compatible endpoint, the Filigran
gateway) that serve other vendors' video models behind the same API.
Gemini implements Veo in its own provider. The providers with a job API of
their own (ElevenLabs, Kling AI, Runway, Seedance on ModelArk) create the job,
then share :func:`await_job` (the polling, ridden-out failures, a resumed job)
and :func:`download_video` (the signed URL, :func:`fetch_signed_media`); one that
answers with the video itself (Gemini Omni's interactions) polls through
:func:`await_finished_job`.

Which models a provider offers for video is read from the provider (its
model list, typed where the provider types it) and classified with
``router.is_video_model``, catalog first; the platform's choice of provider
and model is ``PlatformSettings.video_*`` (Settings > AI and models > Media > Video
generation), resolved by ``router.get_video_provider``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from app.core.utils import format_exception_message
from app.llm.provider_errors import api_error_message, error_body, is_transient, quota_exceeded, retry_delay

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int], Awaitable[None]]
#: Told the upstream job id as soon as the provider created the job.
JobCallback = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class VideoJobState:
    """What the provider says of a job that exists upstream, each time it is asked."""

    #: The provider's own word for it - ``queued`` / ``in_progress`` /
    #: ``completed`` on the Videos API, ``running`` / ``done`` for a Veo
    #: operation - or :func:`busy_state` while a failed poll is ridden out.
    state: str
    #: The percent the provider itself reported; ``None`` when it reports none.
    progress: int | None = None


#: Told the provider's state of the upstream job at every poll.
StateCallback = Callable[[VideoJobState], Awaitable[None]]
#: The states of a job whose provider cannot answer now (:func:`busy_state`).
STATE_RATE_LIMITED = "rate_limited"
STATE_UNAVAILABLE = "unavailable"


def busy_state(code: int | None) -> str:
    """The state of a job whose provider cannot answer now: rate limited (HTTP 429) or unavailable."""
    return STATE_RATE_LIMITED if code == 429 else STATE_UNAVAILABLE


#: How often a running job is asked for its state.
POLL_SECONDS = 8.0
#: How long a job may run before it is given up on.
MAX_WAIT_SECONDS = 30 * 60
#: While a job that exists upstream is polled or its video downloaded, the
#: provider's trouble is ridden out for this long (and at least this many
#: attempts): asking again never resubmits a paid generation, giving up does.
#: A job may run 30 minutes; ten minutes of outage is tolerated.
TRANSIENT_BUDGET_SECONDS = 10 * 60
MIN_TRANSIENT_ATTEMPTS = 3
#: The longest a back-off (or a provider's ``Retry-After``) waits between two attempts.
MAX_BACKOFF_SECONDS = 60.0
DOWNLOAD_ATTEMPTS = 3
#: How long a model's lifecycle read from its provider is trusted.
LIFECYCLE_TTL_SECONDS = 3600.0
_lifecycle: dict[tuple[str, str], tuple[float, str | None]] = {}


def retired_since(record: Any) -> str | None:
    """The date a provider's model record says it was shut down, when that date has passed.

    OpenAI-shaped model records carry ``shutdown_date`` (``YYYY-MM-DD``) once a
    model is scheduled for retirement; the API stops serving it that day.
    """
    raw = getattr(record, "shutdown_date", None)
    if raw is None:
        extra = getattr(record, "model_extra", None)
        raw = extra.get("shutdown_date") if isinstance(extra, dict) else None
    if raw is None and isinstance(record, dict):
        raw = record.get("shutdown_date")
    if not raw:
        return None
    try:
        day = date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None
    return day.isoformat() if day <= datetime.now(UTC).date() else None

ASPECT_RATIOS: tuple[str, ...] = ("16:9", "9:16")
RESOLUTIONS: tuple[str, ...] = ("720p", "1080p")


class VideoGenerationError(RuntimeError):
    """A video the provider could not make; ``str()`` is safe to show."""


class VideoModelNotServedError(VideoGenerationError):
    """The provider answered HTTP 404 to a generation asked of the model: it does not serve it there.

    ``detail``: the provider's own words. The worker records the model as not served on that provider
    (``video_served.note``), so the next listing drops it and a shot naming it is refused before it is paid.
    """

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


class VideoRequestRefusedError(VideoGenerationError):
    """The provider refused the request on its arguments (HTTP 4xx) before any job existed upstream.

    Nothing was made or paid: a shot refused this way is no try of its prompt and start frame
    (``clips.uncount_regeneration``). ``code``: the HTTP status.
    """

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class VideoProviderBusyError(RuntimeError):
    """The provider could not take the request now (rate limit, outage, network): asked again later.

    ``status_code``: the HTTP status it answered, ``None`` when it did not
    answer at all - what :func:`busy_state` names. ``retry_after``: the
    seconds it asked to wait, when it said. ``quota``: its error says
    a quota ran out (``provider_errors.quota_exceeded``) - with a 429, what
    makes the provider cool for every shot asked of it (``provider_cooling``).
    ``remaining`` / ``needed``: the credits the account has left and
    the request would cost, when the provider's refusal says them.
    ``started``: a job was created upstream and the provider ended it for its
    own state (an operation failed for its load): nothing to resume, the
    next attempt asks anew.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
        quota: bool = False,
        remaining: int | None = None,
        needed: int | None = None,
        started: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.quota = quota
        self.remaining = remaining
        self.needed = needed
        self.started = started


@dataclass(frozen=True)
class VideoCredits:
    """What a provider says of an account's credits for one request, before it is asked
    (``BaseLLMProvider.video_credits``): those left and what the request costs, ``None`` where it does not say."""

    remaining: int | None
    needed: int | None = None

    def short(self) -> bool:
        """Whether the request costs more than the account has left: both known, and the cost past what is left."""
        return self.remaining is not None and self.needed is not None and self.needed > self.remaining

    def words(self) -> str:
        """``quota: 1000 remaining, 7272 needed``."""
        needed = f", {self.needed} needed" if self.needed is not None else ""
        return f"quota: {self.remaining if self.remaining is not None else 'unknown'} remaining{needed}"


class VideoAPIError(Exception):
    """A failed call to a provider's own video API (one with no SDK): the HTTP status
    (``None``: no answer at all), the provider's reason, the wait it asked for, and the
    JSON it answered with (``payload``, what its own adapter reads a refusal's structure off)."""

    def __init__(self, code: int | None, message: str, *, retry_after: float | None = None, payload: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after
        self.payload = payload

    @classmethod
    def of(cls, response: Any, reason: str) -> VideoAPIError:
        """The error of an HTTP *response*, with the provider's *reason*, its ``Retry-After`` and its JSON body."""
        raw = response.headers.get("retry-after")
        try:
            wait = float(raw) if raw is not None else None
        except ValueError:
            wait = None
        try:
            payload = response.json()
        except (ValueError, AttributeError, TypeError):
            payload = None
        return cls(response.status_code, reason or f"HTTP {response.status_code}", retry_after=wait, payload=payload)


def names_a_field(exc: BaseException, fields: tuple[str, ...]) -> bool:
    """Whether a refusal is about one of the request *fields* a fallback can relax.

    The structured ``param`` an OpenAI-shaped error carries decides when there
    is one; otherwise the refusal's own text names the field as a word (a
    gateway relays its upstream's message; Google's errors carry no ``param``).
    A refusal naming none of them - a blocked prompt, an unknown model - is
    the answer, and no other shape of the request changes it.
    """
    body = error_body(exc)
    error = body.get("error", body) if isinstance(body, dict) else None
    param = error.get("param") if isinstance(error, dict) else None
    if isinstance(param, str) and param:
        return any(name in param.lower() for name in fields)
    text = api_error_message(exc).lower()
    return any(re.search(rf"\b{re.escape(name)}", text) for name in fields)


class TransientFailures:
    """The provider's trouble while a job that exists upstream is polled or downloaded.

    Ridden out for *budget* seconds of failures in a row (and at least
    *attempts* attempts), each followed by a growing back-off - or the wait
    the provider asked for. A good answer starts the count again.
    """

    def __init__(self, model: str, *, budget: float | None = None, attempts: int | None = None) -> None:
        self.model = model
        self.budget = TRANSIENT_BUDGET_SECONDS if budget is None else budget
        self.attempts = MIN_TRANSIENT_ATTEMPTS if attempts is None else attempts
        self.count = 0
        self.since: float | None = None
        self.wait = 0.0
        self.code: int | None = None

    @property
    def state(self) -> str:
        """The job's state while the last failure is ridden out (:func:`busy_state`)."""
        return busy_state(self.code)

    def failed(self, exc: BaseException, code: int | None) -> None:
        """Count a transient failure, or raise a readable one: a refusal, or trouble past the budget.

        Sets :attr:`wait`, the back-off to sleep before the next attempt.
        """
        if not is_transient(code):
            raise status_error(code, exc, self.model, about_job=True) from None
        self.code = code
        now = time.monotonic()
        self.since = now if self.since is None else self.since
        self.count += 1
        if self.count >= self.attempts and now - self.since >= self.budget:
            raise VideoGenerationError(
                f"The video provider kept failing while the video was made ({api_error_message(exc)})."
            ) from None
        asked = retry_delay(exc)
        backoff = POLL_SECONDS * min(2 ** (self.count - 1), 8)
        self.wait = min(MAX_BACKOFF_SECONDS, asked if asked is not None else backoff)

    def ok(self) -> None:
        self.count = 0
        self.since = None
        self.wait = 0.0


def status_error(code: int | None, exc: BaseException, model: str, *, about_job: bool = False) -> RuntimeError:
    """The error to raise for an HTTP failure of the video API: retried when transient, readable when not.

    A 404 to the request that creates a video is :class:`VideoModelNotServedError`; one met while a job
    that exists upstream is polled or downloaded (*about_job*) is about that job, not the model.
    """
    detail = api_error_message(exc)
    if is_transient(code):
        return VideoProviderBusyError(
            f"The video provider is unavailable (HTTP {code or 'error'}): {detail}",
            status_code=code,
            retry_after=retry_delay(exc),
            quota=quota_exceeded(exc),
        )
    if code in (401, 403):
        return VideoGenerationError(f"The video provider refused the request for {model} (HTTP {code}): {detail}")
    if code == 404:
        if detail.strip().lower() in ("", "error code: 404", type(exc).__name__.lower()):
            # No message at all: the route itself is gone, not a refusal of this request.
            detail = "the endpoint answered with no message - it no longer serves video generation"
        message = f"The video provider has no video generation for {model} at this endpoint (HTTP 404): {detail}"
        return VideoGenerationError(message) if about_job else VideoModelNotServedError(message, detail=detail)
    message = f"The video provider refused the request (HTTP {code}): {detail}"
    if about_job or code is None or not 400 <= code < 500:
        return VideoGenerationError(message)
    # Refused on its arguments before a job existed: nothing made, nothing paid.
    return VideoRequestRefusedError(message, code=code)


@dataclass(frozen=True)
class ReferenceImageLimits:
    """What a video model takes as a reference image or an ingredient, as its provider documents it.

    *min_ratio* and *max_ratio* bound width / height; *min_side* and *max_side*
    bound each side in pixels (``None``: no bound); *formats* are the MIME types
    it reads (an SVG is rasterised to PNG first, never sent as it is);
    *max_count* is how many it takes in one request.
    """

    min_ratio: float
    max_ratio: float
    min_side: int | None = None
    max_side: int | None = None
    formats: tuple[str, ...] = ("image/png", "image/jpeg")
    max_count: int = 0

    def ratio_words(self) -> str:
        """``1:2.5 to 2.5:1``: the accepted range of width / height, as a person reads it."""
        return f"{ratio_label(self.min_ratio)} to {ratio_label(self.max_ratio)}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "aspect_ratio": {"min": round(self.min_ratio, 4), "max": round(self.max_ratio, 4), "range": self.ratio_words()},
            "min_side_px": self.min_side,
            "max_side_px": self.max_side,
            "formats": list(self.formats),
            "svg": "rasterised to PNG before it is checked and sent",
            "max_count": self.max_count,
        }


def ratio_label(ratio: float) -> str:
    """A width / height ratio as ``6.0:1`` (wide) or ``1:2.5`` (tall)."""
    if ratio >= 1:
        return f"{round(ratio, 2):g}:1" if round(ratio, 2) != round(ratio) else f"{ratio:.1f}:1"
    tall = 1 / ratio
    return f"1:{round(tall, 2):g}" if round(tall, 2) != round(tall) else f"1:{tall:.1f}"


@dataclass
class VideoRequest:
    model: str
    prompt: str
    aspect_ratio: str = "16:9"
    duration_seconds: int = 8
    resolution: str = "1080p"
    negative_prompt: str | None = None
    reference_image: bytes | None = None
    reference_mime: str | None = None
    #: ``False`` asks a model that makes its own sound for a silent shot; ``None`` is the model's own choice.
    sound: bool | None = None
    #: ``(bytes, mime)`` of the pictures the shot must keep - a character, a garment, a product, a logo -
    #: beside the prompt; only a model whose provider declares it (``max_reference_images``).
    reference_images: list[tuple[bytes, str]] = field(default_factory=list)
    #: How hard the model holds to the prompt; only set for a model whose provider declares it
    #: (``video_guidance``), ``None`` its own default.
    guidance: float | None = None


@dataclass
class VideoResult:
    data: bytes
    job_id: str
    #: What was made differently from the request (a snapped duration, a size).
    notes: list[str] = field(default_factory=list)


async def _no_progress(_: int) -> None:
    return None


async def _no_state(_: VideoJobState) -> None:
    return None


#: The largest video a signed download may bring back.
MAX_VIDEO_BYTES = 512 * 1024 * 1024
#: How long a signed download may wait on one read.
_DOWNLOAD_TIMEOUT_SECONDS = 120


@dataclass
class JobState:
    """What a provider says of a job that exists upstream."""

    #: The job ended, made or failed.
    finished: bool
    #: Where the made video is (a signed URL), once made.
    url: str | None = None
    #: The made video itself, once made, for a provider that answers with it.
    data: bytes | None = None
    #: Why it was not made, once failed.
    error: str | None = None
    #: The provider's own 0-100 progress, when it reports one.
    progress: int | None = None
    #: While it runs, the provider's state as :class:`VideoJobState` names it:
    #: ``queued`` while it waits upstream, else the provider's own word.
    state: str = "running"


async def await_job(
    read: Callable[[], Awaitable[JobState]],
    model: str,
    progress: ProgressCallback | None = None,
    *,
    resumed: bool = False,
    on_state: StateCallback | None = None,
) -> str:
    """:func:`await_finished_job` for a provider that answers with where its video is: that URL."""
    state = await await_finished_job(read, model, progress, resumed=resumed, on_state=on_state)
    if not state.url:
        raise VideoGenerationError("The video provider answered with no video to download.")
    return state.url


async def await_finished_job(
    read: Callable[[], Awaitable[JobState]],
    model: str,
    progress: ProgressCallback | None = None,
    *,
    resumed: bool = False,
    on_state: StateCallback | None = None,
) -> JobState:
    """Poll a job that exists upstream until it ends: its last state, with its video's URL or the video itself.

    *read* asks the provider once, raising :class:`VideoAPIError`. A transient
    failure is asked again after a back-off within the transient budget, never
    turned into a retry that would make a second video. A job resumed from an
    earlier run is asked at once, and one the provider no longer knows says so.
    *on_state* hears the job's state at every answer, and the failure being ridden out.
    """
    report = progress or _no_progress
    tell = on_state or _no_state
    started = time.monotonic()
    failures = TransientFailures(model)
    asked = False
    if not resumed:
        await tell(VideoJobState("queued"))
    while True:
        if time.monotonic() - started > MAX_WAIT_SECONDS:
            raise VideoGenerationError("The video model did not finish within 30 minutes.")
        if asked or not resumed:
            await asyncio.sleep(POLL_SECONDS + failures.wait)
        try:
            state = await read()
        except VideoAPIError as exc:
            if resumed and not asked and exc.code == 404:
                raise VideoGenerationError(
                    "The provider no longer knows the generation this shot started: generate it again."
                ) from None
            asked = True
            failures.failed(exc, exc.code)
            await tell(VideoJobState(failures.state))
            continue
        asked = True
        failures.ok()
        if state.finished:
            if state.url or state.data:
                await tell(VideoJobState("completed", 100))
                return state
            raise VideoGenerationError(f"The video was not made: {state.error or 'the provider gave no reason'}.")
        elapsed = int(min(90, (time.monotonic() - started) / 3))
        await report(state.progress if state.progress is not None else elapsed)
        await tell(VideoJobState(state.state, state.progress))


async def fetch_signed_media(url: str, *, max_bytes: int) -> bytes:
    """What a provider made, from the signed URL it answered with, or :class:`VideoAPIError`.

    The URL comes from the provider's answer, so it is fetched like any URL the
    platform did not choose (``web_fetch.fetch_url_bytes``: the outbound policy on
    every redirect, the connection pinned to the checked address, the size cap),
    and without the provider's key: a signed URL carries its own.
    """
    import httpx

    from app.services.web_fetch import fetch_url_bytes

    try:
        fetched = await fetch_url_bytes(url, max_bytes=max_bytes, timeout=_DOWNLOAD_TIMEOUT_SECONDS)
    except httpx.HTTPStatusError as exc:
        raise VideoAPIError(exc.response.status_code, f"the media URL answered HTTP {exc.response.status_code}") from None
    except httpx.HTTPError as exc:
        raise VideoAPIError(None, f"the media host could not be reached ({type(exc).__name__})") from None
    except ValueError as exc:
        # Refused by the outbound policy, too many redirects, or past the size cap: final.
        raise VideoAPIError(400, str(exc)) from None
    return fetched.content


async def download_video(url: str, model: str) -> bytes:
    """The made video at the signed URL its provider answered with (:func:`fetch_signed_media`).

    Asked again after a back-off on a transient failure: the video is never made again.
    """
    failures = TransientFailures(model, budget=0, attempts=DOWNLOAD_ATTEMPTS)
    while True:
        try:
            data = await fetch_signed_media(url, max_bytes=MAX_VIDEO_BYTES)
        except VideoAPIError as exc:
            if not is_transient(exc.code):
                raise VideoGenerationError(f"The made video could not be downloaded: {exc.message}.") from None
            failures.failed(exc, exc.code)
            await asyncio.sleep(failures.wait)
            continue
        if not data:
            raise VideoGenerationError("The video provider returned an empty video.")
        return data


def snap_duration(seconds: int, allowed: tuple[int, ...], notes: list[str]) -> int:
    """The nearest duration the model makes, with a note when it differs."""
    snapped = min(allowed, key=lambda d: abs(d - seconds))
    if snapped != seconds:
        notes.append(f"Duration snapped to {snapped} s (the model makes {', '.join(map(str, allowed))} s).")
    return snapped


def snap_resolution(resolution: str, offered: tuple[str, ...], notes: list[str]) -> str:
    """The resolution asked when the model makes it, else the nearest it makes, with a note."""
    if resolution in offered:
        return resolution

    def lines(text: str) -> int:
        digits = "".join(ch for ch in text if ch.isdigit())
        return int(digits) if digits else 0

    snapped = min(offered, key=lambda text: abs(lines(text) - lines(resolution)))
    notes.append(f"The model does not make {resolution}: generated at {snapped}.")
    return snapped


def normalised(request: VideoRequest, notes: list[str]) -> VideoRequest:
    """*request* with an aspect ratio and a resolution every video API takes."""
    if request.aspect_ratio not in ASPECT_RATIOS:
        notes.append(f"Aspect ratio {request.aspect_ratio} is not offered by video models: generated at 16:9.")
        request.aspect_ratio = "16:9"
    if request.resolution not in RESOLUTIONS:
        request.resolution = "1080p"
    return request


class OpenAIVideosMixin:
    """The OpenAI Videos API on ``self.client`` (an ``AsyncOpenAI``-shaped client).

    A provider whose video routes live elsewhere overrides
    :meth:`_videos_client`; one that must pin the upstream per call overrides
    :meth:`_video_headers`; one that types its models overrides
    :meth:`list_video_models`.
    """

    client: Any
    #: The durations the Videos API accepts (its ``seconds`` parameter).
    video_durations: tuple[int, ...] = (4, 8, 12)

    def _videos_client(self) -> Any:
        return self.client

    async def _video_headers(self, model: str) -> dict[str, str]:
        return {}

    async def list_video_models(self) -> list[str]:
        """The video models this endpoint lists, without the ones their provider has retired."""
        from app.llm.router import is_video_model

        try:
            response = await self._videos_client().models.list()
        except Exception as exc:
            logger.info("Video model listing failed: %s", format_exception_message(exc))
            return []
        return sorted({m.id for m in response.data if is_video_model(m.id) and retired_since(m) is None})

    async def video_model_unavailable(self, model: str) -> str | None:
        """Why *model* no longer makes videos here, read from its model record (kept an hour)."""
        client = self._videos_client()
        key = (str(getattr(client, "base_url", "")), model)
        cached = _lifecycle.get(key)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
        try:
            record = await client.models.retrieve(model, extra_headers=await self._video_headers(model) or None)
        except Exception as exc:
            # An endpoint that does not publish model records (a gateway) is
            # not known to have retired anything: the shot is asked for.
            logger.debug("Video model %s lifecycle not read: %s", model, format_exception_message(exc))
            return None
        day = retired_since(record)
        reason = f"{model} was retired by its provider on {day}" if day else None
        _lifecycle[key] = (time.monotonic() + LIFECYCLE_TTL_SECONDS, reason)
        return reason

    @staticmethod
    def _sizes(request: VideoRequest) -> list[str]:
        """The sizes to ask for, best first: the resolution asked (both 1080p shapes models name), then 720p."""
        portrait = request.aspect_ratio == "9:16"
        hd = ["1024x1792", "1080x1920"] if portrait else ["1792x1024", "1920x1080"]
        sd = "720x1280" if portrait else "1280x720"
        return [*hd, sd] if request.resolution == "1080p" else [sd]

    #: The request fields the attempts relax, as a refusal names them.
    relaxable_fields: tuple[str, ...] = ("size", "seconds", "resolution", "duration", "dimension")

    @staticmethod
    def _attempts(sizes: list[str], seconds: int) -> list[tuple[str | None, str | None]]:
        """``(size, seconds)`` asked in turn while the API refuses one of them.

        Every video model behind this API takes its own subset of sizes and
        lengths, and an OpenAI-compatible gateway may take neither field: the
        sizes best first, then the model's own size, then its own length too.
        A refusal about anything else (a blocked prompt) ends the attempts.
        """
        return [(size, str(seconds)) for size in sizes] + [(None, str(seconds)), (None, None)]

    async def _download(self, client: Any, job_id: str, headers: dict[str, str], model: str) -> bytes:
        """The made video, asked again (after a back-off) on a transient failure: it is never made again."""
        import httpx
        import openai

        failures = TransientFailures(model, budget=0, attempts=DOWNLOAD_ATTEMPTS)
        while True:
            try:
                content = await client.videos.download_content(job_id, variant="video", extra_headers=headers or None)
                # The body is read here too: a stream cut mid-body is a transient failure, not a crash.
                data = content.read() if hasattr(content, "read") else content.content
                if asyncio.iscoroutine(data):
                    data = await data
            except openai.APIStatusError as exc:
                failures.failed(exc, exc.status_code)
            except (openai.APIConnectionError, openai.APITimeoutError, httpx.TransportError) as exc:
                failures.failed(exc, None)
            else:
                if not data:
                    raise VideoGenerationError("The video provider returned an empty video.")
                return bytes(data)
            await asyncio.sleep(failures.wait)

    @staticmethod
    def _job_state(job: Any) -> VideoJobState:
        """The upstream job's state as the Videos API reports it: its status and its own ``progress``."""
        reported = getattr(job, "progress", None)
        return VideoJobState(str(job.status), int(reported) if isinstance(reported, int | float) else None)

    async def _await_job(
        self,
        client: Any,
        job: Any,
        headers: dict[str, str],
        model: str,
        report: ProgressCallback,
        tell: StateCallback = _no_state,
    ) -> bytes:
        """Poll the job that exists upstream until it ends, then download its video.

        A transient failure is asked again after a back-off, within the
        transient budget: never turned into a retry that would make a second video.
        *tell* hears the job's state at every answer, and the failure being ridden out.
        """
        import httpx
        import openai

        job_id = job.id
        started = time.monotonic()
        failures = TransientFailures(model)
        await tell(self._job_state(job))
        while job.status in ("queued", "in_progress"):
            if time.monotonic() - started > MAX_WAIT_SECONDS:
                raise VideoGenerationError("The video model did not finish within 30 minutes.")
            await asyncio.sleep(POLL_SECONDS + failures.wait)
            try:
                job = await client.videos.retrieve(job_id, extra_headers=headers or None)
            except openai.APIStatusError as exc:
                failures.failed(exc, exc.status_code)
                await tell(VideoJobState(failures.state))
                continue
            except (openai.APIConnectionError, openai.APITimeoutError, httpx.TransportError) as exc:
                failures.failed(exc, None)
                await tell(VideoJobState(failures.state))
                continue
            failures.ok()
            reported = getattr(job, "progress", None)
            await report(int(reported) if reported else int(min(90, (time.monotonic() - started) / 3)))
            await tell(self._job_state(job))
        if job.status != "completed":
            detail = getattr(getattr(job, "error", None), "message", None) or job.status
            raise VideoGenerationError(f"The video was not made: {detail}")
        return await self._download(client, job_id, headers, model)

    async def generate_video(
        self,
        request: VideoRequest,
        *,
        progress: ProgressCallback | None = None,
        on_job: JobCallback | None = None,
        resume_job: str | None = None,
        on_state: StateCallback | None = None,
    ) -> VideoResult:
        import openai

        report = progress or _no_progress
        tell = on_state or _no_state
        notes: list[str] = []
        request = normalised(request, notes)
        client = self._videos_client()
        headers = await self._video_headers(request.model)
        if resume_job:
            # The job was made by an earlier run of the same shot: resumed, never made again.
            try:
                job = await client.videos.retrieve(resume_job, extra_headers=headers or None)
            except openai.NotFoundError:
                raise VideoGenerationError(
                    "The provider no longer knows the generation this shot started: generate it again."
                ) from None
            except openai.APIStatusError as exc:
                raise status_error(exc.status_code, exc, request.model, about_job=True) from None
            except (openai.APIConnectionError, openai.APITimeoutError) as exc:
                raise VideoProviderBusyError(f"The video provider could not be reached: {api_error_message(exc)}") from exc
            data = await self._await_job(client, job, headers, request.model, report, tell)
            return VideoResult(data=data, job_id=str(resume_job), notes=notes)
        seconds = snap_duration(request.duration_seconds, self.video_durations, notes)
        video = None
        sizes = self._sizes(request)
        refused: BaseException | None = None
        for size, length in self._attempts(sizes, seconds):
            kwargs: dict[str, Any] = {"model": request.model, "prompt": request.prompt}
            if length is not None:
                kwargs["seconds"] = length
            if size is not None:
                kwargs["size"] = size
            if request.reference_image:
                kwargs["input_reference"] = (
                    "reference.png",
                    request.reference_image,
                    request.reference_mime or "image/png",
                )
            if headers:
                kwargs["extra_headers"] = headers
            try:
                video = await client.videos.create(**kwargs)
            except (openai.BadRequestError, openai.UnprocessableEntityError) as exc:
                refused = exc
                if not names_a_field(exc, self.relaxable_fields):
                    break
                continue
            except openai.APIStatusError as exc:
                raise status_error(exc.status_code, exc, request.model) from None
            except (openai.APIConnectionError, openai.APITimeoutError) as exc:
                raise VideoProviderBusyError(
                    f"The video provider could not be reached: {api_error_message(exc)}"
                ) from exc
            if size != sizes[0]:
                notes.append(
                    f"The model does not make {sizes[0]}: generated at {size}."
                    if size
                    else "The model takes no size: generated at its own."
                )
            if length is None:
                notes.append("The model takes no length: generated at its own.")
            break
        if video is None:
            detail = api_error_message(refused) if refused is not None else "no settings accepted"
            raise VideoGenerationError(f"The video model refused the request: {detail}")
        # The job exists upstream from here: its id is handed out at once, so a
        # later failure resumes it instead of paying for a second video.
        if on_job is not None:
            await on_job(str(video.id))
        data = await self._await_job(client, video, headers, request.model, report, tell)
        return VideoResult(data=data, job_id=str(video.id), notes=notes)
