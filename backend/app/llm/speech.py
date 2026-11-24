# SPDX-License-Identifier: Apache-2.0

"""Speech generation through the LLM providers.

The contract every provider answers (``BaseLLMProvider.list_speech_models`` /
``list_voices`` / ``synthesize_speech``), the request, result and voice types,
and the one implementation of the OpenAI speech API (``POST /audio/speech``)
shared by every OpenAI-shaped provider: OpenAI itself, Azure OpenAI's v1
endpoint, and the OpenAI-compatible gateways (LiteLLM, a custom
OpenAI-compatible endpoint, the Filigran gateway) that serve other vendors'
speech models behind the same API. Gemini and ElevenLabs implement their own.

Which models speak is read from the provider and classified with
``router.is_speech_model``, catalog first; the platform's choice of provider,
model and voice is ``PlatformSettings.speech_*`` (Settings > AI and models > Media >
Speech generation), resolved by ``router.get_speech_provider``. The chat's
read-aloud and the Video Studio voice-over both speak through it.
"""

from __future__ import annotations

import asyncio
import io
import logging
import wave
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: The narration speeds every speech API takes.
MIN_SPEED = 0.7
MAX_SPEED = 1.3

#: The OpenAI voices (``gpt-4o-mini-tts`` and its successors); ``marin`` and
#: ``cedar`` sound best.
OPENAI_VOICES: tuple[str, ...] = (
    "marin",
    "cedar",
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "fable",
    "nova",
    "onyx",
    "sage",
    "shimmer",
    "verse",
)
#: The smaller set of the first OpenAI speech models (``tts-1``, ``tts-1-hd``).
OPENAI_CLASSIC_VOICES: tuple[str, ...] = (
    "nova",
    "alloy",
    "ash",
    "coral",
    "echo",
    "fable",
    "onyx",
    "sage",
    "shimmer",
)


class SpeechGenerationError(RuntimeError):
    """Speech the provider could not make; ``str()`` is safe to show."""


@dataclass(frozen=True)
class Voice:
    id: str
    name: str
    #: Where the voice comes from: ``premade``, ``cloned``, ``generated``,
    #: ``professional``, ``prebuilt``...; empty for a fixed built-in set.
    category: str = ""
    #: What it sounds like (accent, gender, tone), as the provider describes it.
    description: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name, "category": self.category, "description": self.description}


@dataclass(frozen=True)
class PronunciationSupport:
    """How a speech model can be told to say a word as a pronunciation lexicon says it.

    The Video Studio voice-over (``video_studio.lexicon.render``) uses the
    strongest one a model offers; with none, it rewrites the text it sends.
    """

    #: The SSML the model reads: ``"w3c"`` (a ``<speak>`` document), ``"inline"`` (tags in plain text), ``""`` (none).
    ssml: str = ""
    #: It honours ``<phoneme alphabet="ipa">``, and ``<sub alias>``.
    phoneme: bool = False
    sub: bool = False
    #: It takes each term's IPA in its delivery instruction (``SpeechRequest.pronunciations``).
    instruction: bool = False
    #: The provider keeps a pronunciation lexicon a request refers to by id (``register_pronunciation_lexicon``).
    lexicon: bool = False


@dataclass(frozen=True)
class LexiconRule:
    """A rule of a provider's pronunciation lexicon: *text* as written is read as *alias*."""

    text: str
    alias: str
    case_sensitive: bool = True


@dataclass
class SpeechRequest:
    model: str
    text: str
    voice: str | None = None
    #: How the voice speaks ("warm, confident, never rushed"), for the models
    #: that take a delivery instruction.
    style: str | None = None
    speed: float = 1.0
    #: The text spoken just before and just after, for the models that keep
    #: a narration split into clips continuous.
    previous_text: str | None = None
    next_text: str | None = None
    #: ``"mp3"`` for a player, ``"wav"`` for a mix. A preference: the result
    #: says what came back.
    audio_format: str = "mp3"
    #: *text* carries the SSML the model reads (``PronunciationSupport.ssml``).
    ssml: bool = False
    #: The provider's pronunciation lexicons the model reads it with (``register_pronunciation_lexicon``).
    lexicons: tuple[str, ...] = ()
    #: ``(written form, IPA)`` of each term *text* writes that the model is told to say as its IPA, in its delivery
    #: instruction (``PronunciationSupport.instruction``).
    pronunciations: tuple[tuple[str, str], ...] = ()


@dataclass
class SpeechResult:
    audio: bytes
    #: The audio type returned, ``audio/mpeg`` or ``audio/wav`` as a rule.
    mime: str
    #: The voice that spoke.
    voice: str
    #: What was done differently from the request (another voice, no style).
    notes: list[str] = field(default_factory=list)


def clamp_speed(speed: float | None) -> float:
    return min(MAX_SPEED, max(MIN_SPEED, float(speed or 1.0)))


def pick_voice(voices: Sequence[Voice], wanted: str | None, default: str | None, notes: list[str]) -> str:
    """The voice to speak with: *wanted* by id or name, else *default*, else the first offered.

    A voice the model does not offer is replaced, with a note naming both.
    """
    if not voices:
        if wanted or default:
            return str(wanted or default)
        raise SpeechGenerationError("The speech model offers no voice.")

    def _match(name: str | None) -> Voice | None:
        if not name:
            return None
        key = name.strip().lower()
        return next((v for v in voices if v.id.lower() == key), None) or next(
            (v for v in voices if v.name.lower() == key), None
        )

    found = _match(wanted)
    if found is not None:
        return found.id
    fallback = _match(default) or voices[0]
    if wanted:
        notes.append(f'Voice "{wanted}" is not offered by this model: spoken with {fallback.name}.')
    return fallback.id


