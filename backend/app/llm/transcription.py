# SPDX-License-Identifier: Apache-2.0

"""Speech-to-text: the platform's one transcription.

The chat's dictation (``POST /chat/stt``) and the Video Studio's voice checks
(``video_studio.voice_check``, ``video_studio.audio_qc``) transcribe through
it, so which provider transcribes for a person is decided once: OpenAI's
audio transcription API (:data:`TRANSCRIPTION_MODEL`) on the key of the first
OpenAI provider the person may use, their team's first, whatever
speaks the video. A transcriber names that provider (:func:`provider_label`)
and a render's checks say it beside the model. A key read
from a vault that cannot be read is skipped like an undecryptable one, but
when no provider has a key that failure is raised, not "no provider".

A transcript can carry its words with their times (``words=True``, the
``verbose_json`` answer), its segments and the language it was heard in: the
video checks place what they find on the video's timeline from them. What a
model is known to write where nobody speaks - silence, music - is declared
with it (:func:`known_hallucinations`), so a check drops it rather than
grading it as words said.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

TRANSCRIPTION_MODEL = "whisper-1"
#: The largest audio file the transcription API takes.
MAX_AUDIO_BYTES = 25 * 1024 * 1024
#: Per model, what it writes where nobody speaks (silence, a music bed): the closing lines of the subtitled videos it
#: learnt on, in any language. A segment of a transcript starting with one is no speech, unless the text read writes it.
_HALLUCINATIONS: dict[str, tuple[str, ...]] = {
    TRANSCRIPTION_MODEL: ("Diolch am wylio'r fideo", "Thanks for watching", "Sous-titres réalisés par", "字幕"),
}


def known_hallucinations(model: str | None) -> tuple[str, ...]:
    """What *model* is known to write where nobody speaks (:data:`_HALLUCINATIONS`); none for a model not listed."""
    return _HALLUCINATIONS.get(str(model or ""), ())


class TranscriptionUnavailableError(RuntimeError):
    """No provider may transcribe for the person; ``str()`` says what to configure."""


@dataclass(frozen=True, slots=True)
class TranscribedWord:
    text: str
    #: Seconds into the audio transcribed.
    start: float
    end: float


@dataclass(frozen=True, slots=True)
class TranscribedSegment:
    """A stretch of a transcript as the model decoded it: its text, and when it is said (seconds into the audio).

    The verbose answer also says how sure the model was of it: what judges a
    transcript before it is trusted (``agent_accounts.voice_notes.judge``).
    """

    text: str
    start: float
    end: float
    #: The mean log-probability of the segment's tokens: the lower, the less sure the model was.
    avg_logprob: float | None = None
    #: The model's probability that the segment holds no speech at all.
    no_speech_prob: float | None = None
    #: How repetitive the segment's text is (its gzip ratio): a model looping on a phrase writes it high.
    compression_ratio: float | None = None


@dataclass(frozen=True)
class Transcript:
    text: str
    #: Each word with its time, when they were asked for (``words=True``) and the model gives them.
    words: tuple[TranscribedWord, ...] = ()
    #: The language the model heard the audio in, as it names it (``english``).
    language: str | None = None
    model: str = TRANSCRIPTION_MODEL
    #: Each segment with its time, beside the words (``words=True``).
    segments: tuple[TranscribedSegment, ...] = ()
    #: The length of the audio in seconds, when the model says it (``words=True``).
    duration: float | None = None


async def openai_key(db: Any, user_id: object, capability: str) -> str:
    """The key of the OpenAI provider *user_id* may use for *capability*, their team's first."""
    return (await _openai_credentials(db, user_id, capability))[1]


def provider_label(config: Any) -> str:
    """How a transcription names the provider it ran on: the vendor, and the name an administrator gave it."""
    name = str(getattr(config, "display_name", None) or "").strip()
    return f"OpenAI ({name})" if name else "OpenAI"


