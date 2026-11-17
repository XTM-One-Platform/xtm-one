"""Outlook helpers shared by the mail and calendar tools: ids, search, signature, links and attachments."""

from __future__ import annotations

import base64
import hashlib
import re
from html import unescape as _html_unescape
from typing import Any
from urllib.parse import quote as _url_encode
from urllib.parse import unquote as _url_decode

from app.core.egress import egress_client
from app.integrations.file_content import (
    file_id_list,
    kilobytes,
)
from app.integrations.http import AutoRefreshClient
from app.tools.base import FileContext

from ._common import (
    _BASE_URL,
    _PROVIDER,
    MicrosoftContext,
    _is_placeholder_id,
    _resolve_platform_file,
)

# Outlook attachments: a file under 3 MB is POSTed whole; a larger one
# goes through an upload session, in chunks that stay under Graph's 4 MB
# per-request limit and are a multiple of 320 KiB (12 x 320 KiB = 3.75 MiB).
_OUTLOOK_SMALL_ATTACHMENT_BYTES = 3 * 1024 * 1024
_OUTLOOK_UPLOAD_CHUNK_BYTES = 12 * 320 * 1024

# Links kept from an email body the answer otherwise shows as plain text.
_MAX_BODY_LINKS = 20
_HREF_RE = re.compile(r"""href\s*=\s*["'](https?://[^"'\s>]+)["']""", re.IGNORECASE)


def _body_links(html: str | None) -> list[str]:
    """The http(s) link targets of an email body, in order, without duplicates.

    The body is shown with its tags stripped, which drops every anchor's
    target - among them the link of a cloud-file (reference) attachment.
    """
    links: list[str] = []
    for match in _HREF_RE.finditer(html or ""):
        url = _html_unescape(match.group(1))
        if url not in links:
            links.append(url)
        if len(links) >= _MAX_BODY_LINKS:
            break
    return links


_OUTLOOK_ATTACHMENT_KINDS = {
    "#microsoft.graph.itemAttachment": "attached Outlook item",
    "#microsoft.graph.referenceAttachment": "link to a cloud file",
}
_ATTACHMENT_FILE_IDS_PROPERTY = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "Optional file_id of each file of this conversation to attach (uploaded, "
        "generated, or saved by a tool such as download_microsoft_file or "
        "get_microsoft_attachment). Each file is attached byte-for-byte."
    ),
}

# ---------------------------------------------------------------------------
# Message-ID sanitisation and validation
# ---------------------------------------------------------------------------

# Microsoft Graph message IDs are long base64url-encoded strings (typically
# 100 + characters).  Any ID shorter than this threshold is structurally
# invalid — the most common bad shapes we have observed are:
#   • short hex strings produced by agent hallucinations (e.g. "6a478516a3959653be9e43af", 24 chars)
#   • plain English placeholder words emitted verbatim by the LLM
# Setting the floor at 50 leaves a comfortable gap below the real minimum
# (≈ 100 chars) while staying well above the longest observed bad IDs.
_MIN_MESSAGE_ID_LENGTH = 50

# Known placeholder strings that an agent emits when it never resolved a real
# Graph message ID.  Mirrors the same pattern used by _INVALID_TEAMS_ID_PLACEHOLDERS
# for Teams IDs.
_INVALID_MESSAGE_ID_PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "n/a",
        "na",
        "none",
        "null",
        "unknown",
        "undefined",
        "placeholder",
        "-",
        "id",
        "message_id",
        "email_id",
        "email-id",
        "draft_id",
        "draft-id",
        "<message_id>",
        "<email_id>",
        "[message_id]",
        "[email_id]",
    }
)


def _clean_message_id(mid: str) -> str:
    """Normalize a Microsoft Graph message ID.

    LLMs sometimes URL-encode characters in the ID (e.g. ``=`` → ``%3D``,
    ``_`` → ``%5F``) when passing it back as a tool argument.  We decode
    any percent-encoding and strip whitespace so the raw ID is used in
    Graph API URL paths.
    """
    return _url_decode(mid.strip())


