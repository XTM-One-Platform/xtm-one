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

"""Voice notes: the audio a message to an agent account carries, heard as text.

A voice message recorded in a chat (a Teams audio clip) or an audio file sent
to an agent account reaches the turn as its transcript, marked as a voice
note with its length: ``[voice note, 0:42, transcribed] ...``. A message with
text and audio carries both, the text first. When the audio cannot be
downloaded or transcribed the turn says so (``[voice note: transcription
failed: <reason>]``), so the agent can answer that it could not hear it
instead of seeing an empty message.

The same lines serve every surface a voice note reaches: the agent accounts
(``pipeline._message_files`` downloads the audio as the account) and the
channel bots (``channels.router``: the Teams bot in a chat, a channel or a
channel thread, and every other channel adapter whose files reach the router).
:func:`heard_line` transcribes one clip with the platform's one transcription
(``app.llm.transcription``) for the person it is heard for; the transcript is
not logged.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

#: What a provider writes for a message with no text (``_teams_message_text``).
EMPTY_TEXT_MARKERS = frozenset({"", "[no text content]"})

#: The extensions of the audio a voice note arrives as (Teams clips, WhatsApp,
#: phone recordings, uploaded files).
AUDIO_EXTENSIONS = frozenset(
    {".m4a", ".mp3", ".mpga", ".mpeg", ".ogg", ".oga", ".opus", ".wav", ".webm", ".aac", ".flac", ".amr"}
)

#: The longest failure reason a turn line keeps.
REASON_LIMIT = 200

# What judges a transcript before a turn trusts it (:func:`judge`). A voice
# note is spoken at 120 to 160 words a minute; a model that lost the audio, or
# misheard its language, writes far fewer words, or invents fluent ones with
# low confidence. The thresholds are Whisper's own (its decoder's fallback
# rules): a segment under -1.0 mean log-probability or over 2.4 gzip ratio is
# one it would have decoded again; over 0.6 no-speech probability with a low
# log-probability, it is silence it wrote words for.
#: Below this length (seconds) a note is too short for a words-per-minute judgement.
MIN_JUDGED_SECONDS = 30
#: Fewer words a minute than this for a note's length: most of the speech was lost.
MIN_WORDS_PER_MINUTE = 60
#: The share of a note's length the audio transcribed must cover (the rest was cut).
MIN_COVERAGE = 0.8
#: Seconds a transcribed audio may fall short of the note's announced length anyway.
COVERAGE_SLACK_SECONDS = 2.0
#: A segment decoded below this mean log-probability is unsure.
LOW_LOGPROB = -1.0
#: A segment whose text compresses past this ratio is the model looping on a phrase.
HIGH_COMPRESSION = 2.4
#: A segment over this no-speech probability (and unsure) is silence written as words.
NO_SPEECH = 0.6
#: The share of the speech (in seconds) decoded unsure past which the whole note is.
MAX_UNSURE_SHARE = 0.3


def is_audio(content_type: str | None, filename: str | None = None) -> bool:
    """Whether a file is audio: an ``audio/*`` media type, else an audio extension."""
    media = str(content_type or "").split(";", 1)[0].strip().lower()
    if media.startswith("audio/"):
        return True
    if media and media != "application/octet-stream":
        return False
    return os.path.splitext(str(filename or ""))[1].lower() in AUDIO_EXTENSIONS


def format_duration(seconds: float | None) -> str | None:
    """``m:ss`` (``h:mm:ss`` past an hour), ``None`` when unknown."""
    if seconds is None or seconds < 0:
        return None
    total = round(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def transcribed_line(text: str, duration: float | None) -> str:
    """The turn line of a voice note heard: ``[voice note, 0:42, transcribed] <text>``."""
    length = format_duration(duration)
    head = f"[voice note, {length}, transcribed]" if length else "[voice note, transcribed]"
    words = " ".join(str(text or "").split())
    return f"{head} {words}" if words else f"{head} (no speech heard)"


def failed_line(reason: str, duration: float | None = None) -> str:
    """The turn line of a voice note that could not be heard, with why."""
    why = " ".join(str(reason or "unknown error").split())[:REASON_LIMIT] or "unknown error"
    length = format_duration(duration)
    return (
        f"[voice note, {length}: transcription failed: {why}]"
        if length
        else f"[voice note: transcription failed: {why}]"
    )


def unread_line(reason: str) -> str:
    """The turn line of an attachment of an otherwise empty message that could not be read."""
    why = " ".join(str(reason or "unknown error").split())[:REASON_LIMIT] or "unknown error"
    return f"[attachment (possibly a voice note) could not be read: {why}]"


def low_confidence_line(reason: str, duration: float | None = None, language: str | None = None) -> str:
    """The turn line of a voice note heard but not to be trusted: why, and never the doubtful text."""
    why = " ".join(str(reason or "unknown").split())[:REASON_LIMIT] or "unknown"
    parts = [p for p in (format_duration(duration), _heard_in(language)) if p]
    head = ", ".join(["voice note", *parts])
    return f"[{head}: transcription low confidence: {why}]"


def _heard_in(language: str | None) -> str | None:
    name = " ".join(str(language or "").split()).lower()[:32]
    return f"heard in {name}" if name else None


def _segment_is_silence(segment: Any, hallucinations: tuple[str, ...]) -> bool:
    """A segment the model wrote where nobody speaks: a known closing line, or silence decoded unsure."""
    text = str(getattr(segment, "text", "") or "").strip()
    if any(text.startswith(h) for h in hallucinations):
        return True
    no_speech = getattr(segment, "no_speech_prob", None)
    logprob = getattr(segment, "avg_logprob", None)
    return no_speech is not None and no_speech > NO_SPEECH and (logprob is None or logprob < LOW_LOGPROB)


def _segment_is_unsure(segment: Any) -> bool:
    logprob = getattr(segment, "avg_logprob", None)
    ratio = getattr(segment, "compression_ratio", None)
    return (logprob is not None and logprob < LOW_LOGPROB) or (ratio is not None and ratio > HIGH_COMPRESSION)


def judge(transcript: Any, announced_seconds: float | None = None) -> tuple[str, str | None]:
    """``(text, None)`` of a transcript a turn may trust, or ``(text, reason)`` of one it may not.

    *announced_seconds* is the note's length as the sender's app announced it
    (WhatsApp's ``seconds``), else the length the model measured. The text
    is the transcript without the segments written where nobody speaks.
    A transcript is not trusted when the audio transcribed is much shorter
    than the note (cut before it was heard), when it holds far too few words
    for the note's length, or when most of its speech was decoded unsure.
    """
    from app.llm.transcription import known_hallucinations

    text = " ".join(str(getattr(transcript, "text", "") or "").split())
    heard = getattr(transcript, "duration", None)
    length = announced_seconds if announced_seconds and announced_seconds > 0 else heard
    segments = tuple(getattr(transcript, "segments", ()) or ())
    if segments:
        hallucinations = known_hallucinations(getattr(transcript, "model", None))
        speech = [s for s in segments if not _segment_is_silence(s, hallucinations)]
        text = " ".join(" ".join(s.text for s in speech).split())
        spoken = sum(max(0.0, s.end - s.start) for s in speech)
        unsure = sum(max(0.0, s.end - s.start) for s in speech if _segment_is_unsure(s))
        if spoken > 0 and unsure / spoken > MAX_UNSURE_SHARE:
            return text, f"{round(100 * unsure / spoken)}% of the speech was decoded unsure"
    cut = heard is not None and heard < MIN_COVERAGE * (announced_seconds or 0) - COVERAGE_SLACK_SECONDS
    if announced_seconds and announced_seconds >= MIN_JUDGED_SECONDS and cut:
        return text, f"only {format_duration(heard)} of the audio was heard"
    if length and length >= MIN_JUDGED_SECONDS:
        words = len(text.split())
        if words < MIN_WORDS_PER_MINUTE * length / 60:
            return text, f"{words} words heard in {format_duration(length)}, most of the speech is missing"
    return text, None


def judged_line(transcript: Any, announced_seconds: float | None = None, *, say_language: bool = False) -> str:
    """The turn line of a transcript as :func:`judge` finds it: the text heard, or why it is not trusted.

    With *say_language* the line names the language the model heard
    (``[voice note, 0:42, transcribed, heard in french]``), so a switch of
    language is never silent.
    """
    text, doubt = judge(transcript, announced_seconds)
    length = announced_seconds if announced_seconds and announced_seconds > 0 else getattr(transcript, "duration", None)
    language = getattr(transcript, "language", None) if say_language else None
    if doubt:
        return low_confidence_line(doubt, length, language)
    heard_in = _heard_in(language)
    line = transcribed_line(text, length)
    if heard_in:
        line = line.replace(" transcribed]", f" transcribed, {heard_in}]", 1)
    return line


async def heard_line(db: Any, user_id: object, audio: bytes, filename: str, *, where: str) -> str:
    """The turn line of one voice note heard for *user_id*: its transcript, or why it could not be heard.

    The language is detected. *where* names the surface in the log line of a
    failure (never the transcript).
    """
    from app.llm.transcription import resolve_transcriber

    try:
        transcriber = await resolve_transcriber(db, user_id)
        transcript = await transcriber.transcribe(audio, filename=filename, words=True)
    except Exception as exc:
        logger.info("%s: a voice note could not be transcribed: %s", where, type(exc).__name__)
        return failed_line(str(exc) or type(exc).__name__)
    return judged_line(transcript)


def with_voice_notes(text: str, lines: list[str]) -> str:
    """The message text with its voice notes: the text first, then each note; the notes alone for an empty text."""
    if not lines:
        return text
    own = str(text or "").strip()
    notes = "\n".join(lines)
    return notes if own in EMPTY_TEXT_MARKERS else f"{own}\n{notes}"
