"""Voice notes in the Teams messages the Microsoft tools read.

A voice clip recorded in a Teams chat or channel is an ``audio/*``
attachment of the Graph ``chatMessage``, served by Graph itself. The tools
that read ONE message in full (``get_microsoft_chat_message``,
``get_microsoft_channel_message``) hear each clip and append its line, as
data: ``[voice note, 0:20, transcribed] ...`` or why it could not be heard
(``voice_notes.failed_line``). The listings name the clips and the call that
hears them (:func:`listing_note`), never transcribing per listed message.

A clip recorded in Teams is often NOT an attachment: Teams keeps it as a
hosted content of the message (``/hostedContents``), the body showing no text
and no ``<img>`` for it, its ``contentType`` absent and its id decoding to an
``.../views/audio`` object. :func:`audio_hosted_contents` tells those apart and
:func:`heard_hosted_lines` hears them from the message's own Graph URL; a
listing marks such an empty-looking message as a possible voice clip.

The clip is downloaded with the integration's token from the Graph host only
(never another host), transcribed with the platform's one transcription
(``voice_notes.heard_line``) for the person the run acts for, and not kept;
the transcript is not logged.
"""

from __future__ import annotations

import base64
import binascii
import logging
from typing import Any
from urllib.parse import quote as _url_encode
from urllib.parse import unquote as _url_decode

from ._common import _BASE_URL, _PROVIDER, MicrosoftContext

logger = logging.getLogger(__name__)

#: The most clips one read hears; the rest are stated, not transcribed.
MAX_HEARD = 5