def _validate_message_id(
    message_id: str,
    tool_name: str,
    *,
    field_name: str = "message_id",
    recovery_hint: str = "Use list_microsoft_emails or search_microsoft_emails to retrieve a valid email ID",
) -> str | None:
    """Validate a Microsoft Graph message ID before issuing a Graph API call.

    Returns an actionable error string when the ID is obviously invalid
    (empty, a known placeholder word, an unsubstituted template token, or
    too short to be a real Graph message ID).  Returns ``None`` when the
    value looks usable.

    ``field_name`` and ``recovery_hint`` let callers whose parameter is not
    literally ``message_id`` (e.g. ``send_microsoft_draft``'s ``draft_id``)
    keep the error message accurate so the agent self-corrects with the
    right tool instead of re-listing emails.

    This guard short-circuits the Graph round-trip that would otherwise
    fail with HTTP 400 ``ErrorInvalidIdMalformed``.
    """
    mid = message_id.strip() if isinstance(message_id, str) else ""
    if _is_placeholder_id(mid, _INVALID_MESSAGE_ID_PLACEHOLDERS):
        return (
            f"Error: {field_name} is missing or invalid ({message_id!r}). "
            f"{recovery_hint} before calling {tool_name}."
        )
    if len(mid) < _MIN_MESSAGE_ID_LENGTH:
        return (
            f"Error: {field_name} {message_id!r} is too short to be a valid "
            f"Microsoft Graph message ID (got {len(mid)} chars, expected at "
            f"least {_MIN_MESSAGE_ID_LENGTH}). "
            f"{recovery_hint} before calling {tool_name}."
        )
    return None


def _calendar_url(calendar_id: str, suffix: str) -> str:
    """Build a Graph calendar URL, optionally scoped to a specific calendar.

    When ``calendar_id`` is provided the path targets that calendar
    (``/me/calendars/{id}/{suffix}``) - this is how events from calendars
    OTHER people have shared with the user (and which the user has added to
    their Outlook) are read and written. Otherwise the user's default
    calendar is used (``/me/{suffix}``), preserving the historical behaviour.

    The calendar id is percent-encoded because Graph calendar ids are
    base64-ish and can contain ``/`` ``+`` ``=`` which would otherwise break
    the URL path.
    """
    cid = (calendar_id or "").strip()
    if cid:
        return f"{_BASE_URL}/me/calendars/{_url_encode(cid, safe='')}/{suffix}"
    return f"{_BASE_URL}/me/{suffix}"

# Microsoft Graph ``$search`` on ``/me/messages`` requires the *entire* search
# value to be wrapped in double quotes — including AQS/KQL field restrictions
# (``from:``, ``to:``, ``subject:`` …) and boolean operators (AND/OR/NOT). An
# unquoted ``from:user@domain.com`` is rejected with HTTP 400
# ("Syntax error: character ':' is not valid …"), The KQL inside
# the quotes is still interpreted by Graph, so a quoted ``"from:x AND
# subject:y"`` keeps its field-restriction + boolean semantics — only the
# *outer* quoting (which Graph mandates) was missing. ``_build_email_search_query``
# therefore always emits a quoted, escaped value.


def _is_fully_quoted_search_value(raw: str) -> bool:
    """True if ``raw`` is already a single, fully double-quoted Graph ``$search``
    value.

    A value qualifies when it opens with ``"``, its final character is an
    *unescaped* ``"``, and it contains no *unescaped* ``"`` in between
    (escaped interior quotes/backslashes are allowed). This recognises both
    the documented Graph form (``"from:alice@example.com"``) and this helper's
    own escaped output (``"subject:\\"A & B\\""``), so re-running the builder on
    such a value returns it unchanged instead of nesting another quote level —
    which would make Graph treat the whole thing as a literal phrase and drop
    any field restriction.
    """
    if len(raw) < 2 or not raw.startswith('"'):
        return False
    i = 1
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "\\":
            i += 2  # skip the escaped character
            continue
        if ch == '"':
            # First unescaped quote since the opener — balanced only if it is
            # the final character (otherwise the value continues past it).
            return i == n - 1
        i += 1
    # No unescaped closing quote (e.g. the trailing ``"`` was escaped) → the
    # string isn't a balanced quoted value.
    return False