def pcm_to_wav(pcm: bytes, *, rate: int = 24000) -> bytes:
    """Wrap 16-bit mono little-endian PCM in a WAV header."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)
    return buffer.getvalue()


def _takes_instructions(model: str) -> bool:
    """Whether an OpenAI speech model takes ``instructions``: every one after ``tts-1``."""
    return not model.lower().rsplit("/", 1)[-1].startswith("tts-1")


def pronunciation_instruction(pronunciations: Sequence[tuple[str, str]]) -> str:
    """How a model taking a delivery instruction is told to say each term: its IPA, wherever the text writes it."""
    if not pronunciations:
        return ""
    said = "; ".join(f'"{written}" as /{ipa}/' for written, ipa in pronunciations)
    return f"Pronounce these words exactly as their IPA reads, every time the text says them: {said}."


_MIME_ALIASES = {"audio/x-wav": "audio/wav", "audio/wave": "audio/wav", "audio/mp3": "audio/mpeg"}


def _returned_mime(response: Any, asked: str) -> str:
    """The audio type the server says it sent, else *asked*: a gateway may answer another format (WAV for Gemini)."""
    headers = getattr(getattr(response, "response", None), "headers", None)
    content_type = headers.get("content-type") if headers is not None else None
    mime = str(content_type or "").split(";", 1)[0].strip().lower()
    return _MIME_ALIASES.get(mime, mime) if mime.startswith("audio/") else asked


def _speaks_openai_voices(model: str | None) -> bool:
    """Whether *model* is one of OpenAI's speech models, which speak the OpenAI voices."""
    base = (model or "").lower().rsplit("/", 1)[-1]
    return base.startswith("tts-") or (base.startswith("gpt-") and "tts" in base)


class OpenAISpeechMixin:
    """The OpenAI speech API on ``self.client`` (an ``AsyncOpenAI``-shaped client).

    A provider whose speech route lives elsewhere overrides
    :meth:`_speech_client`; one that must pin the upstream per call overrides
    :meth:`_speech_headers`; one that types its models overrides
    :meth:`list_speech_models`. A gateway serving other vendors' voices sets
    :attr:`speech_voices_are_fixed` to ``False``: the OpenAI voices are then
    suggestions and any voice its upstream knows is passed through.
    """

    client: Any
    speech_voices_are_fixed: bool = True

    def _speech_client(self) -> Any:
        return self.client

    async def _speech_headers(self, model: str) -> dict[str, str]:
        return {}

    async def list_speech_models(self) -> list[str]:
        from app.llm.router import is_speech_model

        try:
            response = await self._speech_client().models.list()
        except Exception as exc:
            logger.info("Speech model listing failed: %s", format_exception_message(exc))
            return []
        return sorted({m.id for m in response.data if is_speech_model(m.id)})

    def speech_takes_style(self, model: str) -> bool | None:
        """OpenAI's models after ``tts-1`` take ``instructions``; another vendor's behind a gateway is unknown."""
        return _takes_instructions(model) if _speaks_openai_voices(model) else None

    def speech_pronunciation(self, model: str) -> PronunciationSupport:
        """OpenAI's models after ``tts-1`` read no SSML but take a term's IPA in ``instructions``."""
        return PronunciationSupport(instruction=bool(self.speech_takes_style(model)))

    async def list_voices(self, model: str | None = None) -> list[Voice]:
        if not self.speech_voices_are_fixed and model and not _speaks_openai_voices(model):
            # Another vendor's model behind a gateway: its voices are its own.
            return []
        names = OPENAI_VOICES if not model or _takes_instructions(model) else OPENAI_CLASSIC_VOICES
        return [Voice(name, name.title()) for name in names]

    async def synthesize_speech(self, request: SpeechRequest) -> SpeechResult:
        import openai

        notes: list[str] = []
        # A passed-through voice needs no listing, nor a listing that may fail.
        voices = [] if request.voice and not self.speech_voices_are_fixed else await self.list_voices(request.model)
        if self.speech_voices_are_fixed:
            voice = pick_voice(voices, request.voice, None, notes)
        elif request.voice or voices:
            voice = request.voice or voices[0].id
        else:
            raise SpeechGenerationError(
                f"Name the voice {request.model} speaks with (Settings > AI and models > Media > Speech generation)."
            )
        response_format = "wav" if request.audio_format == "wav" else "mp3"
        kwargs: dict[str, Any] = {
            "model": request.model,
            "voice": voice,
            "input": request.text,
            "response_format": response_format,
            "speed": clamp_speed(request.speed),
        }
        guide = pronunciation_instruction(request.pronunciations)
        dropped = " and ".join(
            what for what, given in (("the style", request.style), ("the IPA of its terms", guide)) if given
        )
        if request.style or guide:
            if self.speech_takes_style(request.model) is not False and _takes_instructions(request.model):
                kwargs["instructions"] = "\n\n".join(part for part in (request.style, guide) if part)
            else:
                notes.append(f"{request.model} takes no delivery instruction: spoken without {dropped}.")
        headers = await self._speech_headers(request.model)
        if headers:
            kwargs["extra_headers"] = headers
        client = self._speech_client()
        try:
            response = await client.audio.speech.create(**kwargs)
        except openai.BadRequestError:
            if "instructions" not in kwargs:
                raise
            kwargs.pop("instructions")
            notes.append(f"{request.model} refused the delivery instruction: spoken without {dropped}.")
            response = await client.audio.speech.create(**kwargs)
        data = response.read() if hasattr(response, "read") else response.content
        if asyncio.iscoroutine(data):
            data = await data
        mime = _returned_mime(response, "audio/wav" if response_format == "wav" else "audio/mpeg")
        return SpeechResult(audio=bytes(data), mime=mime, voice=voice, notes=notes)