def audio_attachments(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """The voice clips of a Graph ``chatMessage``: its ``audio/*`` attachments."""
    from app.services.agent_accounts.voice_notes import is_audio

    clips: list[dict[str, Any]] = []
    for att in msg.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        kind = str(att.get("contentType") or "")
        if kind.lower().startswith("audio/") or (kind == "reference" and is_audio(None, str(att.get("name") or ""))):
            clips.append(att)
    return clips


def _decoded_hosted_id(hosted_id: str) -> str:
    """A hosted content id as Teams encodes it (base64 of an ``id=...`` locator), lower-cased; ``""`` when not base64."""
    raw = _url_decode(hosted_id).strip()
    padded = raw + "=" * (-len(raw) % 4)
    for decode in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            return decode(padded).decode("utf-8", "ignore").lower()
        except (binascii.Error, ValueError):
            continue
    return ""


def is_audio_hosted_content(item: dict[str, Any]) -> bool:
    """Whether a hosted content of a message is a voice clip.

    Declared ``audio/*``, or (Teams leaves the media type out of a recorded
    clip) an id that names an ``.../views/audio`` object, encoded or not.
    """
    if str(item.get("contentType") or "").split(";", 1)[0].strip().lower().startswith("audio/"):
        return True
    hosted_id = str(item.get("id") or "")
    return "views/audio" in hosted_id.lower() or "views/audio" in _decoded_hosted_id(hosted_id)


def audio_hosted_contents(hosted: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """*hosted* split into its voice clips and the rest, each in order."""
    clips: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for item in hosted:
        if isinstance(item, dict) and item.get("id") and is_audio_hosted_content(item):
            clips.append(item)
        else:
            rest.append(item)
    return clips, rest


def _looks_textless(msg: dict[str, Any], text: str | None) -> bool:
    """A message that shows no text, no file and no pasted image: where a hosted voice clip hides.

    The same test as the read tools' (``_teams._looks_empty``): what the
    listing SHOWS of the attachments counts, not whether Graph sent any. A
    clip recorded in Teams comes with an ``attachments`` entry that renders
    nothing (no file, no card, no name), so ``not msg["attachments"]`` hid
    the note from every listing (#9628 retest).
    """
    from app.services.agent_accounts.voice_notes import EMPTY_TEXT_MARKERS

    from ._teams import _format_message_attachments

    return (
        text is not None
        and str(text).strip() in EMPTY_TEXT_MARKERS
        and not msg.get("deletedDateTime")
        and not _format_message_attachments(msg)
    )


def listing_note(msg: dict[str, Any], read_tool: str, text: str | None = None) -> str:
    """The suffix a listing gives a message with voice clips, ``""`` without.

    *text* is the message text the listing shows: a message that shows none
    and carries no attachment may hold a voice clip as a hosted content, so
    the listing names the call that hears it.
    """
    clips = audio_attachments(msg)
    if not clips:
        if _looks_textless(msg, text):
            return f"\n[no text: it may be a voice note - read the message with {read_tool} to hear it as text]"
        return ""
    noun = "a voice note" if len(clips) == 1 else f"{len(clips)} voice notes"
    return f"\n[{noun}: read the message with {read_tool} to hear it as text]"


def _graph_download_url(att: dict[str, Any]) -> str | None:
    """Where a clip is downloaded with the integration's token, ``None`` off Graph."""
    url = str(att.get("contentUrl") or "")
    if str(att.get("contentType") or "").lower().startswith("audio/"):
        return url if url.startswith(f"{_BASE_URL}/") else None
    if att.get("contentType") == "reference" and url.lower().startswith("https://"):
        from ._drive import _encode_share_id

        return f"{_BASE_URL}/shares/{_encode_share_id(url)}/driveItem/content"
    return None


def _db_session() -> Any:
    """A database session for the transcription's provider lookup."""
    from app.core.database import async_session

    return async_session()


async def heard_lines(ctx: MicrosoftContext, client: Any, msg: dict[str, Any]) -> list[str]:
    """One line per voice clip of *msg*: its transcript, or why it could not be heard."""
    from app.llm.provider_access import acting_user_id
    from app.services.agent_accounts import voice_notes

    clips = audio_attachments(msg)
    if not clips:
        return []
    person = acting_user_id()
    lines: list[str] = []
    for index, att in enumerate(clips):
        if index >= MAX_HEARD:
            lines.append(voice_notes.failed_line(f"not heard: at most {MAX_HEARD} voice notes per read"))
            continue
        url = _graph_download_url(att)
        if url is None:
            lines.append(voice_notes.failed_line("the audio is not served by Microsoft Graph"))
            continue
        if person is None:
            lines.append(voice_notes.failed_line("no person this run acts for to transcribe it for"))
            continue
        resp, err = await ctx.request(client, "get", url, _PROVIDER, "download voice note", follow_redirects=True)
        if err or resp is None:
            lines.append(voice_notes.failed_line("the audio could not be downloaded"))
            continue
        filename = str(att.get("name") or "").strip() or "teams-voice-note.m4a"
        async with _db_session() as db:
            lines.append(
                await voice_notes.heard_line(db, person, resp.content, filename, where="microsoft teams read tool")
            )
    return lines


async def heard_hosted_lines(
    ctx: MicrosoftContext, client: Any, message_url: str, clips: list[dict[str, Any]], *, already: int = 0
) -> list[str]:
    """One line per voice clip kept as a hosted content of the message at *message_url*.

    Each is downloaded from ``{message_url}/hostedContents/{id}/$value`` on the
    Graph host only, with the integration's token, and transcribed for the
    person the run acts for. *already* counts the clips this read heard from
    the attachments, so one read hears at most :data:`MAX_HEARD`.
    """
    from app.llm.provider_access import acting_user_id
    from app.services.agent_accounts import voice_notes

    if not clips:
        return []
    if not message_url.startswith(f"{_BASE_URL}/"):
        return [voice_notes.failed_line("the audio is not served by Microsoft Graph") for _ in clips]
    person = acting_user_id()
    lines: list[str] = []
    for index, item in enumerate(clips, start=already):
        if index >= MAX_HEARD:
            lines.append(voice_notes.failed_line(f"not heard: at most {MAX_HEARD} voice notes per read"))
            continue
        if person is None:
            lines.append(voice_notes.failed_line("no person this run acts for to transcribe it for"))
            continue
        hosted_id = _url_decode(str(item.get("id") or ""))
        url = f"{message_url}/hostedContents/{_url_encode(hosted_id, safe='')}/$value"
        resp, err = await ctx.request(client, "get", url, _PROVIDER, "download voice note", follow_redirects=True)
        if err or resp is None:
            lines.append(voice_notes.failed_line("the audio could not be downloaded"))
            continue
        async with _db_session() as db:
            lines.append(
                await voice_notes.heard_line(
                    db, person, resp.content, "teams-voice-note.m4a", where="microsoft teams read tool"
                )
            )
    return lines


def with_heard(text: str, lines: list[str]) -> str:
    """*text* followed by the voice note lines, as data."""
    return text + "".join(f"\n{line}" for line in lines)