async def _openai_credentials(db: Any, user_id: object, capability: str) -> tuple[Any, str]:
    """``(config, key)`` of the OpenAI provider *user_id* may use for *capability* (:func:`openai_key`)."""
    from app.llm.provider_access import usable_provider_configs
    from app.services.vault import reveal_field
    from app.services.vault.errors import SecretUnavailableError

    unavailable: SecretUnavailableError | None = None
    for config in await usable_provider_configs(db, user_id, ("openai",)):
        try:
            api_key = (await reveal_field(config, "api_key_enc") or "").strip()
        except SecretUnavailableError as exc:
            unavailable = exc
            continue
        except Exception as exc:
            logger.warning(
                "Skipping LLM provider %s for %s: its API key cannot be decrypted (%s)",
                config.id,
                capability,
                format_exception_message(exc),
            )
            continue
        if api_key:
            return config, api_key
    if unavailable is not None:
        raise unavailable
    raise TranscriptionUnavailableError(
        f"An OpenAI provider available to you is required for {capability}. "
        "An administrator can configure one in Settings > AI and models > Models."
    )


def _field(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def _timed(items: Any, name: str, kind: type) -> tuple:
    out = []
    for item in items or ():
        text, start, end = _field(item, name), _field(item, "start"), _field(item, "end")
        if isinstance(text, str) and text.strip() and isinstance(start, int | float) and isinstance(end, int | float):
            out.append(kind(text.strip(), float(start), float(end)))
    return tuple(out)


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _segments(items: Any) -> tuple[TranscribedSegment, ...]:
    """The segments of a verbose answer, each with how sure the model was of it."""
    return tuple(
        TranscribedSegment(
            timed.text,
            timed.start,
            timed.end,
            avg_logprob=_number(_field(item, "avg_logprob")),
            no_speech_prob=_number(_field(item, "no_speech_prob")),
            compression_ratio=_number(_field(item, "compression_ratio")),
        )
        for item in items or ()
        for timed in _timed((item,), "text", TranscribedSegment)
    )


def _transcript(result: Any, model: str) -> Transcript:
    text = _field(result, "text")
    duration = _number(_field(result, "duration"))
    return Transcript(
        text=(text if isinstance(text, str) else str(result or "")).strip(),
        words=_timed(_field(result, "words"), "word", TranscribedWord),
        language=_field(result, "language") or None,
        model=model,
        segments=_segments(_field(result, "segments")),
        duration=duration,
    )


@dataclass(frozen=True)
class Transcriber:
    """Transcribes for one person: their provider's key, the model, and the provider as it is named (:func:`provider_label`)."""

    api_key: str = field(repr=False)
    model: str = TRANSCRIPTION_MODEL
    provider: str = "OpenAI"

    async def transcribe(
        self, audio: bytes, *, filename: str = "audio.wav", language: str | None = None, words: bool = False
    ) -> Transcript:
        """The text said in *audio*; with *words*, each word and each segment, and when it is said."""
        from openai import AsyncOpenAI

        if len(audio) > MAX_AUDIO_BYTES:
            raise ValueError(f"Audio too large to transcribe: at most {MAX_AUDIO_BYTES // (1024 * 1024)} MB.")
        kwargs: dict[str, Any] = {"model": self.model, "file": (filename, audio)}
        if language:
            kwargs["language"] = language
        if words:
            kwargs["response_format"] = "verbose_json"
            kwargs["timestamp_granularities"] = ["word", "segment"]
        async with AsyncOpenAI(api_key=self.api_key) as client:
            result = await client.audio.transcriptions.create(**kwargs)
        return _transcript(result, self.model)


async def resolve_transcriber(db: Any, user_id: object) -> Transcriber:
    """What transcribes for *user_id*: :class:`TranscriptionUnavailableError` when no provider may."""
    config, api_key = await _openai_credentials(db, user_id, "speech-to-text")
    return Transcriber(api_key, provider=provider_label(config))
