# SPDX-License-Identifier: Apache-2.0

"""Music generation through the LLM providers.

The contract every provider answers (``BaseLLMProvider.list_music_models`` /
``generate_music``), the request and result types and the errors. ElevenLabs
(``POST /v1/music``) and Gemini (Lyria, through the Interactions API) implement
it in their own providers.

Which models compose is read from the provider and classified with
``router.is_music_model``; the platform's choice of provider and model is
``PlatformSettings.music_*`` (Settings > AI and models > Media > Music generation),
resolved by ``router.get_music_provider``. The Video Studio's AI tracks
(``services/video_studio/music.py``) are its consumer.

A composition is one request answered with the audio: nothing runs upstream
once it fails, so a request the provider could not take now (no answer, a
rate limit, a server error) is :class:`MusicProviderBusyError` and the job
asks again after a back-off; any other refusal is :class:`MusicGenerationError`.
The gateway's provider is stricter: a request is billed once it may have
composed, so only what it knows was not (a 429, a ``503 gateway_overloaded``,
a retryable ``502 provider_error``, no connection) is busy.

A request may continue a reference clip (:attr:`MusicRequest.continue_from`):
the music that follows the clip, starting where it ends, on the models a
provider says take one (``BaseLLMProvider.continues_music``). A provider whose
answer replays the clip before its continuation says where the new music starts
(:attr:`MusicResult.continues_at`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The lengths a track is asked for, in seconds: the shortest a bed makes sense
#: at, the longest the providers compose in one request.
MIN_SECONDS = 5.0
MAX_SECONDS = 600.0


class MusicGenerationError(RuntimeError):
    """Music the provider could not make; ``str()`` is safe to show."""


class MusicProviderBusyError(RuntimeError):
    """The provider could not take the request now (rate limit, outage, network): asked again later - after
    *retry_after* seconds when the provider said how long, else after the job's own back-off."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class MusicReference:
    """A clip of music a composition continues: what comes just before the music asked for."""

    audio: bytes
    #: ``audio/wav`` or ``audio/mpeg``.
    mime: str
    seconds: float


@dataclass
class MusicRequest:
    model: str
    #: What the music is: genre, mood, instruments, tempo, how it evolves.
    prompt: str
    #: The length wanted; a model that makes a fixed length says so in the notes. With :attr:`continue_from`, the
    #: length of the continuation alone.
    duration_seconds: float = 60.0
    #: No vocals: what a bed under a narration needs.
    instrumental: bool = True
    #: What the music must not have ("drums", "brass"), for the models that take it.
    negative_prompt: str | None = None
    #: The clip the music continues, from where it ends (``BaseLLMProvider.continues_music``); ``None``: a new piece.
    continue_from: MusicReference | None = None


@dataclass
class MusicResult:
    audio: bytes
    #: ``audio/mpeg`` or ``audio/wav``.
    mime: str
    #: The lyrics or the structure the model wrote with the music, when it gives them.
    description: str = ""
    #: What was done differently from the request (another length, no negative prompt).
    notes: list[str] = field(default_factory=list)
    #: Seconds into :attr:`audio` where the continuation of :attr:`MusicRequest.continue_from` starts: the clip's
    #: length for a provider that replays it first, 0 for one that answers with the new music alone.
    continues_at: float = 0.0


def clamp_seconds(value: float | None) -> float:
    try:
        seconds = float(value or 60.0)
    except (TypeError, ValueError):
        seconds = 60.0
    return min(MAX_SECONDS, max(MIN_SECONDS, seconds))


def is_transient(code: int | None) -> bool:
    """A failure worth asking again: no HTTP answer at all, a rate limit, a server error."""
    return code is None or code == 429 or code >= 500


def status_error(code: int | None, reason: str, *, vendor: str, retry_after: float | None = None) -> RuntimeError:
    """The error for a refused composition: busy when it is worth asking again - after *retry_after* seconds when the
    provider named them -, readable when not."""
    detail = f": {reason.strip()[:300]}" if reason and reason.strip() else ""
    if is_transient(code):
        what = f"HTTP {code}" if code is not None else "no answer"
        return MusicProviderBusyError(f"{vendor} could not compose now ({what}){detail}", retry_after=retry_after)
    return MusicGenerationError(f"{vendor} refused the music request (HTTP {code}){detail}")


def length_label(seconds: float) -> str:
    """``1:05`` for 65 s: how a length is written in a prompt to a music model."""
    whole = round(seconds)
    return f"{whole // 60}:{whole % 60:02d}"