def _build_email_search_query(query: str) -> str:
    """Build a Graph ``$search`` value for Outlook ``/me/messages``.

    Microsoft Graph requires the whole ``$search`` value to be a
    double-quoted string. We therefore *always* wrap the query in outer
    quotes (escaping any embedded ``\\`` / ``"``) — for plain text **and**
    for AQS/KQL expressions. Inside those outer quotes Graph still
    interprets KQL, so field restrictions and boolean operators keep
    working:

    * ``pizza``                       → ``"pizza"``
    * ``from:alice@example.com``      → ``"from:alice@example.com"``
    * ``from:alice AND subject:plan`` → ``"from:alice AND subject:plan"``
    * ``subject:"A & B"``             → ``"subject:\\"A & B\\""``

    Forwarding an AQS expression *unquoted* (the previous behaviour) made
    Graph reject ``from:user@domain.com`` with HTTP 400.

    The wrapping is **idempotent**: if the caller already passed a fully
    double-quoted value — the documented Graph form, e.g.
    ``"from:alice@example.com"``, or this helper's own escaped output, e.g.
    ``"subject:\\"A & B\\""`` — it is returned unchanged. Otherwise the extra
    quoting level would nest the quotes and make Graph treat the value as a
    literal phrase, silently dropping the field restriction.
    """
    raw = query.strip()
    if not raw:
        return '""'
    # Already a single fully double-quoted string (escaped interior quotes
    # allowed) → the caller followed the Graph $search form (or this is our own
    # output), so don't double-wrap it.
    if _is_fully_quoted_search_value(raw):
        return raw
    escaped = raw.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'

# ---------------------------------------------------------------------------
# Signature extraction helper
# ---------------------------------------------------------------------------


def _extract_signature_block(html: str) -> str:
    """Extract the Outlook email signature block from an HTML email body.

    Microsoft Graph API has no endpoint to retrieve a user's email
    signature, so we parse it from sent-mail HTML.  Outlook marks
    the signature with ``<div id="Signature">`` (or ``x_Signature``
    in forwarded / quoted contexts).
    """
    sig_start = re.search(
        r'<div[^>]*\bid\s*=\s*["\'](?:x_)?[Ss]ignature["\'][^>]*>',
        html,
    )
    if not sig_start:
        return ""

    remaining = html[sig_start.start() :]

    # Markers that follow the signature in Outlook reply HTML.
    # ``</body>`` is included as a safety stop so that non-reply sent
    # emails (where no ``<hr>`` or ``divRplyFwdMsg`` follows) don't
    # capture stray closing tags that would break draft injection.
    # A quote written as text (a reply sent with a flattened quote) is
    # a stop too: Outlook's underscore separator followed by a From field and a
    # Sent / To / Subject one. Without it the quote of another thread was read
    # as part of the signature and carried into every later reply. An
    # underscore rule alone (a divider), or From / To text without the
    # separator (a signature's own lines), is no stop.
    end_patterns = [
        r'<div[^>]*\bid\s*=\s*["\'](?:x_)?divRplyFwdMsg["\']',
        r'<div[^>]*\bid\s*=\s*["\'](?:x_)?appendonsend["\']',
        r'<hr[^>]*style\s*=\s*["\'][^"\']*display\s*:\s*inline-block',
        r'<hr[^>]*tabindex\s*=\s*["\']-1["\']',
        r"<blockquote\b",
        r"_{16,}(?:<[^>]*>|\s)*(?<!\w)(?:From|De|Von)\s*:(?:<[^>]*>|[^<\n]){1,300}?(?:<[^>]*>|\s)+"
        r"(?:<[^>]*>)*\s*(?:Sent|Date|Envoyé|Gesendet|Enviado|To|À|An|Para|Subject|Objet|Betreff|Asunto)\s*:",
        r"</body\s*>",
    ]

    end_pos = len(remaining)
    for pattern in end_patterns:
        m = re.search(pattern, remaining, re.IGNORECASE)
        if m and m.start() > 0:
            end_pos = min(end_pos, m.start())

    sig_html = remaining[:end_pos].strip()

    # Must contain some meaningful text
    text_content = re.sub(r"<[^>]+>", "", sig_html).strip()
    if len(text_content) < 3:
        return ""

    return sig_html


