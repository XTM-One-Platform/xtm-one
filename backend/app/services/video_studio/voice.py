# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Voice-over: a scene's narration as speech.

The speech model is the platform's (Settings > AI and models > Media > Speech
generation, ``router.get_speech_provider``) unless the video names a
provider and model of its own; only a provider the person may use speaks for
them. Any speech provider does: OpenAI, Azure OpenAI, Gemini, ElevenLabs
(the account's cloned voices included) and the speech models an
OpenAI-compatible gateway serves (``app.llm.speech``).

Each scene is spoken with the text before and after it, for the models that
keep a narration continuous, and with the video's pronunciation lexicon
(``lexicon.py``, the platform's entries under the video's own,
``lexicon_store.for_voiceover``): each term the narration writes is said
through the strongest mechanism the speech model offers
(``lexicon.render``: an SSML phoneme, its IPA in the delivery instruction,
the provider's own lexicon - its rules
registered once, the id it answers kept under ``lexicons/`` in the voice cache
- or the text rewritten), the captions keeping the written words. Every clip
goes through the voice chain
(``encoder.condition_voice``: the engine's silences trimmed, high-pass,
de-esser, gentle compression, the reference loudness), so every provider and
every scene speaks at one level, and is cached in object storage under the
digest of everything that decides it (provider, model, voice, style, speed,
the spoken text and its neighbours, the chain's version): re-rendering a
project re-synthesises only the narration that changed.

The editor's preview speaks from the same cache: a manager's preview
speaks in the background the narrations no one has spoken yet
(:func:`prepare`), plans every spoken one at its measured length
(:func:`spoken_seconds`, read from the cached WAV's header) and plays it as
AAC (:func:`preview_audio`). The render then finds them spoken already.

A cached take is never written over: an object key names one sound.
A narration spoken again after it failed its transcript check
(``voice_check``) is another take, kept under its own bytes' name
(:func:`speak_take`); the check's record names the take that passed, which
the render places and the preview plays (:func:`placed`). One the video's
voice failed twice is said with each term it heard wrong twice in another
context (``lexicon.REFRAMED``), then once more slower
(:func:`slower_speed`), then by the voice of the same speech model nearest
the video's (:func:`closest_voice`), never by another provider's.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
import time
from collections import OrderedDict
from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

_CACHE_PREFIX = "video-studio/voice-cache"
#: Enough of a cached WAV to read its length from its header.
_WAV_HEAD_BYTES = 4096
#: The lengths of the narrations this process has seen spoken, by digest.
_SECONDS: OrderedDict[str, float] = OrderedDict()
_SECONDS_KEPT = 4096
#: Shared by the API replicas: a narration a preview is speaking, and why one could not be spoken.
_SPEAKING_KEY = "xtm:video-voice:speaking:{}"
_FAILED_KEY = "xtm:video-voice:failed:{}"
_SPEAKING_TTL = 180
_FAILED_TTL = 600
#: Narrations a process speaks at once for previews; the render speaks its own.
_SPEAKING = asyncio.Semaphore(3)
_TASKS: dict[str, asyncio.Task] = {}
#: The AAC copies the editor plays, on the API's own disk.
_PREVIEW_DIR = Path(tempfile.gettempdir()) / "xtm-voice-previews"
_PREVIEW_BYTES = 256 * 1024 * 1024
_PREVIEW_LOCKS: dict[str, asyncio.Lock] = {}


class VoiceUnavailableError(RuntimeError):
    """No speech model the person may use; ``str()`` says what to configure."""


async def resolve(db, user_id: object, voiceover: Any):
    """The ``router.SpeechTarget`` *voiceover* is spoken with, for *user_id*.

    A voice-over with a ``style`` that follows the platform's speech model,
    when that model takes no written delivery instruction, is spoken by a
    model of the same provider that does - its default when it is
    one, else the first it lists. A video naming its own model keeps it
    (:func:`describe` warns that its style is dropped).
    """
    from app.llm.provider_access import provider_routing
    from app.llm.router import get_speech_provider, select_provider_configs

    provider_id = getattr(voiceover, "provider_id", None)
    legacy = (getattr(voiceover, "provider", None) or "auto").strip()
    if not provider_id and legacy != "auto":
        # An older spec named a provider type: its first config the person may use.
        configs = (await db.execute(select_provider_configs())).scalars().all()
        routing = await provider_routing(db, user_id, configs)
        provider_id = next((c.id for c in routing.team_first if c.provider == legacy), None)
    try:
        target = await get_speech_provider(
            db, user_id=user_id, provider_id=provider_id, model=getattr(voiceover, "model", None)
        )
    except ValueError as exc:
        raise VoiceUnavailableError(f"{exc} The narration is shown as captions.") from None
    if not (getattr(voiceover, "style", None) or "").strip() or names_its_own(voiceover):
        return target
    if load := getattr(target.provider, "load_speech_capabilities", None):
        await load(target.model)
    if target.provider.speech_takes_style(target.model) is not False:
        return target
    styled = await _styled_model(target.provider)
    if styled is None:
        return target
    if own := getattr(voiceover, "voice", None):
        # The video's own voice outranks its style: a model without it would refuse it.
        return replace(target, model=styled) if await _offers(target.provider, styled, own) else target
    # The platform's voice was chosen for its model: another vendor's model may not have it.
    return replace(target, model=styled, voice=target.voice if await _offers(target.provider, styled, target.voice) else None)


async def _offers(provider: Any, model: str, voice: str | None) -> bool:
    """Whether *model* lists *voice* by id or name; a listing that fails keeps it."""
    if not voice:
        return False
    try:
        voices = await provider.list_voices(model)
    except Exception as exc:
        logger.debug("Voices of %s not listed: %s", model, format_exception_message(exc))
        return True
    wanted = voice.strip().casefold()
    return any(wanted in (v.id.casefold(), v.name.casefold()) for v in voices)


async def _styled_model(provider: Any) -> str | None:
    """A speech model of *provider* that follows a delivery instruction: its default when it does, else the first."""
    default = getattr(provider, "default_speech_model", None)
    if default and provider.speech_takes_style(default):
        return default
    try:
        models = await provider.list_speech_models()
    except Exception as exc:  # a listing that fails leaves the platform's model speaking
        logger.debug("Speech models not listed: %s", format_exception_message(exc))
        return None
    return next((model for model in models if provider.speech_takes_style(model)), None)


def detached(target: Any) -> Any:
    """*target* with its provider config copied: spoken from once the session that read it is closed."""
    config = SimpleNamespace(
        id=target.config.id,
        provider=target.config.provider,
        display_name=getattr(target.config, "display_name", None),
    )
    return replace(target, config=config)


def names_its_own(voiceover: Any) -> bool:
    """Whether *voiceover* names a speech model of its own instead of following the platform's."""
    legacy = (getattr(voiceover, "provider", None) or "auto").strip()
    return bool(getattr(voiceover, "provider_id", None) or getattr(voiceover, "model", None) or legacy != "auto")


def _spoken_by(target) -> str:
    return f"{target.model} on {getattr(target.config, 'display_name', None) or target.config.provider}"


async def describe(db, user_id: object, spec: Any) -> str:
    """The ``Voice-over:`` line of a tool result: what will speak *spec*'s narration, or why nothing will.

    Resolved as the render resolves it, so the result says before any render
    whether the video follows the platform's speech model or names its own,
    and with which voice. Empty when nothing is narrated.
    """
    from app.llm.router import get_speech_provider

    voiceover = spec.voiceover
    if not voiceover.enabled or not any(scene.narration for scene in spec.scenes):
        return ""
    try:
        target = await resolve(db, user_id, voiceover)
    except VoiceUnavailableError as exc:
        return f"Voice-over: {exc}"
    except Exception as exc:  # advisory: a provider that cannot be read never fails a save
        return f"Voice-over: not checked ({format_exception_message(exc)[:200]})."
    styled = bool((voiceover.style or "").strip())
    platform = None
    if names_its_own(voiceover) or styled:
        try:
            platform = await get_speech_provider(db, user_id=user_id)
        except Exception as exc:  # the platform's model only adds context to the line
            logger.debug("The platform's speech model was not resolved: %s", format_exception_message(exc))
    whose, style = "the platform's speech model", ""
    if styled and target.provider.speech_takes_style(target.model) is False:
        style = (
            f" Its style is NOT applied: {target.model} takes no delivery instruction. Name a speech model that "
            "does (video_studio_capabilities lists them; preview_voiceover tries one), or drop the style."
        )
    if not names_its_own(voiceover):
        if styled and platform is not None and platform.model != target.model:
            whose = "the platform's provider"
            style = (
                f" Its style: the platform's speech model, {platform.model}, takes no delivery instruction, so "
                f"{target.model} of the same provider speaks it."
            )
    else:
        if platform is not None and (platform.config.id, platform.model) == (target.config.id, target.model):
            whose = "named by the video, the platform's speech model today"
        elif platform is not None:
            whose = (
                f"named by the video; the platform's is {_spoken_by(platform)}: "
                "voiceover without provider_id and model follows it"
            )
        else:
            whose = "named by the video"
    chosen = voiceover.voice or target.voice
    return (
        f"Voice-over: {_spoken_by(target)} ({whose}), {f'voice {chosen}' if chosen else 'its default voice'}.{style}"
    )


def cache_digest(*parts: object) -> str:
    raw = "\x1f".join("" if p is None else f"{p:.3f}" if isinstance(p, float) else str(p).strip() for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def lexicon_of(voiceover: Any, lexicon: Any = None) -> Any:
    """The lexicon a voice-over is said with: *lexicon* when given, else the video's own entries alone."""
    from app.services.video_studio import lexicon as lexicons

    if lexicon is not None:
        return lexicon
    return lexicons.merge((), getattr(voiceover, "pronunciations", None) or ())


#: How much slower a narration the video's voice failed twice is said once more: a share of the video's speed.
SLOWER = 0.85


def slower_speed(speed: float | None) -> float | None:
    """The speed a narration the video's voice failed twice is said once more at; ``None`` when none is slower."""
    from app.llm.speech import MIN_SPEED, clamp_speed

    current = clamp_speed(speed)
    slower = max(MIN_SPEED, round(current * SLOWER, 2))
    return slower if slower < current else None


@dataclass(frozen=True)
class ClosestVoice:
    """The voice of a speech model nearest the video's: which, and what makes it the nearest."""

    voice: str
    name: str
    #: The traits of the provider's descriptions it shares with the video's voice, in the video voice's order.
    shared: tuple[str, ...]
    #: The video's voice as the model lists it, and its traits (none when the model does not describe it).
    of: str
    traits: tuple[str, ...]

    @property
    def label(self) -> str:
        """How a person names it: its name, with its id when the id says something else."""
        return self.name if self.name.casefold() == self.voice.casefold() else f"{self.name} ({self.voice})"

    def why(self) -> str:
        if self.shared:
            return f"the voice of the same speech model sharing the most with {self.of} ({', '.join(self.shared)})"
        if self.traits:
            return f"the first other voice its speech model lists, none sharing a trait with {self.of} ({', '.join(self.traits)})"
        return f"the first other voice its speech model lists, which describes neither it nor {self.of}"


def _traits(description: str) -> tuple[str, ...]:
    """The traits a provider describes a voice by (``speech.Voice.description``: ``american, female, calm``)."""
    return tuple(dict.fromkeys(part.strip().casefold() for part in (description or "").split(",") if part.strip()))


async def closest_voice(target, voice: str | None) -> ClosestVoice | None:
    """The voice of *target*'s speech model nearest *voice*; ``None`` when the model lists no other.

    The nearest shares the most traits with *voice* in what the provider says
    of its voices (accent, gender, age, tone...: ``speech.Voice.description``),
    the provider's own order - its best, or the account's own, first - breaking
    a tie. *voice* ``None`` is the model's default: the voice *target* names,
    else the first it lists.
    """
    voices = await target.provider.list_voices(target.model)
    wanted = (voice or getattr(target, "voice", None) or "").strip().casefold()

    def named(candidate: Any) -> bool:
        return bool(wanted) and wanted in (candidate.id.casefold(), candidate.name.casefold())

    own = next((candidate for candidate in voices if named(candidate)), None)
    if own is None and not wanted and voices:
        own = voices[0]
    others = [candidate for candidate in voices if candidate is not own and not named(candidate)]
    if not others:
        return None
    traits = _traits(own.description) if own is not None else ()

    def shared(candidate: Any) -> tuple[str, ...]:
        theirs = set(_traits(candidate.description))
        return tuple(trait for trait in traits if trait in theirs)

    best = max(others, key=lambda candidate: len(shared(candidate)))
    of = own.name if own is not None else (voice or getattr(target, "voice", None) or "its default voice")
    return ClosestVoice(best.id, best.name, shared(best), of, traits)


def pronunciation_support(target: Any) -> Any:
    """How the speech model of *target* can be told to say a word (``speech.PronunciationSupport``)."""
    from app.llm.speech import PronunciationSupport

    try:
        support = target.provider.speech_pronunciation(target.model)
    except Exception as exc:  # a provider that cannot say gets the text rewrite
        logger.debug("Pronunciation support not read: %s", format_exception_message(exc))
        return PronunciationSupport()
    return support if isinstance(support, PronunciationSupport) else PronunciationSupport()


@dataclass(frozen=True)
class Line:
    """One narration as it is spoken: its text and the narrations on either side of it."""

    scene_id: str
    text: str
    previous_text: str | None
    next_text: str | None
    #: How fast it is said, when not the voice-over's (:func:`line_speed`): its scene's ``narration_speed``.
    speed: float | None = None


def line_speed(line: Line, voiceover: Any) -> float:
    """How fast *line* is said: its own speed (its scene's ``narration_speed``), else *voiceover*'s."""
    return float(line.speed) if line.speed is not None else float(voiceover.speed)


#: The delivery a ``voiceover.style`` naming a preset asks for: the words sent to a speech model that takes
#: a delivery instruction, the preset's name kept in the spec.
STYLE_PRESETS: dict[str, str] = {
    "energetic": (
        "Energetic, upbeat product demo delivery: brisk pace, crisp consonants, a confident rising energy on each "
        "action, short pauses between sentences, never shouting or salesy."
    ),
    "calm": (
        "Calm, measured walkthrough delivery: an even pace, warm and clear, natural pauses after each step, "
        "reassuring and never hurried."
    ),
}


def delivery(style: str | None) -> str | None:
    """What a speech model is asked for a ``voiceover.style``: a preset's words (:data:`STYLE_PRESETS`), else the style
    as written."""
    if style is None:
        return None
    return STYLE_PRESETS.get(style.strip().casefold(), style)


def lines_of(spec: Any, *, within: Collection[str] | None = None) -> list[Line]:
    """The narrated scenes of *spec*, each with its neighbours: what the render and the preview speak alike.

    *within*: the scenes a scene preview draws, each still with its neighbours in the whole of *spec* - a
    window cut at a scene that says nothing would give them others, and a digest the whole render never reads.
    """
    narrated = [scene for scene in spec.scenes if scene.narration]
    return [
        Line(
            scene.id or "",
            scene.narration or "",
            narrated[index - 1].narration if index else None,
            narrated[index + 1].narration if index + 1 < len(narrated) else None,
            getattr(scene, "narration_speed", None),
        )
        for index, scene in enumerate(narrated)
        if within is None or scene.id in within
    ]


@dataclass(frozen=True)
class _Request:
    voice: str | None
    speed: float
    text: str
    previous_text: str | None
    next_text: str | None
    digest: str
    #: How the text says the lexicon's terms (``lexicon.Rendition``).
    rendition: Any = None


def _request(
    target,
    *,
    text: str,
    voice: str | None,
    style: str | None,
    speed: float,
    previous_text: str | None,
    next_text: str | None,
    lexicon: Any,
    level: str,
    reframe: Collection[str] = (),
) -> _Request:
    """What the speech model is asked, and the digest of everything that decides its answer.

    A narration that writes no lexicon term keeps the digest it always had:
    its cached take still plays. *reframe*: the terms said in another context at the ``REFRAMED`` level.
    """
    from app.llm.speech import clamp_speed
    from app.services.video_studio import encoder
    from app.services.video_studio import lexicon as lexicons

    wanted = voice or target.voice
    speed = clamp_speed(speed)
    rendition = lexicons.render(text or "", lexicon, pronunciation_support(target), level=level, reframe=reframe)
    # The neighbours are context, never spoken: their terms rewritten, never as SSML.
    previous_text = lexicon.rewrite(previous_text)
    next_text = lexicon.rewrite(next_text)
    parts: list[object] = [
        target.config.provider,
        str(target.config.id),
        target.model,
        wanted,
        delivery(style),
        speed,
        rendition.text,
        previous_text,
        next_text,
        f"chain{encoder.VOICE_CHAIN_VERSION}",
    ]
    if rendition.terms:
        parts.append(rendition.key())
    return _Request(wanted, speed, rendition.text, previous_text, next_text, cache_digest(*parts), rendition)


def _line_request(target, line: Line, voiceover: Any, lexicon: Any, level: str, reframe: Collection[str] = ()) -> _Request:
    from app.services.video_studio import lexicon as lexicons

    return _request(
        target,
        text=line.text,
        voice=voiceover.voice,
        style=voiceover.style,
        speed=line_speed(line, voiceover),
        previous_text=line.previous_text,
        next_text=line.next_text,
        lexicon=lexicon_of(voiceover, lexicon),
        level=level or lexicons.STRONGEST,
        reframe=reframe,
    )


def digest_of(
    target, line: Line, voiceover: Any, lexicon: Any = None, *, level: str = "", reframe: Collection[str] = ()
) -> str:
    """The cache digest of *line* spoken by *target* with *voiceover* and *lexicon*: the render's and the preview's alike."""
    return _line_request(target, line, voiceover, lexicon, level, reframe).digest


def rendition_of(
    target, line: Line, voiceover: Any, lexicon: Any = None, *, level: str = "", reframe: Collection[str] = ()
) -> Any:
    """How *line* is sent to *target*'s speech model (``lexicon.Rendition``): its mechanism and the exact text."""
    return _line_request(target, line, voiceover, lexicon, level, reframe).rendition


def text_digest_of(target, line: Line, voiceover: Any, lexicon: Any = None) -> str:
    """The digest of *line*'s words as *target* says them with *voiceover*, whatever narrations surround it.

    Its provider, model, voice, style and speed, its text, the lexicon
    entries it writes and the voice chain - never its neighbours, nor how hard
    its terms are said: two takes under one are the same voice saying the
    same words, which a take that passed its check under it vouches for.
    """
    from app.llm.speech import clamp_speed
    from app.services.video_studio import encoder
    from app.services.video_studio import lexicon as lexicons

    said = lexicons.Lexicon(tuple(lexicon_of(voiceover, lexicon).voice().present(line.text)))
    return cache_digest(
        "said",
        target.config.provider,
        str(target.config.id),
        target.model,
        voiceover.voice or target.voice,
        delivery(voiceover.style),
        clamp_speed(line_speed(line, voiceover)),
        line.text,
        said.digest() if said else "",
        f"chain{encoder.VOICE_CHAIN_VERSION}",
    )


def object_name(digest: str) -> str:
    """Where the first take of the narration spoken under *digest* is cached; it is never written over."""
    return f"{_CACHE_PREFIX}/{digest}.wav"


def take_name(digest: str, data: bytes) -> str:
    """Where another take of the same narration is kept: named by its own bytes, so a key names one sound."""
    return f"{_CACHE_PREFIX}/{digest}.take-{hashlib.sha256(data).hexdigest()[:16]}.wav"


#: ``digest -> (until, object name)``: the take a narration's transcript check placed, read again after a while.
_PLACED: OrderedDict[str, tuple[float, str]] = OrderedDict()
_PLACED_SECONDS = 120.0


async def placed(digest: str) -> str:
    """The take a narration spoken under *digest* plays: the one its last transcript check passed, else its first.

    Never another voice's take: a narration the platform's default voice said failed in its own.
    """
    from app.services.video_studio import voice_check

    now = time.monotonic()
    known = _PLACED.get(digest)
    if known is not None and known[0] > now:
        return known[1]
    record = await voice_check.load_record(digest)
    passed = record is not None and record.get("status") == voice_check.PASSED and not voice_check.failed(record)
    name = str(record.get("placed") or "") if passed else ""
    name = name or object_name(digest)
    _PLACED[digest] = (now + _PLACED_SECONDS, name)
    _PLACED.move_to_end(digest)
    while len(_PLACED) > _SECONDS_KEPT:
        _PLACED.popitem(last=False)
    return name


def wav_seconds(head: bytes) -> float | None:
    """The length of a WAV from its first bytes: its ``data`` chunk over the byte rate its ``fmt `` chunk declares."""
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return None
    offset, byte_rate = 12, 0
    while offset + 8 <= len(head):
        chunk, size = head[offset : offset + 4], int.from_bytes(head[offset + 4 : offset + 8], "little")
        if chunk == b"fmt " and offset + 20 <= len(head):
            byte_rate = int.from_bytes(head[offset + 16 : offset + 20], "little")
        elif chunk == b"data":
            return size / byte_rate if byte_rate and 0 < size < 0xFFFFFFFF else None
        offset += 8 + size + (size & 1)
    return None


def _remember(name: str, path: str) -> None:
    try:
        with open(path, "rb") as audio:
            seconds = wav_seconds(audio.read(_WAV_HEAD_BYTES))
    except OSError:
        return
    if seconds:
        _SECONDS[name] = seconds
        _SECONDS.move_to_end(name)
        while len(_SECONDS) > _SECONDS_KEPT:
            _SECONDS.popitem(last=False)


async def spoken_seconds(digest: str) -> float | None:
    """How long a narration already spoken under *digest* lasts (the take it plays); ``None`` when it was never spoken."""
    from app.services.file_storage import FileNotFoundInStorageError, file_storage

    name = await placed(digest)
    if name in _SECONDS:
        return _SECONDS[name]
    try:
        head = await file_storage.read_range(name, 0, _WAV_HEAD_BYTES)
    except FileNotFoundInStorageError:
        return None
    except Exception as exc:  # a cache that cannot be read is a miss
        logger.debug("Voice cache unreadable: %s", format_exception_message(exc))
        return None
    seconds = wav_seconds(head)
    if seconds:
        _SECONDS[name] = seconds
    return seconds


async def fetch(name: str, out_path: str) -> bool:
    """Whether the take kept under *name* is in the cache: written to *out_path* when it is."""
    from app.services.file_storage import FileNotFoundInStorageError, file_storage

    try:
        await file_storage.download_to_path(name, out_path, missing_ok=True)
    except FileNotFoundInStorageError:
        return False
    except Exception as exc:  # a cache that cannot be read is a miss
        logger.debug("Voice cache unreadable: %s", format_exception_message(exc))
        return False
    if os.path.getsize(out_path) <= 44:
        return False
    _remember(name, out_path)
    return True


async def cached(digest: str, out_path: str) -> bool:
    """Whether the first take of the narration spoken under *digest* is in the cache: written to *out_path* when it is."""
    return await fetch(object_name(digest), out_path)


#: ``provider config id + rules digest -> lexicon id``: the provider lexicons this process registered or read.
_LEXICON_IDS: dict[str, str] = {}
_LEXICON_LOCKS: dict[str, asyncio.Lock] = {}


def _lexicon_key(target, rules: Sequence[Any]) -> tuple[str, str]:
    digest = hashlib.sha256(repr(tuple(rules)).encode("utf-8")).hexdigest()[:24]
    return f"{target.config.id}:{digest}", f"{_CACHE_PREFIX}/lexicons/{target.config.id}/{digest}.json"


async def provider_lexicon(target, rules: Sequence[Any]) -> str:
    """The id of the provider lexicon holding *rules*, registered once per provider and per rule set.

    Kept in the voice cache (``lexicons/<config>/<digest>.json``), so every
    replica and every render refers to the same one rather than registering
    it again.
    """
    import json

    from app.services.file_storage import FileNotFoundInStorageError, file_storage

    key, name = _lexicon_key(target, rules)
    if key in _LEXICON_IDS:
        return _LEXICON_IDS[key]
    async with _LEXICON_LOCKS.setdefault(key, asyncio.Lock()):
        if key in _LEXICON_IDS:
            return _LEXICON_IDS[key]
        try:
            stored = json.loads((await file_storage.read_range(name, 0, 4096)).decode("utf-8"))
            lexicon_id = str(stored.get("id") or "") if isinstance(stored, dict) else ""
        except FileNotFoundInStorageError:
            lexicon_id = ""
        except Exception as exc:  # unreadable: registered again
            logger.debug("Provider lexicon id not read: %s", format_exception_message(exc))
            lexicon_id = ""
        if not lexicon_id:
            lexicon_id = await target.provider.register_pronunciation_lexicon(f"xtm-one-{key.rsplit(':', 1)[-1]}", rules)
            body = json.dumps({"id": lexicon_id, "rules": len(rules)}).encode("utf-8")
            try:
                await file_storage.upload_to(body, name, "application/json")
            except Exception as exc:  # unkept: registered again by the next process
                logger.debug("Provider lexicon id not kept: %s", format_exception_message(exc))
        _LEXICON_IDS[key] = lexicon_id
        return lexicon_id


def forget_provider_lexicon(target, rules: Sequence[Any]) -> None:
    """Drop the id this process holds for *rules* (a request referring to it failed): the next read checks again."""
    _LEXICON_IDS.pop(_lexicon_key(target, rules)[0], None)


async def _say(target, asked: _Request, style: str | None, out_path: str) -> Any:
    """*asked* spoken by *target* and conditioned into *out_path*: the provider's result."""
    from app.llm.speech import SpeechRequest
    from app.services.video_studio import encoder

    rendition = asked.rendition
    rules = tuple(rendition.rules) if rendition is not None else ()
    lexicons = (await provider_lexicon(target, rules),) if rules else ()
    try:
        result = await target.provider.synthesize_speech(
            SpeechRequest(
                model=target.model,
                text=asked.text,
                voice=asked.voice,
                style=delivery(style),
                speed=asked.speed,
                previous_text=asked.previous_text,
                next_text=asked.next_text,
                audio_format="wav",
                ssml=bool(rendition is not None and rendition.ssml),
                lexicons=lexicons,
                pronunciations=tuple(rendition.ipa) if rendition is not None else (),
            )
        )
    except Exception:
        if rules:
            forget_provider_lexicon(target, rules)
        raise
    with tempfile.NamedTemporaryFile(suffix=".audio", delete=False) as raw:
        raw.write(result.audio)
        raw_path = raw.name
    try:
        await encoder.condition_voice(raw_path, out_path)
    finally:
        os.unlink(raw_path)
    return result


async def synthesize(
    target,
    *,
    text: str,
    voice: str | None,
    style: str | None,
    speed: float,
    out_path: str,
    previous_text: str | None = None,
    next_text: str | None = None,
    lexicon: Any = None,
    level: str = "",
    reframe: Collection[str] = (),
) -> tuple[str, list[str]]:
    """Write the narration of *text* to *out_path* (48 kHz stereo float WAV, conditioned); the voice used and the notes.

    *lexicon* (``lexicon.Lexicon``) says the terms at *level* (``lexicon.STRONGEST`` by default), those *reframe*
    names in another context at ``lexicon.REFRAMED``.
    """
    from app.services.file_storage import file_storage
    from app.services.video_studio import lexicon as lexicons

    asked = _request(
        target,
        text=text,
        voice=voice,
        style=style,
        speed=speed,
        previous_text=previous_text,
        next_text=next_text,
        lexicon=lexicon if lexicon is not None else lexicons.Lexicon(),
        level=level or lexicons.STRONGEST,
        reframe=reframe,
    )
    if await cached(asked.digest, out_path):
        return asked.voice or "", []
    result = await _say(target, asked, style, out_path)
    try:
        await file_storage.upload_path(out_path, object_name(asked.digest), "audio/wav")
    except Exception as exc:  # the cache is an optimisation
        logger.debug("Voice cache not written: %s", format_exception_message(exc))
    else:
        _remember(object_name(asked.digest), out_path)
    return result.voice, list(result.notes)


async def speak_take(
    target,
    *,
    text: str,
    voice: str | None,
    style: str | None,
    speed: float,
    out_path: str,
    previous_text: str | None = None,
    next_text: str | None = None,
    lexicon: Any = None,
    level: str = "",
    reframe: Collection[str] = (),
) -> tuple[str, list[str], str]:
    """Another take of the narration: never read from the cache, never written over the take it holds.

    Kept under its own bytes' name (:func:`take_name`): ``(voice, notes, object name)``.
    """
    from app.services.file_storage import file_storage
    from app.services.video_studio import lexicon as lexicons

    asked = _request(
        target,
        text=text,
        voice=voice,
        style=style,
        speed=speed,
        previous_text=previous_text,
        next_text=next_text,
        lexicon=lexicon if lexicon is not None else lexicons.Lexicon(),
        level=level or lexicons.STRONGEST,
        reframe=reframe,
    )
    result = await _say(target, asked, style, out_path)
    name = take_name(asked.digest, Path(out_path).read_bytes())
    try:
        await file_storage.upload_path(out_path, name, "audio/wav")
    except Exception as exc:  # unkept, the take still plays in this render
        logger.debug("Voice take not kept: %s", format_exception_message(exc))
    else:
        _remember(name, out_path)
    return result.voice, list(result.notes), name


# ── The editor's preview ──────────────────────────────────────────


async def _redis():
    from app.core.redis import redis_client

    return await redis_client()


async def failure(digest: str) -> str | None:
    """Why a preview could not speak the narration under *digest*, for a while after it failed."""
    try:
        return await (await _redis()).get(_FAILED_KEY.format(digest))
    except Exception as exc:  # no shared state: the preview tries again
        logger.debug("Voice failure not read: %s", format_exception_message(exc))
        return None


async def _claim(digest: str) -> bool:
    """Whether this replica speaks *digest* now: no other one is on it (shared through Redis)."""
    try:
        return bool(await (await _redis()).set(_SPEAKING_KEY.format(digest), "1", nx=True, ex=_SPEAKING_TTL))
    except Exception as exc:  # no shared state: this process alone keeps it to one attempt
        logger.debug("Voice claim not shared: %s", format_exception_message(exc))
        return digest not in _TASKS


async def _release(digest: str, error: str | None) -> None:
    try:
        redis = await _redis()
        if error:
            await redis.set(_FAILED_KEY.format(digest), error[:300], ex=_FAILED_TTL)
        await redis.delete(_SPEAKING_KEY.format(digest))
    except Exception as exc:
        logger.debug("Voice state not shared: %s", format_exception_message(exc))


async def _speak(target, caller: tuple[Any, str], line: Line, voiceover: Any, digest: str, lexicon: Any) -> None:
    from app.llm.identity import set_llm_caller

    set_llm_caller(*caller)
    error = None
    try:
        async with _SPEAKING:
            with tempfile.TemporaryDirectory(prefix="xtm-voice-") as work:
                await synthesize(
                    target,
                    text=line.text,
                    voice=voiceover.voice,
                    style=voiceover.style,
                    speed=line_speed(line, voiceover),
                    out_path=os.path.join(work, "voice.wav"),
                    previous_text=line.previous_text,
                    next_text=line.next_text,
                    lexicon=lexicon_of(voiceover, lexicon),
                )
    except Exception as exc:
        error = format_exception_message(exc)
        logger.info("Preview narration of scene %s not spoken: %s", line.scene_id, error)
    finally:
        await _release(digest, error)
        _TASKS.pop(digest, None)


async def prepare(
    target, user: Any, lines: Sequence[tuple[Line, str]], voiceover: Any, lexicon: Any = None
) -> set[str]:
    """Speak in the background the narrations of a preview no one has spoken yet; the digests being spoken.

    The render reads the same cache, so a narration heard in the editor is
    never paid for again when the video renders. One replica speaks a given
    narration at a time, a few at once per process.
    """
    caller = (user.id, user.email)
    target = detached(target)
    speaking: set[str] = set()
    for line, digest in lines:
        speaking.add(digest)
        if digest in _TASKS or not await _claim(digest):
            continue
        _TASKS[digest] = asyncio.create_task(_speak(target, caller, line, voiceover, digest, lexicon))
    return speaking


async def preview_audio(digest: str) -> Path | None:
    """A spoken narration as a browser plays it (AAC), the take a render places (:func:`placed`), prepared once per take
    on local disk; ``None`` if never spoken."""
    from app.services.file_storage import FileNotFoundInStorageError, file_storage
    from app.services.video_studio import encoder

    name = await placed(digest)
    target = _PREVIEW_DIR / f"{Path(name).stem}.m4a"
    if target.exists():
        os.utime(target, (time.time(), time.time()))
        return target
    async with _PREVIEW_LOCKS.setdefault(name, asyncio.Lock()):
        if target.exists():
            return target
        _PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="xtm-voice-preview-"))
        try:
            wav = workdir / "voice.wav"
            try:
                await file_storage.download_to_path(name, str(wav), missing_ok=True)
            except FileNotFoundInStorageError:
                return None
            partial = workdir / "voice.m4a"
            await encoder.encode_preview_bed(str(wav), str(partial))
            shutil.move(str(partial), target)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            _PREVIEW_LOCKS.pop(name, None)
    _trim_previews()
    return target


def _trim_previews() -> None:
    try:
        entries = sorted(((p.stat().st_mtime, p.stat().st_size, p) for p in _PREVIEW_DIR.glob("*.m4a")), key=lambda e: e[0])
    except OSError:
        return
    total = sum(size for _mtime, size, _path in entries)
    for _mtime, size, path in entries:
        if total <= _PREVIEW_BYTES:
            break
        path.unlink(missing_ok=True)
        total -= size