_CID_IMAGE = re.compile(r"<img\b[^>]*\bsrc\s*=\s*([\"'])cid:[^\"']*\1[^>]*>", re.IGNORECASE)


def sent_mail_signature(html: str) -> str:
    """The signature block of a sent email, as a new message may reuse it.

    Its ``cid:`` images point at attachments of that sent email alone: in
    another message they are broken placeholders, so they are left out. Its
    ``https`` images stay (the send paths attach them inline once).
    """
    block = _extract_signature_block(html)
    return _CID_IMAGE.sub("", block) if block else ""


_SIGNATURE_DIV_ID = re.compile(r"^(?:x_)?signature$", re.IGNORECASE)


def signature_block(html: str) -> str:
    """A signature service's HTML as the ``<div id="Signature">`` block Outlook writes.

    It is the block :func:`_extract_signature_block` reads back from sent
    mail, put where Outlook puts it (above the quoted thread): a signature
    add-in that manages the block, when the person opens the draft, refreshes
    it in place instead of adding a second signature. The HTML is made safe by
    ``email_signatures.signature_markup``; a signature already in such a
    block keeps one block. Empty when there is nothing to show.
    """
    from bs4 import BeautifulSoup

    from app.services.email_signatures import signature_markup

    markup = signature_markup(html)
    if not markup:
        return ""
    soup = BeautifulSoup(markup, "html.parser")
    existing = soup.find("div", id=_SIGNATURE_DIV_ID)
    if existing is not None:
        existing.unwrap()
    return f'<div id="Signature">{soup.decode().strip()}</div>'

# ── Outlook attachments (emails and calendar events) ───────────────


async def _list_outlook_attachments(
    ctx: MicrosoftContext, client: AutoRefreshClient, owner_url: str
) -> tuple[list[dict], str | None]:
    """The attachments of an email or event, without their bytes."""
    resp, err = await ctx.request(
        client,
        "get",
        f"{owner_url}/attachments",
        _PROVIDER,
        "list attachments",
        params={"$select": "id,name,contentType,size,isInline"},
    )
    if err:
        return [], err
    return resp.json().get("value", []), None


def _format_outlook_attachments(attachments: list[dict], how_to_read: str) -> str:
    lines = [f"Attachments ({len(attachments)}):"]
    for att in attachments:
        details = [att.get("contentType") or "unknown type", kilobytes(att.get("size"))]
        kind = _OUTLOOK_ATTACHMENT_KINDS.get(att.get("@odata.type") or "")
        if kind:
            details.append(kind)
        if att.get("isInline"):
            details.append("inline")
        lines.append(
            f"- {att.get('name') or 'attachment'} ({', '.join(details)}) - "
            f"attachment_id: {att.get('id', '')}"
        )
    lines.append(how_to_read)
    return "\n".join(lines)


async def _resolve_attachment_files(
    attachment_file_ids: Any, file_context: FileContext | None
) -> tuple[list[tuple[str, bytes, str, str]], str | None]:
    """Every platform file an email should carry, resolved before any write, each once.

    Each comes as ``(file_id, bytes, filename, content_type)``: the answers
    name a file by its file_id, never by its stored name, a third party's
    text. A file named twice, or under two ids (a file id and its revision,
    its name), is the same name and the same bytes: it is attached once.
    """
    files: list[tuple[str, bytes, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for file_id in file_id_list(attachment_file_ids):
        resolved, err = await _resolve_platform_file(file_id, file_context)
        if err:
            return [], err
        data, name, _mime = resolved
        key = (name, hashlib.sha256(data).hexdigest())
        if key in seen:
            continue
        seen.add(key)
        files.append((file_id, *resolved))
    return files, None


async def settle_message_attachments(ctx: MicrosoftContext, client: AutoRefreshClient, message_url: str) -> str:
    """Read a draft's attachments back, remove a second copy of a file, and say what it carries.

    A file attachment with the name and size of one listed before it is a
    second copy (a retried upload, a file attached twice) and is deleted;
    inline images are the signature tools' (:mod:`._signature_images`, one per
    content id). Returns the read-back sentence for the tool's answer, empty
    when the attachments could not be listed.
    """
    listed, err = await _list_outlook_attachments(ctx, client, message_url)
    if err:
        return ""
    seen: set[tuple[str, int]] = set()
    files = inline = removed = 0
    for att in listed:
        if att.get("isInline"):
            inline += 1
            continue
        key = (str(att.get("name") or ""), int(att.get("size") or 0))
        if key not in seen:
            seen.add(key)
            files += 1
            continue
        _, del_err = await ctx.request(
            client,
            "delete",
            f"{message_url}/attachments/{_url_encode(str(att.get('id') or ''), safe='')}",
            _PROVIDER,
            "remove duplicate attachment",
        )
        if del_err:
            files += 1
            continue
        removed += 1
    dropped = f", {removed} duplicate copy removed" if removed == 1 else (
        f", {removed} duplicate copies removed" if removed else ""
    )
    return (
        f" Read back: {files} file attachment(s), {inline} inline image(s){dropped}; "
        "the body carries no colour or font of its own (the reader's theme applies)."
    )


async def _upload_large_attachment(
    ctx: MicrosoftContext, client: AutoRefreshClient, attachments_url: str, data: bytes, name: str, mime: str
) -> str | None:
    """Attach a file of 3 MB or more through a Graph upload session."""
    resp, err = await ctx.request(
        client,
        "post",
        f"{attachments_url}/createUploadSession",
        _PROVIDER,
        "start attachment upload",
        json={
            "AttachmentItem": {
                "attachmentType": "file",
                "name": name,
                "size": len(data),
                "contentType": mime,
            }
        },
    )
    if err:
        return err
    upload_url = (resp.json() or {}).get("uploadUrl") or ""
    if not upload_url.lower().startswith("https://"):
        return "Error: Microsoft Graph returned no usable upload URL for the attachment."
    # The upload URL is pre-authenticated: Graph refuses a bearer token on it.
    async with egress_client(timeout=120) as raw_client:
        total = len(data)
        for start in range(0, total, _OUTLOOK_UPLOAD_CHUNK_BYTES):
            chunk = data[start : start + _OUTLOOK_UPLOAD_CHUNK_BYTES]
            _, err = await ctx.request(
                raw_client,
                "put",
                upload_url,
                _PROVIDER,
                "upload attachment",
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{total}",
                },
                content=chunk,
            )
            if err:
                return err
    return None


async def _attach_files(
    ctx: MicrosoftContext,
    client: AutoRefreshClient,
    owner_url: str,
    files: list[tuple[str, bytes, str, str]],
) -> tuple[list[str], str | None]:
    """Attach *files* byte-for-byte to an Outlook message or event.

    Returns what the answer says about each file attached (its file_id and
    size), and the error that stopped the next one.
    """
    attachments_url = f"{owner_url}/attachments"
    attached: list[str] = []
    for file_id, data, name, mime in files:
        mime = mime or "application/octet-stream"
        if len(data) < _OUTLOOK_SMALL_ATTACHMENT_BYTES:
            _, err = await ctx.request(
                client,
                "post",
                attachments_url,
                _PROVIDER,
                "attach file",
                json={
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": name,
                    "contentType": mime,
                    "contentBytes": base64.b64encode(data).decode("ascii"),
                },
            )
        else:
            err = await _upload_large_attachment(ctx, client, attachments_url, data, name, mime)
        if err:
            return attached, err
        attached.append(f"file {file_id} ({kilobytes(len(data))})")
    return attached, None
