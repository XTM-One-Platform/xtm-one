# SPDX-License-Identifier: Apache-2.0

"""Google integration – Gmail, Calendar, Drive, Docs, Sheets, Chat, Tasks.

The Google Docs review tools (comment threads, anchored comments, suggestions,
edits) live in ``google_docs.py``, the Google Sheets tools (read, append,
update, clear, delete rows) in ``google_sheets.py``; both are registered here.
"""

import base64
import json
import mimetypes
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from email import encoders, message_from_bytes
from email import policy as email_policy
from email.message import EmailMessage, Message
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape as html_escape
from typing import Any
from urllib.parse import quote as _url_encode
from urllib.parse import urlparse

import httpx

from app.core.egress import egress_client
from app.core.utils import format_exception_message
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.file_content import (
    DOCUMENT_EXTENSIONS,
    DOCUMENT_MIME_EXTENSIONS,
    MAX_DOCUMENT_BYTES,
    MAX_TEXT_CHARS,
    cut_inline,
    file_id_list,
    keep_file,
    kept_file_line,
    kilobytes,
    readable_text,
    rest_of_file_hint,
)
from app.integrations.file_delivery import untrusted_file_facts, uploaded_file_answer
from app.integrations.http import AutoRefreshClient, format_entity_list, safe_request
from app.integrations.providers.google_docs import (
    DOCS_SCOPE,
    GOOGLE_DOC_MIME,
    GoogleDocTools,
    bool_arg,
    export_note,
)
from app.integrations.providers.google_sheets import SHEETS_SCOPE, GoogleSheetTools
from app.services.branding import get_cached_branding
from app.services.email_html import markdown_to_email_html
from app.services.email_signatures import (
    MailSigner,
    append_to_body,
    register_email_sender,
    signed_text,
    text_to_html,
)
from app.tools.base import (
    CallableToolExecutor,
    FileContext,
    SendGuard,
    ToolDefinition,
    ToolResult,
    WriteCoalescing,
    WriteReadBack,
)

# Gmail tools send email: each integration has an Email signature setting.
register_email_sender("google")

_PROVIDER = "Google"
_GMAIL_BASE_URL = "https://gmail.googleapis.com/gmail/v1"
GMAIL_API = _GMAIL_BASE_URL
_DRIVE_BASE_URL = "https://www.googleapis.com/drive/v3"
DRIVE_API = _DRIVE_BASE_URL
_GMAIL_UPLOAD_URL = "https://gmail.googleapis.com/upload/gmail/v1"
_DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3"
_CHAT_API = "https://chat.googleapis.com/v1"
_CHAT_UPLOAD_URL = "https://chat.googleapis.com/upload/v1"

# Files: the bytes a download brings into the conversation or an upload
# sends to Drive / Chat (the Microsoft file tools use the same cap), and the
# attachments Gmail accepts on one message.
_GOOGLE_MAX_FILE_BYTES = 100 * 1024 * 1024
_GMAIL_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
# Drive takes a multipart upload for small files only; above this a file goes
# through a resumable upload session.
_DRIVE_MULTIPART_MAX_BYTES = 5 * 1024 * 1024
# Calendar event attachments are Drive files: a copy of each file in
# the connected account's Drive, shared read-only with the guests. An event
# holds at most 25 attachments.
_CALENDAR_MAX_ATTACHMENTS = 25
_DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
_ATTACHMENTS_FOLDER_PROPERTY = "calendarAttachments"
_ATTACHMENT_COPY_PROPERTY = "calendarAttachmentCopy"
_SEND_UPDATES = {
    "all": "sent to every guest",
    "externalOnly": "sent to the guests outside Google Calendar only",
    "none": "not sent",
}

# The Office twin a Google Workspace file is downloaded as by default.
_WORKSPACE_DEFAULT_EXPORT = {
    "application/vnd.google-apps.document": "docx",
    "application/vnd.google-apps.spreadsheet": "xlsx",
    "application/vnd.google-apps.presentation": "pptx",
    "application/vnd.google-apps.drawing": "png",
}
# Export formats a caller can name: the MIME types Drive may use for it, the
# file extension. The file's own ``exportLinks`` decide which one is offered.
_EXPORT_FORMATS: dict[str, tuple[tuple[str, ...], str]] = {
    "pdf": (("application/pdf",), ".pdf"),
    "docx": (("application/vnd.openxmlformats-officedocument.wordprocessingml.document",), ".docx"),
    "xlsx": (("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",), ".xlsx"),
    "pptx": (("application/vnd.openxmlformats-officedocument.presentationml.presentation",), ".pptx"),
    "odt": (("application/vnd.oasis.opendocument.text",), ".odt"),
    "ods": (
        ("application/x-vnd.oasis.opendocument.spreadsheet", "application/vnd.oasis.opendocument.spreadsheet"),
        ".ods",
    ),
    "odp": (("application/vnd.oasis.opendocument.presentation",), ".odp"),
    "rtf": (("application/rtf",), ".rtf"),
    "txt": (("text/plain",), ".txt"),
    "csv": (("text/csv",), ".csv"),
    "tsv": (("text/tab-separated-values",), ".tsv"),
    "md": (("text/markdown", "text/x-markdown"), ".md"),
    "epub": (("application/epub+zip",), ".epub"),
    "png": (("image/png",), ".png"),
    "jpg": (("image/jpeg",), ".jpg"),
    "svg": (("image/svg+xml",), ".svg"),
}
_MIME_TYPE_RE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
# A Chat attachment's ``attachmentDataRef.resourceName``, used as a URL path.
_CHAT_RESOURCE_RE = re.compile(r"^[A-Za-z0-9._~=+-]+(?:/[A-Za-z0-9._~=+-]+)*$")
_ATTACHMENT_FILE_IDS_PROPERTY = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "Optional file_id of each file of this conversation to attach (uploaded, "
        "generated, or saved by a tool such as download_google_drive_file or "
        "get_gmail_attachment). Each file is attached byte-for-byte."
    ),
}
_EVENT_ATTACHMENT_FILE_IDS_PROPERTY = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "Optional file_id of each file of this conversation to attach (uploaded, "
        "generated, or saved by a tool such as download_google_drive_file). Each file "
        "is copied to a dedicated folder of the connected account's Google Drive and "
        "shared read-only with the attendees; at most "
        f"{_CALENDAR_MAX_ATTACHMENTS} attachments per event."
    ),
}
_SEND_UPDATES_PROPERTY = {
    "type": "string",
    "enum": list(_SEND_UPDATES),
    "default": "all",
    "description": (
        "Who Google Calendar notifies: 'all' guests (default), 'externalOnly' "
        "(guests outside Google Calendar), or 'none'"
    ),
}
_ALL_DAY_PROPERTY = {
    "type": "boolean",
    "description": (
        "true for an all-day event: start_time and end_time are then dates "
        "(YYYY-MM-DD), end_time being the LAST day of the event, included (a one-day "
        "event has end_time = start_time; Google's exclusive end date is computed). "
        "Two bare dates make an all-day event without it; false takes date-times."
    ),
}
_DATES_MESSAGE = (
    "an all-day event takes start_time and end_time as dates, YYYY-MM-DD "
    "(end_time is its last day, included)"
)


def _bare_date(value: object) -> date | None:
    """The date of a date-only ISO value (``2026-11-04``); None when it carries a time."""
    text = value.strip() if isinstance(value, str) else ""
    if len(text) != 10:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _flag(value: object) -> bool | None:
    """An optional boolean argument, its JSON string form included."""
    if isinstance(value, str):
        lowered = value.strip().lower()
        return {"true": True, "false": False}.get(lowered)
    return value if isinstance(value, bool) else None


def _all_day_span(start_time: str, end_time: str, all_day: bool | None) -> tuple[tuple[date, date] | None, str | None]:
    """``(first day, exclusive end day)`` of an all-day event; None for a timed one.

    ``all_day=True`` takes two dates, ``end_time`` being the last day included:
    Google Calendar stores the day after it (``end.date`` is exclusive).
    Omitted, two bare dates make the event all-day. ``all_day=False`` takes
    date-times.
    """
    first, last = _bare_date(start_time), _bare_date(end_time)
    dated = first is not None or last is not None
    if all_day is False:
        if dated:
            return None, (
                "Error: all_day=false takes start_time and end_time as ISO 8601 "
                "date-times (e.g. '2026-11-04T09:00:00'), not dates."
            )
        return None, None
    if all_day is None and not dated:
        return None, None
    if first is None or last is None:
        return None, f"Error: {_DATES_MESSAGE}."
    if last < first:
        return None, "Error: end_time (the last day of the all-day event) is before start_time."
    return (first, last + timedelta(days=1)), None


def _last_day(end_date: str, first_day: str) -> str:
    """The last day an all-day event covers, from Google's exclusive ``end.date``."""
    try:
        last = date.fromisoformat(end_date) - timedelta(days=1)
    except (TypeError, ValueError):
        return end_date or first_day
    return max(last.isoformat(), first_day)


def _event_span(event: dict) -> tuple[str, str, bool]:
    """``(start, end, all_day)`` of a Calendar event: an all-day event's dates, its last day included."""
    start = event.get("start") or {}
    end = event.get("end") or {}
    if start.get("date") and not start.get("dateTime"):
        return start["date"], _last_day(end.get("date") or "", start["date"]), True
    return (
        start.get("dateTime") or start.get("date") or "N/A",
        end.get("dateTime") or end.get("date") or "N/A",
        False,
    )


def _window_bound(value: str, *, end: bool) -> datetime:
    """A ``read_google_calendar`` bound: a date covers its whole day (UTC); a naive time is UTC."""
    day = _bare_date(value)
    if day is not None:
        moment = datetime(day.year, day.month, day.day, tzinfo=UTC)
        return moment + timedelta(days=1) if end else moment
    moment = datetime.fromisoformat(value.strip())
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def chat_media_url(resource_name: str | None) -> str | None:
    """The ``media.download`` URL of a Chat attachment's ``attachmentDataRef.resourceName``.

    ``None`` for anything that is not a resource name, so a name cannot aim a
    token at another API path. Serves the ``?alt=media`` bytes of content
    uploaded to Chat; a Drive file is not served there (it has a
    ``driveDataRef`` instead).
    """
    resource = (resource_name or "").strip()
    if not resource or ".." in resource or not _CHAT_RESOURCE_RE.fullmatch(resource):
        return None
    return f"{_CHAT_API}/media/{resource}"


def _safe_mime(mime: str | None) -> str:
    """*mime* when it is a plain ``type/subtype``, else ``application/octet-stream``.

    It lands in a multipart header: a CR/LF or a parameter must not ride along.
    """
    value = (mime or "").split(";", 1)[0].strip()
    return value if _MIME_TYPE_RE.match(value) else "application/octet-stream"


def _drive_link_lines(meta: dict) -> list[str]:
    """The links Drive returns for a file.

    ``webViewLink`` opens it, ``webContentLink`` downloads a stored file,
    ``exportLinks`` download a Google Docs / Sheets / Slides file in each format
    it can be exported to.
    """
    lines = []
    if meta.get("webViewLink"):
        lines.append(f"Web URL: {meta['webViewLink']}")
    if meta.get("webContentLink"):
        lines.append(f"Download URL: {meta['webContentLink']}")
    export_links = meta.get("exportLinks") or {}
    if isinstance(export_links, dict) and export_links:
        lines.append("Export URLs:")
        lines.extend(f"- {mime}: {url}" for mime, url in sorted(export_links.items()))
    return lines


def _not_downloadable(meta: dict) -> str:
    """The error for a Drive item with no file to download (a folder, a form, a site)."""
    return (
        "Error: this Drive item (a folder, a form, a site or a shortcut) cannot be "
        "downloaded as a file; open it with its Web URL instead.\n"
        + untrusted_file_facts(
            source="Google Drive",
            filename=meta.get("name") or "drive-file",
            content_type=meta.get("mimeType"),
            details=_drive_link_lines(meta),
        )
    )


def _drive_download_hint(file_id: str) -> str:
    return (
        f'To work on the whole file, put it in the conversation with download_google_drive_file(file_id="{file_id}"), '
        "then read it with read_file_content or process it with code_interpreter."
    )


def _is_drive_document(name: str, mime: str) -> bool:
    return (name or "").lower().endswith(DOCUMENT_EXTENSIONS) or mime in DOCUMENT_MIME_EXTENSIONS


def _mime_filename(name: str) -> str | tuple[str, str, str]:
    """A Content-Disposition filename, RFC 2231-encoded when it is not ASCII."""
    return name if name.isascii() else ("utf-8", "", name)


def _with_attachments(message: MIMEMultipart, files: list[tuple[bytes, str, str]]) -> MIMEMultipart:
    """*message* (the text / HTML alternative) wrapped with *files* attached.

    The envelope headers (To, Subject, threading) move to the outer
    ``multipart/mixed``; each file is attached byte-for-byte, base64-encoded.
    """
    outer = MIMEMultipart("mixed")
    body_headers = {"content-type", "mime-version", "content-transfer-encoding"}
    envelope = [(key, value) for key, value in message.items() if key.lower() not in body_headers]
    for key, value in envelope:
        outer[key] = value
    for key in {key for key, _value in envelope}:
        del message[key]
    outer.attach(message)
    for data, name, mime in files:
        maintype, _, subtype = _safe_mime(mime).partition("/")
        if maintype == "message":
            # A ``message/*`` body may not be base64-encoded (RFC 2046).
            maintype, subtype = "application", "octet-stream"
        part = MIMEBase(maintype, subtype)
        part.set_payload(data)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=_mime_filename(name or "attachment"))
        outer.attach(part)
    return outer


def _gmail_signature_block(markup: str) -> str:
    """A signature as the block Gmail writes: a draft opened in Gmail shows it as its signature, once."""
    return f'<div dir="ltr" class="gmail_signature" data-smartmail="gmail_signature">{markup}</div>' if markup else ""


#: The style Gmail's composer writes on a message: what a rendered Markdown body carries.
GMAIL_COMPOSE_STYLE = "font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#000000"


def _body_markup(body: str) -> str:
    """A body written as Markdown (or plain text) rendered as the HTML part of a Gmail message.

    Its lists, emphasis, links and paragraphs show as such in Gmail and
    Outlook (a ``- item`` line is a bullet, not a hyphen); a tag the text
    mentions shows as written, escaped before the rendering, as the Outlook
    tools treat a ``text`` body. Empty when the body is.
    """
    text = (body or "").strip()
    if not text:
        return ""
    return markdown_to_email_html(html_escape(text, quote=False), assume_html=False, compose_style=GMAIL_COMPOSE_STYLE)


def _alternative(body: str, body_html: str, signature: str) -> MIMEMultipart:
    """The text / HTML alternative of a message, signed with *signature* (markup) when there is one.

    The text part is *body* as written. The HTML part is *body_html* when the
    caller gives one, else *body* rendered from Markdown (:func:`_body_markup`);
    it ends with Gmail's signature block, the text part with its text rendering.
    """
    msg = MIMEMultipart("alternative")
    html = body_html or _body_markup(body) or text_to_html(body)
    if signature:
        msg.attach(MIMEText(signed_text(body, signature), "plain"))
        msg.attach(MIMEText(append_to_body(html, _gmail_signature_block(signature)), "html"))
        return msg
    msg.attach(MIMEText(body, "plain"))
    if html:
        msg.attach(MIMEText(html, "html"))
    return msg


def _body_parts(message: Message) -> tuple[Any, Any]:
    """The text and HTML parts (``EmailMessage``) of a message's body, its attachments left out."""
    plain: Any = None
    html: Any = None
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        kind = part.get_content_type()
        if kind == "text/plain" and plain is None:
            plain = part
        elif kind == "text/html" and html is None:
            html = part
    return plain, html


def _is_gmail_signed(message: Message) -> bool:
    """Whether a message's HTML already carries Gmail's signature block."""
    _plain, html = _body_parts(message)
    return html is not None and "gmail_signature" in html.get_content()


def _sign_message(message: EmailMessage, signature: str) -> bool:
    """Sign a parsed message in place, as :func:`_alternative` does; ``False`` when it has no body to sign."""
    plain, html = _body_parts(message)
    if html is not None:
        html.set_content(append_to_body(html.get_content(), _gmail_signature_block(signature)), subtype="html")
        if plain is not None:
            plain.set_content(signed_text(plain.get_content(), signature))
        return True
    if plain is None:
        return False
    text = plain.get_content()
    plain.set_content(signed_text(text, signature))
    plain.make_alternative()
    plain.add_alternative(append_to_body(text_to_html(text), _gmail_signature_block(signature)), subtype="html")
    return True


class GoogleIntegration(BaseIntegration):
    provider_name = "google"
    display_name = "Google (Gmail, Calendar, Drive, Docs, Sheets, Chat, Tasks)"
    icon = "Mail"
    description = (
        "Connect Gmail, Google Calendar, Google Drive, Google Docs, Google Sheets, Google Chat, and Google Tasks."
    )
    auth_types = ["oauth"]

    def get_oauth_defaults(self) -> OAuthDefaults:
        return OAuthDefaults(
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",  # noqa: S106
            # OAuth token exchange endpoint URL, not a secret.
            scopes=[
                "openid",
                "email",
                "profile",
                # Gmail – modify (read + label + mark read + trash) + compose (drafts + send)
                "https://www.googleapis.com/auth/gmail.modify",
                "https://www.googleapis.com/auth/gmail.compose",
                "https://www.googleapis.com/auth/gmail.send",
                # Calendar – full access (read + write + delete)
                "https://www.googleapis.com/auth/calendar",
                # Drive – full access (read + write + delete)
                "https://www.googleapis.com/auth/drive",
                # Google Docs – suggestions, edits and comments attached to a passage
                DOCS_SCOPE,
                # Google Sheets – read, append, update, clear and delete rows
                SHEETS_SCOPE,
                # Google Chat – spaces, messages, memberships
                "https://www.googleapis.com/auth/chat.spaces",
                "https://www.googleapis.com/auth/chat.spaces.create",
                "https://www.googleapis.com/auth/chat.spaces.readonly",
                "https://www.googleapis.com/auth/chat.messages",
                "https://www.googleapis.com/auth/chat.messages.create",
                "https://www.googleapis.com/auth/chat.memberships",
                "https://www.googleapis.com/auth/chat.memberships.readonly",
                # Google Tasks – full access
                "https://www.googleapis.com/auth/tasks",
            ],
        )

    async def validate_connection(self, token: str) -> bool:
        async with egress_client(timeout=15) as client:
            resp, _err = await safe_request(
                client,
                "get",
                "https://www.googleapis.com/oauth2/v1/userinfo",
                _PROVIDER,
                "validate connection",
                headers={"Authorization": f"Bearer {token}"},
            )
            return resp is not None

    async def get_user_info(self, token: str) -> dict:
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                "https://www.googleapis.com/oauth2/v1/userinfo",
                _PROVIDER,
                "get user info",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp is None:
                return {"error": err}
            return resp.json()


# ---------------------------------------------------------------------------
# Google tools
# ---------------------------------------------------------------------------


def build_tools(
    token: str, executor: CallableToolExecutor, *, refresh_fn=None, **_kwargs
) -> list[ToolDefinition]:
    # ``**_kwargs`` absorbs request-scoped extras the dispatcher forwards to
    # every provider (e.g. ``scope_headers``); without it, any chat session that
    # populates scope_headers raised TypeError and all Gmail/Calendar tools were
    # silently dropped for that conversation.
    headers = {"Authorization": f"Bearer {token}"}

    # File persistence callback for the file tools (download_google_drive_file,
    # get_gmail_attachment, download_google_chat_attachment) - injected by
    # integration_tools when the calling context has a user / conversation /
    # run scope; None otherwise (the file is then kept in memory).
    persist_file_fn = _kwargs.get("persist_file_fn")
    # The integration's Email signature setting as resolved for this run.
    signer = MailSigner(_kwargs.get("signature_fn"), _kwargs.get("signature_plan"))
    _mailbox: dict[str, str] = {}

    def _client():
        return AutoRefreshClient(headers, refresh_fn)

    docs = GoogleDocTools(headers, _client)
    sheets = GoogleSheetTools(headers, _client)

    async def _own_gmail_address() -> str:
        """The Gmail account's address (its profile), for the signature integration's lookup."""
        if "address" not in _mailbox:
            async with _client() as client:
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{_GMAIL_BASE_URL}/users/me/profile",
                    _PROVIDER,
                    "read the mailbox address",
                    headers=headers,
                )
            profile = resp.json() if not err and resp is not None else {}
            _mailbox["address"] = str(profile.get("emailAddress") or "").strip()
        return _mailbox["address"]

    async def _gmail_signature(*, reply: bool) -> str:
        """The person's signature (its reply variant for a reply) as markup; empty without one."""
        if not signer.signs:
            return ""
        return await signer.markup(await _own_gmail_address(), reply=reply)

    # ── File helpers ───────────────────────────────────────────

    def _b64url_decode(data: str) -> bytes:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))

    async def _resolve_platform_files(
        file_ids: Any, file_context: FileContext | None, *, max_total: int
    ) -> tuple[list[tuple[bytes, str, str]], str | None]:
        """The conversation files named by *file_ids*, resolved before any write."""
        ids = file_id_list(file_ids)
        if not ids:
            return [], None
        if file_context is None:
            return [], (
                "Error: no file context available — files can only be "
                "forwarded from a conversation or assignment run."
            )
        files: list[tuple[bytes, str, str]] = []
        total = 0
        for file_id in ids:
            try:
                resolved = await file_context.resolve_upload(file_id)
            except Exception as exc:
                return [], f"Error: file '{file_id}' could not be read ({format_exception_message(exc)})."
            if resolved is None:
                return [], (
                    f"Error: file '{file_id}' was not found in this conversation. "
                    "Use a file_id from an attached or generated file."
                )
            total += len(resolved[0])
            if total > max_total:
                return [], (
                    f"Error: the file(s) add up to {round(total / 1024 / 1024, 1)} MB, over "
                    f"the {max_total // 1024 // 1024} MB limit."
                )
            files.append(resolved)
        return files, None

    async def _multipart_upload(
        client: AutoRefreshClient,
        url: str,
        metadata: dict,
        data: bytes,
        mime: str,
        action: str,
        *,
        method: str = "post",
    ) -> tuple[httpx.Response | None, str | None]:
        """A Google ``uploadType=multipart`` request: JSON metadata + the media."""
        boundary = f"xtm-{uuid.uuid4().hex}"
        body = b"".join(
            [
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode(),
                json.dumps(metadata).encode(),
                f"\r\n--{boundary}\r\nContent-Type: {_safe_mime(mime)}\r\n\r\n".encode(),
                data,
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )
        return await safe_request(
            client,
            method,
            url,
            _PROVIDER,
            action,
            headers={**headers, "Content-Type": f"multipart/related; boundary={boundary}"},
            content=body,
        )

    async def _resumable_upload(
        client: AutoRefreshClient,
        url: str,
        metadata: dict,
        data: bytes,
        mime: str,
        action: str,
    ) -> tuple[httpx.Response | None, str | None]:
        """A Google ``uploadType=resumable`` upload: open the session, send the bytes."""
        media_type = _safe_mime(mime)
        init, err = await safe_request(
            client,
            "post",
            url,
            _PROVIDER,
            action,
            headers={
                **headers,
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": media_type,
                "X-Upload-Content-Length": str(len(data)),
            },
            json=metadata,
        )
        if err:
            return None, err
        session = init.headers.get("location") or ""
        host = urlparse(session).hostname or ""
        # The session request carries the bearer token: only to Google APIs.
        if not session.lower().startswith("https://") or not host.endswith(".googleapis.com"):
            return None, "Error: Google returned no usable upload session; nothing was uploaded."
        return await safe_request(
            client,
            "put",
            session,
            _PROVIDER,
            action,
            headers={**headers, "Content-Type": media_type},
            content=data,
        )

    def _forwarded_names(file_ids: Any, files: list[tuple[bytes, str, str]]) -> list[str]:
        """The stored name of each file of the run *file_ids* named, one quarantine block each.

        A stored name is a third party's text: the answer's own lines name the
        file by its file_id, and the name reaches the model only here.
        """
        return [
            untrusted_file_facts(source=_PROVIDER, filename=name, details=[f"file_id: {fid}"])
            for fid, (_data, name, _mime) in zip(file_id_list(file_ids), files, strict=True)
        ]

    def _with_attached(text: str, file_ids: Any, files: list[tuple[bytes, str, str]]) -> str:
        """*text*, the files attached by file_id, then their stored names (:func:`_forwarded_names`)."""
        listed = ", ".join(
            f"file {fid} ({kilobytes(len(data))})"
            for fid, (data, _name, _mime) in zip(file_id_list(file_ids), files, strict=True)
        )
        return "\n".join([f"{text} Attachments: {listed}.", *_forwarded_names(file_ids, files)])

    async def _upload_to_drive(
        client: AutoRefreshClient,
        data: bytes,
        name: str,
        mime: str,
        parent_id: str = "",
        app_properties: dict[str, str] | None = None,
    ) -> tuple[dict | None, str | None]:
        """Upload *data* to Drive (multipart, or resumable when large)."""
        metadata: dict[str, Any] = {"name": name}
        if parent_id:
            metadata["parents"] = [parent_id]
        if app_properties:
            metadata["appProperties"] = app_properties
        upload = (
            _resumable_upload if len(data) > _DRIVE_MULTIPART_MAX_BYTES else _multipart_upload
        )
        upload_type = "resumable" if upload is _resumable_upload else "multipart"
        resp, err = await upload(
            client,
            f"{_DRIVE_UPLOAD_URL}/files?uploadType={upload_type}&supportsAllDrives=true"
            "&fields=id,name,mimeType,size,webViewLink,webContentLink,owners(emailAddress)",
            metadata,
            data,
            mime,
            "upload file to Drive",
        )
        if err:
            return None, err
        return resp.json(), None

    # ── Calendar attachments ───────────────────────────────────

    async def _calendar_attachments_folder(
        client: AutoRefreshClient,
    ) -> tuple[dict | None, str | None]:
        """The connected account's Drive folder holding the copies attached to events.

        Found by the app property it was created with, not by its name: the
        person may rename or move it, and a folder of theirs that happens to
        carry the same name is not one this integration may write into.
        """
        resp, err = await safe_request(
            client,
            "get",
            f"{_DRIVE_BASE_URL}/files",
            _PROVIDER,
            "find the calendar attachments folder",
            headers=headers,
            params={
                "q": (
                    f"appProperties has {{ key='{_ATTACHMENTS_FOLDER_PROPERTY}' and value='true' }} "
                    f"and mimeType = '{_DRIVE_FOLDER_MIME}' and trashed = false"
                ),
                "fields": "files(id,name,webViewLink)",
                "pageSize": "1",
            },
        )
        if err:
            return None, err
        found = (resp.json() or {}).get("files") or []
        if found:
            return found[0], None
        resp, err = await safe_request(
            client,
            "post",
            f"{_DRIVE_BASE_URL}/files",
            _PROVIDER,
            "create the calendar attachments folder",
            headers={**headers, "Content-Type": "application/json"},
            params={"fields": "id,name,webViewLink"},
            json={
                "name": f"{get_cached_branding().name} calendar attachments",
                "mimeType": _DRIVE_FOLDER_MIME,
                "parents": ["root"],
                "appProperties": {_ATTACHMENTS_FOLDER_PROPERTY: "true"},
            },
        )
        if err:
            return None, err
        return resp.json(), None

    async def _share_read_only(
        client: AutoRefreshClient, attachments: list[dict], emails: list[str]
    ) -> dict[str, tuple[str, list[str]]]:
        """Give each guest read access to each copy.

        Returns, for each guest Drive refused, the first reason and the files
        concerned. One request at a time: the client refreshes an expired
        token once, and concurrent requests would each meet the expiry while
        only one of them retries.
        """
        refused: dict[str, tuple[str, list[str]]] = {}
        for attachment in attachments:
            for email in emails:
                _, err = await safe_request(
                    client,
                    "post",
                    f"{_DRIVE_BASE_URL}/files/{_url_encode(attachment['fileId'], safe='')}/permissions",
                    _PROVIDER,
                    f"share an event attachment with {email}",
                    headers={**headers, "Content-Type": "application/json"},
                    # The event invitation is what tells the guests; Drive's own
                    # share email would be a second message about the same file.
                    params={"sendNotificationEmail": "false", "supportsAllDrives": "true", "fields": "id"},
                    json={"role": "reader", "type": "user", "emailAddress": email},
                )
                if err:
                    # Named by its Drive id: the title is a stored file name.
                    refused.setdefault(email, (err, []))[1].append(f"Drive file {attachment['fileId']}")
        return refused

    async def _discard_copies(client: AutoRefreshClient, attachments: list[dict]) -> str:
        """Delete the Drive copies of a calendar write that did not happen.

        Deleting a copy also ends every share of it: no guest keeps access to
        a file no event links.
        """
        left: list[str] = []
        for attachment in attachments:
            _, err = await safe_request(
                client,
                "delete",
                f"{_DRIVE_BASE_URL}/files/{_url_encode(attachment['fileId'], safe='')}",
                _PROVIDER,
                "delete an unused event attachment copy",
                headers=headers,
                params={"supportsAllDrives": "true"},
            )
            if err:
                left.append(f"Drive file {attachment['fileId']} ({attachment['fileUrl']})")
        if not left:
            return "Its Drive copies were deleted, so no guest keeps access to them."
        return (
            "These Drive copies could not be deleted and stay shared with the guests; "
            f"delete them by hand: {', '.join(left)}."
        )

    async def _copy_for_event(
        client: AutoRefreshClient,
        files: list[tuple[bytes, str, str]],
        guests: list[str],
        file_ids: Any,
    ) -> tuple[list[dict], list[str], list[str], str | None]:
        """Copy *files* to the attachments folder and share them with *guests*.

        Returns the event's ``attachments`` entries, the lines the answer says
        about them (naming each file by its file_id), the quarantine blocks of
        their stored names that close the answer (:func:`_forwarded_names`),
        or the error that stopped the copy (nothing was written to the
        calendar then, and no copy is left behind).
        """
        folder, err = await _calendar_attachments_folder(client)
        if err:
            return [], [], [], f"{err}\nNothing was written to the calendar."
        attachments: list[dict] = []
        names: list[str] = []
        owners: set[str] = set()
        for fid, (data, name, mime) in zip(file_id_list(file_ids), files, strict=True):
            item, err = await _upload_to_drive(
                client, data, name, mime, folder.get("id", ""), {_ATTACHMENT_COPY_PROPERTY: "true"}
            )
            if err:
                discarded = f" {await _discard_copies(client, attachments)}" if attachments else ""
                return [], [], [], f"{err}\nNothing was written to the calendar.{discarded}"
            owners.update(
                (owner.get("emailAddress") or "").lower()
                for owner in item.get("owners") or []
                if isinstance(owner, dict)
            )
            attachments.append(
                {
                    "fileUrl": item.get("webViewLink")
                    or f"https://drive.google.com/open?id={item.get('id', '')}",
                    "fileId": item.get("id", ""),
                    "title": item.get("name") or name,
                    "mimeType": item.get("mimeType") or _safe_mime(mime),
                }
            )
            names.append(f"file {fid} ({kilobytes(len(data))}) as Drive file {item.get('id', '')}")
        # The connected account owns the copies: it is never shared with.
        seen = set(owners)
        shared_with: list[str] = []
        for email in guests:
            if email and email.lower() not in seen:
                seen.add(email.lower())
                shared_with.append(email)
        refused = await _share_read_only(client, attachments, shared_with)
        where = f"your Google Drive folder '{folder.get('name', '')}'"
        if folder.get("webViewLink"):
            where += f" ({folder['webViewLink']})"
        readers = [email for email in shared_with if email not in refused]
        line = f"Attachments: {', '.join(names)} - copied to {where}"
        line += f" and shared read-only with {', '.join(readers)}." if readers else "."
        lines = [line]
        if refused:
            lines.append(_refusal_line(refused))
        return attachments, lines, _forwarded_names(file_ids, files), None

    def _refusal_line(refused: dict[str, tuple[str, list[str]]]) -> str:
        return (
            "Drive refused to share "
            + "; ".join(
                f"{', '.join(titles)} with {email} ({reason})"
                for email, (reason, titles) in refused.items()
            )
            + " - these guests cannot open those attachments."
        )

    def _guest_emails(attendees: Any) -> list[str]:
        """The guests of an event's attendee list: not the account itself, not a room."""
        return [
            a["email"]
            for a in attendees or []
            if isinstance(a, dict) and a.get("email") and not a.get("self") and not a.get("resource")
        ]

    async def _event_copies(
        client: AutoRefreshClient, attachments: list[dict]
    ) -> tuple[list[dict], list[str]]:
        """The event's attachments that are this integration's Drive copies, with their shares.

        Returns the copies (each attachment entry with the copy's
        ``permissions``) and the attachments that could not be read to tell,
        with why: the caller then changes nothing, or a copy could stay shared
        with a guest the event no longer has. An attachment someone added from
        their own Drive is not a copy and is left alone.
        """
        copies: list[dict] = []
        unchecked: list[str] = []
        for attachment in attachments:
            file_id = attachment.get("fileId")
            if not file_id:
                continue
            resp, err = await safe_request(
                client,
                "get",
                f"{_DRIVE_BASE_URL}/files/{_url_encode(file_id, safe='')}",
                _PROVIDER,
                "read an event attachment",
                headers=headers,
                params={
                    "fields": "id,appProperties,permissions(id,emailAddress,role,type)",
                    "supportsAllDrives": "true",
                },
                expected_statuses={404},
            )
            if err:
                if resp is None or resp.status_code != 404:
                    unchecked.append(f"{attachment.get('title') or file_id} ({err})")
                continue
            meta = resp.json() or {}
            if (meta.get("appProperties") or {}).get(_ATTACHMENT_COPY_PROPERTY) == "true":
                copies.append(
                    {
                        "fileId": file_id,
                        "title": attachment.get("title") or file_id,
                        "fileUrl": attachment.get("fileUrl") or "",
                        "permissions": meta.get("permissions") or [],
                    }
                )
        return copies, unchecked

    async def _follow_guests(
        client: AutoRefreshClient,
        copies: list[dict],
        before: list[str],
        after: list[str],
        *,
        series: tuple[set[str], set[str]] | None = None,
    ) -> list[str]:
        """Keep the event's copies readable by its guests, and by its guests only.

        A guest added gets read access; a guest removed loses the read access
        the copy gave them. A share the person made by hand to someone who was
        never a guest of the event is left alone. For one occurrence of a
        recurring event, *series* is the series' copy ids and guests: a copy
        the series links stays readable by a removed guest the series still
        invites.
        """
        was = {email.lower() for email in before}
        now = {email.lower() for email in after}
        added = [email for email in after if email.lower() not in was]
        removed = was - now
        series_copies, series_guests = series or (set(), set())
        lines: list[str] = []
        kept_by_series: set[str] = set()
        if added:
            refused = await _share_read_only(client, copies, added)
            readers = [email for email in added if email not in refused]
            if readers:
                lines.append(
                    f"The event's attachment copies are now shared read-only with {', '.join(readers)}."
                )
            if refused:
                lines.append(_refusal_line(refused))
        revoked: set[str] = set()
        kept: set[str] = set()
        for copy in copies:
            for permission in copy["permissions"]:
                email = (permission.get("emailAddress") or "").lower()
                if permission.get("role") != "reader" or permission.get("type") != "user" or email not in removed:
                    continue
                if copy["fileId"] in series_copies and email in series_guests:
                    kept_by_series.add(email)
                    continue
                _, err = await safe_request(
                    client,
                    "delete",
                    f"{_DRIVE_BASE_URL}/files/{_url_encode(copy['fileId'], safe='')}"
                    f"/permissions/{_url_encode(permission.get('id') or '', safe='')}",
                    _PROVIDER,
                    "stop sharing an event attachment copy",
                    headers=headers,
                    params={"supportsAllDrives": "true"},
                )
                (kept if err else revoked).add(email)
        if revoked - kept:
            lines.append(
                "The removed guests no longer read the event's attachment copies: "
                f"{', '.join(sorted(revoked - kept))}."
            )
        if kept:
            lines.append(
                f"Drive refused to stop sharing the attachment copies with {', '.join(sorted(kept))}: "
                "remove their access by hand."
            )
        if kept_by_series:
            lines.append(
                f"{', '.join(sorted(kept_by_series))} keep read access to the recurring event's "
                "attachment copies: the series still invites them."
            )
        return lines

    async def _series_of(
        client: AutoRefreshClient, calendar_id: str, event: dict
    ) -> tuple[tuple[set[str], set[str]] | None, str | None]:
        """For one occurrence of a recurring event: the series' copy ids and guests.

        ``(None, None)`` for any other event; the error when the series cannot
        be read, so the caller changes nothing rather than delete or unshare a
        copy the series still links.
        """
        series_id = event.get("recurringEventId")
        if not series_id:
            return None, None
        resp, err = await safe_request(
            client,
            "get",
            f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/"
            f"{_url_encode(str(series_id), safe='')}",
            _PROVIDER,
            "get the recurring event of this occurrence",
            headers=headers,
        )
        if err:
            return None, err
        series = resp.json() or {}
        file_ids = {
            str(a.get("fileId"))
            for a in series.get("attachments") or []
            if isinstance(a, dict) and a.get("fileId")
        }
        return (file_ids, {email.lower() for email in _guest_emails(series.get("attendees"))}), None

    def _send_updates_error(send_updates: str) -> str | None:
        if send_updates in _SEND_UPDATES:
            return None
        return f"Error: send_updates must be one of: {', '.join(_SEND_UPDATES)}."

    # ── Gmail helpers ──────────────────────────────────────────────────

    def _extract_body(payload: dict[str, Any]) -> str:
        """The text of a Gmail message: its text parts, else its HTML parts as text.

        A message sent as a text / HTML alternative carries the same words
        twice; only the text version is returned, so the two never run into
        each other. Attachments are left out.
        """
        found: dict[str, list[str]] = {"text/plain": [], "text/html": []}

        def _walk(part: dict[str, Any]) -> None:
            mime = part.get("mimeType", "")
            data_val = (part.get("body") or {}).get("data")
            if mime in found and data_val and not part.get("filename"):
                found[mime].append(_b64url_decode(data_val).decode("utf-8", errors="ignore"))
            for child in part.get("parts") or []:
                _walk(child)

        _walk(payload)
        if found["text/plain"]:
            return "\n\n".join(text.strip() for text in found["text/plain"]).strip()
        texts = []
        for markup in found["text/html"]:
            text = re.sub(r"<[^>]+>", " ", markup)
            texts.append(re.sub(r"\s+", " ", text).strip())
        return "\n\n".join(texts).strip()

    def _get_headers(data: dict) -> dict[str, str]:
        """Extract message headers into a dict."""
        return {
            h["name"]: h["value"] for h in data.get("payload", {}).get("headers", [])
        }

    def _latest_gmail_message(messages: object) -> dict | None:
        """The newest message of a Gmail thread (``internalDate``), sent or received, drafts left out."""
        if not isinstance(messages, list):
            return None
        kept = [m for m in messages if isinstance(m, dict) and m.get("id") and "DRAFT" not in (m.get("labelIds") or [])]
        return max(kept, key=lambda m: int(m.get("internalDate") or 0), default=None)

    def _attachment_parts(payload: dict) -> list[dict]:
        """Every part of a Gmail message that is a file (it has a filename)."""
        found: list[dict] = []

        def _walk(part: dict) -> None:
            body = part.get("body") or {}
            if part.get("filename") and (body.get("attachmentId") or body.get("data")):
                found.append(part)
            for child in part.get("parts") or []:
                if isinstance(child, dict):
                    _walk(child)

        _walk(payload or {})
        return found

    def _format_gmail_attachments(parts: list[dict]) -> str:
        lines = [f"Attachments ({len(parts)}):"]
        for part in parts:
            part_headers = {
                (h.get("name") or "").lower(): h.get("value") or ""
                for h in part.get("headers") or []
            }
            details = [
                part.get("mimeType") or "unknown type",
                kilobytes((part.get("body") or {}).get("size")),
            ]
            if "content-id" in part_headers or part_headers.get(
                "content-disposition", ""
            ).lower().startswith("inline"):
                details.append("inline")
            lines.append(
                f"- {part.get('filename')} ({', '.join(details)}) - part_id: {part.get('partId', '')}"
            )
        lines.append(
            "Read one inline and save it as a file with get_gmail_attachment, "
            "passing this message_id and the part_id."
        )
        return "\n".join(lines)

    # ── Gmail Read Tools ───────────────────────────────────────────────

    async def get_gmail_message(message_id: str) -> str:
        """Read full content of a specific email by ID."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_GMAIL_BASE_URL}/users/me/messages/{message_id}",
                _PROVIDER,
                "read email",
                headers=headers,
                params={"format": "full"},
            )
            if err:
                return err
            data = resp.json()
            hdrs = _get_headers(data)
            body_text = _extract_body(data.get("payload", {}))
            label_ids = data.get("labelIds", [])
            tags = ""
            if "UNREAD" in label_ids:
                tags += "[UNREAD] "
            if "STARRED" in label_ids:
                tags += "[STARRED] "
            parts = _attachment_parts(data.get("payload", {}))
            attachments_block = _format_gmail_attachments(parts) + "\n" if parts else ""
            return (
                f"{tags}\n"
                f"Message ID: {message_id}\n"
                f"Thread ID: {data.get('threadId', '')}\n"
                f"From: {hdrs.get('From', 'N/A')}\n"
                f"To: {hdrs.get('To', 'N/A')}\n"
                f"CC: {hdrs.get('Cc', '')}\n"
                f"Subject: {hdrs.get('Subject', 'N/A')}\n"
                f"Date: {hdrs.get('Date', 'N/A')}\n"
                f"Labels: {', '.join(label_ids)}\n"
                f"{attachments_block}"
                f"\nBody:\n{body_text[:3000] or '(No body content)'}\n"
            )

    async def get_gmail_attachment(
        message_id: str,
        part_id: str = "",
        attachment_name: str = "",
        file_context: FileContext | None = None,
    ) -> ToolResult | str:
        """Read a Gmail attachment and keep it as a file in the conversation.

        The attachment is picked by ``part_id`` (stable, listed by
        ``get_gmail_message``), else by ``attachment_name``, else the email's
        only attachment. Its bytes are saved as a reusable file (file_id + a
        file chip) and the text of a readable file is returned inline.
        """
        mid = (message_id or "").strip()
        if not mid:
            return "Error: message_id is required."
        mid_path = _url_encode(mid, safe="")
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_GMAIL_BASE_URL}/users/me/messages/{mid_path}",
                _PROVIDER,
                "read email",
                headers=headers,
                params={"format": "full"},
            )
            if err:
                return err
            parts = _attachment_parts(resp.json().get("payload") or {})
            if not parts:
                return f"Error: email {mid} has no attachment."
            wanted_part = (part_id or "").strip()
            wanted_name = (attachment_name or "").strip().lower()
            chosen = None
            if wanted_part:
                chosen = next((p for p in parts if str(p.get("partId", "")) == wanted_part), None)
            if chosen is None and wanted_name:
                chosen = next(
                    (p for p in parts if (p.get("filename") or "").lower() == wanted_name), None
                )
            if chosen is None and not wanted_part and not wanted_name and len(parts) == 1:
                chosen = parts[0]
            if chosen is None:
                return (
                    "Error: no attachment of this email matches that part_id / "
                    "attachment_name.\n" + _format_gmail_attachments(parts)
                )
            body = chosen.get("body") or {}
            declared = int(body.get("size") or 0)
            if declared > _GOOGLE_MAX_FILE_BYTES:
                return (
                    f"Error: the attachment is {round(declared / 1024 / 1024, 1)} MB, over "
                    f"the {_GOOGLE_MAX_FILE_BYTES // 1024 // 1024} MB download limit."
                )
            if body.get("data"):
                data = _b64url_decode(body["data"])
            else:
                att_resp, err = await safe_request(
                    client,
                    "get",
                    f"{_GMAIL_BASE_URL}/users/me/messages/{mid_path}/attachments/"
                    f"{_url_encode(str(body.get('attachmentId')), safe='')}",
                    _PROVIDER,
                    "download attachment",
                    headers=headers,
                )
                if err:
                    return err
                data = _b64url_decode((att_resp.json() or {}).get("data") or "")

        if not data:
            return "Error: the attachment is empty."
        # The declared size can be absent or wrong: the cap holds on the bytes.
        if len(data) > _GOOGLE_MAX_FILE_BYTES:
            return (
                f"Error: the attachment is {round(len(data) / 1024 / 1024, 1)} MB, over "
                f"the {_GOOGLE_MAX_FILE_BYTES // 1024 // 1024} MB download limit."
            )
        name = chosen.get("filename") or "gmail-attachment"
        mime = chosen.get("mimeType") or mimetypes.guess_type(name)[0] or "application/octet-stream"
        file_ref = await keep_file(persist_file_fn, file_context, name, data, mime)
        text, limit, reason = await readable_text(data, name, mime)
        answer = [
            f"Attachment: {name} ({mime}, {kilobytes(len(data))}) from the email {mid}.",
            kept_file_line(file_ref),
        ]
        if text is not None:
            answer.append("Content:\n" + cut_inline(text, limit, rest_of_file_hint(file_ref)))
        elif mime.startswith("image/"):
            answer.append("It is an image: its pixels are attached to this result.")
        else:
            answer.append(f"Its content is not shown here: it is {reason}. {rest_of_file_hint(file_ref)}")
        return ToolResult(
            tool_call_id="",
            content="\n".join(answer),
            # An image reaches the model as pixels; readable text is inline above.
            files=[file_ref] if mime.startswith("image/") else [],
        )

    async def search_gmail(query: str, max_results: int = 20) -> str:
        """Search emails using Gmail search query syntax."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_GMAIL_BASE_URL}/users/me/messages",
                _PROVIDER,
                "search emails",
                headers=headers,
                params={"q": query, "maxResults": str(max_results)},
            )
            if err:
                return err
            msg_list = resp.json().get("messages", [])
            if not msg_list:
                return f"No emails found matching query: {query}"
            lines: list[str] = []
            for msg_ref in msg_list[:max_results]:
                detail_resp, detail_err = await safe_request(
                    client,
                    "get",
                    f"{_GMAIL_BASE_URL}/users/me/messages/{msg_ref['id']}",
                    _PROVIDER,
                    "read email detail",
                    headers=headers,
                    params={
                        "format": "metadata",
                        "metadataHeaders": ["Subject", "From", "Date"],
                    },
                )
                if detail_err:
                    continue
                data = detail_resp.json()
                hdrs = _get_headers(data)
                lines.append(
                    f"ID: {msg_ref['id']}\n"
                    f"From: {hdrs.get('From', '?')}\n"
                    f"Subject: {hdrs.get('Subject', '?')}\n"
                    f"Date: {hdrs.get('Date', '?')}\n"
                    f"Preview: {data.get('snippet', '')[:200]}"
                )
            return "\n---\n".join(lines)

    async def list_gmail_labels() -> str:
        """List all Gmail labels (folders and categories)."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_GMAIL_BASE_URL}/users/me/labels",
                _PROVIDER,
                "list labels",
                headers=headers,
            )
            if err:
                return err
            labels = resp.json().get("labels", [])
            return format_entity_list(
                labels,
                lambda lb: f"{lb.get('name', '?')} (ID: {lb.get('id', '')}, type: {lb.get('type', 'user')})",
                noun="label",
                empty_msg="No labels found.",
            )

    # ── Gmail Write Tools ──────────────────────────────────────────────

    async def send_gmail(
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        bcc: str = "",
        body_html: str = "",
        attachment_file_ids: list[str] | str | None = None,
        file_context: FileContext | None = None,
    ) -> str:
        """Compose and send a new email via Gmail immediately.

        ``attachment_file_ids`` attaches files of this conversation
        byte-for-byte (sent through Gmail's upload endpoint).
        """
        files, err = await _resolve_platform_files(
            attachment_file_ids, file_context, max_total=_GMAIL_MAX_ATTACHMENT_BYTES
        )
        if err:
            return err
        msg = _alternative(body, body_html, await _gmail_signature(reply=False))
        msg["To"] = to
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = cc
        if bcc:
            msg["Bcc"] = bcc
        if files:
            async with _client() as client:
                resp, err = await _multipart_upload(
                    client,
                    f"{_GMAIL_UPLOAD_URL}/users/me/messages/send?uploadType=multipart",
                    {},
                    _with_attachments(msg, files).as_bytes(),
                    "message/rfc822",
                    "send email",
                )
                if err:
                    return err
                result = resp.json()
                return _with_attached(
                    f"Email sent to {to}. Message ID: {result.get('id', '')}.",
                    attachment_file_ids,
                    files,
                )
        raw_message = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_GMAIL_BASE_URL}/users/me/messages/send",
                _PROVIDER,
                "send email",
                headers=headers,
                json={"raw": raw_message},
            )
            if err:
                return err
            result = resp.json()
            return f"Email sent to {to}. Message ID: {result.get('id', '')}."

    async def create_gmail_draft(
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        bcc: str = "",
        body_html: str = "",
        attachment_file_ids: list[str] | str | None = None,
        file_context: FileContext | None = None,
    ) -> str:
        """Create a draft email in Gmail (saved, not sent).

        ``attachment_file_ids`` attaches files of this conversation
        byte-for-byte.
        """
        files, err = await _resolve_platform_files(
            attachment_file_ids, file_context, max_total=_GMAIL_MAX_ATTACHMENT_BYTES
        )
        if err:
            return err
        msg = _alternative(body, body_html, await _gmail_signature(reply=False))
        msg["To"] = to
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = cc
        if bcc:
            msg["Bcc"] = bcc
        if files:
            async with _client() as client:
                resp, err = await _multipart_upload(
                    client,
                    f"{_GMAIL_UPLOAD_URL}/users/me/drafts?uploadType=multipart",
                    {},
                    _with_attachments(msg, files).as_bytes(),
                    "message/rfc822",
                    "create draft",
                )
                if err:
                    return err
                result = resp.json()
                return _with_attached(
                    f"Draft created. Draft ID: {result.get('id', '')}. "
                    f"Message ID: {result.get('message', {}).get('id', '')}. "
                    "Saved in Drafts — NOT sent.",
                    attachment_file_ids,
                    files,
                )
        raw_message = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_GMAIL_BASE_URL}/users/me/drafts",
                _PROVIDER,
                "create draft",
                headers=headers,
                json={"message": {"raw": raw_message}},
            )
            if err:
                return err
            result = resp.json()
            return (
                f"Draft created. Draft ID: {result.get('id', '')}. "
                f"Message ID: {result.get('message', {}).get('id', '')}. "
                f"Saved in Drafts — NOT sent."
            )

    async def _sign_gmail_draft(client: AutoRefreshClient, draft_id: str) -> str:
        """Sign a draft that carries no signature yet, before it is sent; a note for the result.

        A draft that cannot be read or signed is still sent: the send reports
        its own error, and a failed signing is said in the result.
        """
        resp, err = await safe_request(
            client,
            "get",
            f"{_GMAIL_BASE_URL}/users/me/drafts/{draft_id}",
            _PROVIDER,
            "read draft",
            headers=headers,
            params={"format": "raw"},
        )
        if err or resp is None:
            return ""
        message = (resp.json() or {}).get("message") or {}
        if not message.get("raw"):
            return ""
        parsed = message_from_bytes(_b64url_decode(message["raw"]), policy=email_policy.default)
        if not isinstance(parsed, EmailMessage) or _is_gmail_signed(parsed):
            return ""
        signature = await _gmail_signature(reply=bool(parsed.get("In-Reply-To")))
        if not signature or not _sign_message(parsed, signature):
            return ""
        metadata: dict[str, Any] = {"id": draft_id}
        if message.get("threadId"):
            metadata["message"] = {"threadId": message["threadId"]}
        _resp, err = await _multipart_upload(
            client,
            f"{_GMAIL_UPLOAD_URL}/users/me/drafts/{draft_id}?uploadType=multipart",
            metadata,
            parsed.as_bytes(),
            "message/rfc822",
            "sign draft",
            method="put",
        )
        if err:
            return f" It was sent without the user's signature, which could not be added: {err}"
        return " The user's email signature was added before it was sent."

    async def send_gmail_draft(draft_id: str) -> str:
        """Send an existing draft email from Gmail, signed first when it carries no signature yet."""
        async with _client() as client:
            signed_note = await _sign_gmail_draft(client, draft_id) if signer.signs else ""
            resp, err = await safe_request(
                client,
                "post",
                f"{_GMAIL_BASE_URL}/users/me/drafts/send",
                _PROVIDER,
                "send draft",
                headers=headers,
                json={"id": draft_id},
            )
            if err:
                return err
            result = resp.json()
            return f"Draft sent. Message ID: {result.get('id', '')}.{signed_note}"

    async def get_gmail_email_signature() -> str:
        """The user's email signature as the Gmail tools add it, and where it comes from."""
        if not signer.signs:
            return signer.report("", "")
        address = await _own_gmail_address()
        return signer.report(address, await signer.markup(address, reply=False))

    async def reply_gmail(
        message_id: str,
        body: str,
        reply_all: bool = False,
        body_html: str = "",
        attachment_file_ids: list[str] | str | None = None,
        to: str = "",
        cc: str = "",
        bcc: str = "",
        file_context: FileContext | None = None,
        reply_to_exact_message: bool = False,
    ) -> str:
        """Reply to an existing Gmail email.

        The reply is made on the newest message of the thread (sent or
        received, drafts left out) unless ``reply_to_exact_message``: a
        follow-up after this mailbox's own reply answers that reply, and goes
        to the people it went to.

        Fetches the original message to extract the thread ID, subject,
        and recipients, then sends a reply preserving the thread.
        ``to``, ``cc`` and ``bcc``, when given, replace the recipients the
        reply would get (the sender, and with ``reply_all`` the others), so a
        person added in copy stays in the thread.
        ``attachment_file_ids`` attaches files of this conversation
        byte-for-byte.
        """
        files, err = await _resolve_platform_files(
            attachment_file_ids, file_context, max_total=_GMAIL_MAX_ATTACHMENT_BYTES
        )
        if err:
            return err
        async with _client() as client:
            orig_resp, err = await safe_request(
                client,
                "get",
                f"{_GMAIL_BASE_URL}/users/me/messages/{message_id}",
                _PROVIDER,
                "fetch original email for reply",
                headers=headers,
                params={
                    "format": "metadata",
                    "metadataHeaders": ["Subject", "From", "Reply-To", "To", "Cc", "Message-ID", "References"],
                },
            )
            if err:
                return err
            orig = orig_resp.json()
            thread_id = orig.get("threadId", "")
            repointed = ""
            if not reply_to_exact_message and thread_id:
                try:
                    thread_resp, thread_err = await safe_request(
                        client,
                        "get",
                        f"{_GMAIL_BASE_URL}/users/me/threads/{thread_id}",
                        _PROVIDER,
                        "read the thread for the latest message",
                        headers=headers,
                        params={
                            "format": "metadata",
                            "metadataHeaders": ["Subject", "From", "Reply-To", "To", "Cc", "Message-ID", "References"],
                        },
                    )
                    thread = thread_resp.json() if not thread_err and thread_resp is not None else {}
                except Exception:  # best effort: the reply goes on the message given
                    thread = {}
                newest = _latest_gmail_message(thread.get("messages") if isinstance(thread, dict) else None)
                if (
                    newest is not None
                    and newest.get("id") != orig.get("id")
                    and int(newest.get("internalDate") or 0) > int(orig.get("internalDate") or 0)
                ):
                    orig = newest
                    repointed = (
                        f" Replied on the latest message of the thread ({_get_headers(newest).get('From', 'unknown sender')}) "
                        f"instead of {message_id}: pass reply_to_exact_message=true to answer an older message."
                    )
            hdrs = _get_headers(orig)
            own_sent = "SENT" in (orig.get("labelIds") or [])

            # Build reply recipients: the caller's to / cc / bcc replace the defaults.
            # A follow-up on this mailbox's own sent message goes to the people it went to.
            reply_to = (
                to.strip() or (hdrs.get("To", "") if own_sent else "") or hdrs.get("Reply-To") or hdrs.get("From", "")
            )
            subject = hdrs.get("Subject", "")
            if not subject.lower().startswith("re:"):
                subject = f"Re: {subject}"

            msg = _alternative(body, body_html, await _gmail_signature(reply=True))
            msg["To"] = reply_to
            msg["Subject"] = subject
            if cc.strip():
                msg["Cc"] = cc.strip()
            elif reply_all:
                # Include original To and CC (minus the user's own address);
                # on this mailbox's own sent message its To is already the reply's To.
                orig_to = "" if own_sent else hdrs.get("To", "")
                orig_cc = hdrs.get("Cc", "")
                all_recipients = f"{orig_to}, {orig_cc}".strip(", ")
                if all_recipients:
                    msg["Cc"] = all_recipients
            if bcc.strip():
                msg["Bcc"] = bcc.strip()
            # Thread headers: the parent, after the chain it already carries.
            orig_message_id = hdrs.get("Message-ID", "")
            if orig_message_id:
                references = hdrs.get("References", "").strip()
                msg["In-Reply-To"] = orig_message_id
                msg["References"] = (
                    references if orig_message_id in references else f"{references} {orig_message_id}".strip()
                )

            if files:
                resp, err = await _multipart_upload(
                    client,
                    f"{_GMAIL_UPLOAD_URL}/users/me/messages/send?uploadType=multipart",
                    {"threadId": thread_id} if thread_id else {},
                    _with_attachments(msg, files).as_bytes(),
                    "message/rfc822",
                    "send reply",
                )
                if err:
                    return err
                result = resp.json()
                return _with_attached(
                    f"Reply sent to {reply_to}. Message ID: {result.get('id', '')}. Thread ID: {thread_id}.{repointed}",
                    attachment_file_ids,
                    files,
                )

            raw_message = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
            resp, err = await safe_request(
                client,
                "post",
                f"{_GMAIL_BASE_URL}/users/me/messages/send",
                _PROVIDER,
                "send reply",
                headers=headers,
                json={"raw": raw_message, "threadId": thread_id},
            )
            if err:
                return err
            result = resp.json()
            return f"Reply sent to {reply_to}. Message ID: {result.get('id', '')}. Thread ID: {thread_id}.{repointed}"

    async def modify_gmail_labels(
        message_id: str,
        add_labels: str = "",
        remove_labels: str = "",
    ) -> str:
        """Add or remove labels from a Gmail message.

        Common label IDs: INBOX, UNREAD, STARRED, IMPORTANT, SPAM,
        TRASH, CATEGORY_PERSONAL, CATEGORY_SOCIAL, CATEGORY_PROMOTIONS.
        Use list_gmail_labels to see all available labels.

        To mark as read: remove_labels='UNREAD'.
        To star: add_labels='STARRED'.
        To archive: remove_labels='INBOX'.
        """
        add_list = (
            [lb.strip() for lb in add_labels.split(",") if lb.strip()]
            if add_labels
            else []
        )
        remove_list = (
            [lb.strip() for lb in remove_labels.split(",") if lb.strip()]
            if remove_labels
            else []
        )
        if not add_list and not remove_list:
            return "Error: provide at least one label to add or remove."
        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_GMAIL_BASE_URL}/users/me/messages/{message_id}/modify",
                _PROVIDER,
                "modify labels",
                headers=headers,
                json={"addLabelIds": add_list, "removeLabelIds": remove_list},
            )
            if err:
                return err
            added = f"Added: {', '.join(add_list)}" if add_list else ""
            removed = f"Removed: {', '.join(remove_list)}" if remove_list else ""
            return f"Labels updated for email {message_id}. {added} {removed}".strip()

    async def trash_gmail(message_id: str) -> str:
        """Move an email to the Trash in Gmail."""
        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_GMAIL_BASE_URL}/users/me/messages/{message_id}/trash",
                _PROVIDER,
                "trash email",
                headers=headers,
            )
            if err:
                return err
            return f"Email {message_id} moved to Trash."

    # ── Google Calendar Tools ──────────────────────────────────────────

    async def list_google_calendars() -> str:
        """List all Google Calendars the user has access to."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                "https://www.googleapis.com/calendar/v3/users/me/calendarList",
                _PROVIDER,
                "list calendars",
                headers=headers,
                params={"maxResults": "50"},
            )
            if err:
                return err
            calendars = resp.json().get("items", [])
            if not calendars:
                return "No calendars found."
            lines: list[str] = []
            for cal in calendars:
                primary = " [PRIMARY]" if cal.get("primary") else ""
                access = cal.get("accessRole", "")
                lines.append(
                    f"{cal.get('summary', 'Untitled')}{primary}\n  ID: {cal.get('id', '')}\n  Access: {access}"
                )
            return "\n---\n".join(lines)

    async def read_google_calendar(
        days_ahead: int = 7,
        max_results: int = 50,
        calendar_id: str = "primary",
        start_datetime: str = "",
        end_datetime: str = "",
        days_back: int = 0,
    ) -> str:
        """Read the calendar events of a window, past or future.

        Without bounds the window is ``[now - days_back, now + days_ahead]``
        (the historical "next 7 days"). ``start_datetime`` / ``end_datetime``
        (ISO 8601; a bare date covers its whole day, a time without offset is
        UTC) set the window; a missing end is ``start + days_ahead``. All-day
        events are marked "(all day)" with their dates, the last day included.
        """
        for name, value in (("days_ahead", days_ahead), ("days_back", days_back)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return f"Error: '{name}' must be a non-negative integer."
        now = datetime.now(UTC)
        try:
            start = _window_bound(start_datetime, end=False) if start_datetime else None
            end = _window_bound(end_datetime, end=True) if end_datetime else None
        except ValueError:
            return (
                "Error: start_datetime and end_datetime are ISO 8601 dates or date-times "
                "(e.g. '2026-11-04' or '2026-11-04T09:00:00Z')."
            )
        if start is None:
            start = now - timedelta(days=days_back)
        if end is None:
            end = (start if start_datetime else now) + timedelta(days=days_ahead)
        if end <= start:
            return "Error: the window ends before it starts: pass an end_datetime after start_datetime (or days_back)."
        time_min = start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        time_max = end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
                _PROVIDER,
                "read calendar events",
                headers=headers,
                params={
                    "timeMin": time_min,
                    "timeMax": time_max,
                    "maxResults": str(max_results),
                    "orderBy": "startTime",
                    "singleEvents": "true",
                },
            )
            if err:
                return err
            events = resp.json().get("items", [])
            if not events:
                if not (start_datetime or end_datetime or days_back):
                    return f"No events found in the next {days_ahead} days."
                return f"No events found between {time_min[:19]}Z and {time_max[:19]}Z."
            lines: list[str] = []
            for event in events:
                summary = event.get("summary", "No title")
                first, last, all_day = _event_span(event)
                if all_day:
                    when = f"{first} (all day)" if last == first else f"{first} -> {last} (all day)"
                else:
                    when = f"{first} — {last}"
                location = event.get("location", "")
                attendees = event.get("attendees", [])
                attendee_emails = ", ".join(a.get("email", "") for a in attendees[:5])
                if len(attendees) > 5:
                    attendee_emails += f" (+{len(attendees) - 5} more)"
                line = f"ID: {event.get('id', '')}\nEvent: {summary} | {when}"
                if location:
                    line += f" | Location: {location}"
                if attendee_emails:
                    line += f"\n  Attendees: {attendee_emails}"
                lines.append(line)
            return "\n---\n".join(lines)

    async def get_google_event(event_id: str, calendar_id: str = "primary") -> str:
        """Get full details of a specific calendar event."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{event_id}",
                _PROVIDER,
                "get calendar event",
                headers=headers,
            )
            if err:
                return err
            e = resp.json()
            start, end, all_day = _event_span(e)
            if all_day:
                end += " (last day, included)"
            attendees = e.get("attendees", [])
            attendee_lines = []
            for a in attendees[:15]:
                status = a.get("responseStatus", "needsAction")
                attendee_lines.append(f"  {a.get('email', '')} ({status})")
            result = (
                f"Subject: {e.get('summary', 'Untitled')}\n"
                f"ID: {e.get('id', '')}\n"
                f"Start: {start}\n"
                f"End: {end}\n"
                f"All day: {'Yes' if all_day else 'No'}\n"
                f"Location: {e.get('location', 'None')}\n"
                f"Status: {e.get('status', '')}\n"
                f"Organizer: {e.get('organizer', {}).get('email', '')}\n"
                f"Conference: {e.get('hangoutLink', 'None')}\n"
            )
            if attendee_lines:
                result += (
                    f"Attendees ({len(attendees)}):\n"
                    + "\n".join(attendee_lines)
                    + "\n"
                )
            desc = e.get("description", "")
            if desc:
                result += f"\nDescription:\n{desc[:1000]}\n"
            # Event attachments are Drive files.
            attachments = [a for a in e.get("attachments") or [] if isinstance(a, dict)]
            if attachments:
                lines = [f"\nAttachments ({len(attachments)}):"]
                for att in attachments:
                    line = f"- {att.get('title') or 'attachment'} ({att.get('mimeType') or 'unknown type'})"
                    if att.get("fileUrl"):
                        line += f" - URL: {att['fileUrl']}"
                    if att.get("fileId"):
                        line += f" - file_id: {att['fileId']}"
                    lines.append(line)
                lines.append(
                    "Read one with read_google_doc or save it in the conversation "
                    "with download_google_drive_file, passing its file_id."
                )
                result += "\n".join(lines) + "\n"
            return result

    async def create_google_event(
        summary: str,
        start_time: str,
        end_time: str,
        description: str = "",
        location: str = "",
        attendees: str = "",
        timezone: str = "UTC",
        calendar_id: str = "primary",
        conference: bool = False,
        send_updates: str = "all",
        attachment_file_ids: list[str] | str | None = None,
        all_day: bool | str | None = None,
        file_context: FileContext | None = None,
    ) -> str:
        """Create a calendar event. Times should be ISO 8601 format.

        An all-day event (``all_day=true``, or two bare dates) is written with
        ``start.date`` / ``end.date``: ``end_time`` is its last day, included,
        and Google's exclusive end date is the day after it.

        ``attachment_file_ids`` attaches files of this conversation:
        each is copied to a folder of the connected account's Drive and shared
        read-only with the guests before the event is written, so the
        invitation (``send_updates``) links files they can open.
        """
        if err := _send_updates_error(send_updates):
            return err
        span, err = _all_day_span(start_time, end_time, _flag(all_day))
        if err:
            return err
        files, err = await _resolve_platform_files(
            attachment_file_ids, file_context, max_total=_GOOGLE_MAX_FILE_BYTES
        )
        if err:
            return err
        if len(files) > _CALENDAR_MAX_ATTACHMENTS:
            return f"Error: an event holds at most {_CALENDAR_MAX_ATTACHMENTS} attachments."
        event_data: dict = {"summary": summary}
        if span:
            event_data["start"] = {"date": span[0].isoformat()}
            event_data["end"] = {"date": span[1].isoformat()}
        else:
            event_data["start"] = {"dateTime": start_time, "timeZone": timezone}
            event_data["end"] = {"dateTime": end_time, "timeZone": timezone}
        if description:
            event_data["description"] = description
        if location:
            event_data["location"] = location
        attendee_list = [{"email": e.strip()} for e in attendees.split(",") if e.strip()]
        if attendee_list:
            event_data["attendees"] = attendee_list
        if conference:
            event_data["conferenceData"] = {
                "createRequest": {
                    "requestId": f"copilot-{datetime.now(UTC).timestamp()}"
                },
            }
        params: dict = {"sendUpdates": send_updates}
        if conference:
            params["conferenceDataVersion"] = "1"
        async with _client() as client:
            attachments: list[dict] = []
            attachment_lines: list[str] = []
            attachment_names: list[str] = []
            if files:
                attachments, attachment_lines, attachment_names, err = await _copy_for_event(
                    client, files, [a["email"] for a in attendee_list], attachment_file_ids
                )
                if err:
                    return err
                event_data["attachments"] = attachments
                params["supportsAttachments"] = "true"
            resp, err = await safe_request(
                client,
                "post",
                f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
                _PROVIDER,
                "create calendar event",
                headers={**headers, "Content-Type": "application/json"},
                json=event_data,
                params=params,
            )
            if err:
                if attachments:
                    return f"{err}\nThe event was not created. {await _discard_copies(client, attachments)}"
                return err
            result = resp.json()
            link = result.get("hangoutLink") or result.get("htmlLink", "")
            lines = [
                f"Event created: '{result.get('summary', '')}'. ID: {result.get('id', '')}. Link: {link}",
                *attachment_lines,
            ]
            if span:
                last = (span[1] - timedelta(days=1)).isoformat()
                lines.append(f"All day: {span[0].isoformat()} to {last}, both included.")
            if attendee_list:
                lines.append(f"Invitations: {_SEND_UPDATES[send_updates]}.")
            return "\n".join([*lines, *attachment_names])

    async def update_google_event(
        event_id: str,
        summary: str = "",
        start_time: str = "",
        end_time: str = "",
        description: str = "",
        location: str = "",
        attendees: str = "",
        timezone: str = "",
        calendar_id: str = "primary",
        send_updates: str = "all",
        attachment_file_ids: list[str] | str | None = None,
        all_day: bool | str | None = None,
        file_context: FileContext | None = None,
    ) -> str:
        """Update an existing Google Calendar event.

        Only provided fields are updated; omitted fields remain unchanged.
        ``all_day=true`` (or two bare dates) makes the event all-day over
        those dates, ``end_time`` its last day included; ``all_day=false``
        with two date-times makes it a timed event again. The other form of
        each bound is cleared (``null``): a PATCH merges ``start`` and ``end``,
        so a ``dateTime`` left next to a ``date`` would be refused.
        ``attendees`` replaces existing attendees. ``attachment_file_ids`` adds
        files of this conversation next to the event's attachments, copied to
        Drive and shared read-only with the guests like on creation.
        """
        if err := _send_updates_error(send_updates):
            return err
        all_day_flag = _flag(all_day)
        if all_day_flag is not None and not (start_time and end_time):
            return (
                "Error: all_day changes the event's form, so it needs both start_time and "
                "end_time: dates (YYYY-MM-DD, end_time the last day included) for "
                "all_day=true, ISO 8601 date-times for all_day=false."
            )
        span, err = _all_day_span(start_time, end_time, all_day_flag) if start_time and end_time else (None, None)
        if err:
            return err
        if not span and (_bare_date(start_time) or _bare_date(end_time)):
            return f"Error: {_DATES_MESSAGE}: pass both start_time and end_time."
        files, err = await _resolve_platform_files(
            attachment_file_ids, file_context, max_total=_GOOGLE_MAX_FILE_BYTES
        )
        if err:
            return err
        payload: dict = {}
        if summary:
            payload["summary"] = summary
        if span:
            payload["start"] = {"date": span[0].isoformat(), "dateTime": None, "timeZone": None}
            payload["end"] = {"date": span[1].isoformat(), "dateTime": None, "timeZone": None}
        else:
            if start_time:
                payload["start"] = {"dateTime": start_time, "timeZone": timezone or "UTC", "date": None}
            if end_time:
                payload["end"] = {"dateTime": end_time, "timeZone": timezone or "UTC", "date": None}
        if description:
            payload["description"] = description
        if location:
            payload["location"] = location
        if attendees:
            attendee_list = [
                {"email": e.strip()} for e in attendees.split(",") if e.strip()
            ]
            payload["attendees"] = attendee_list
        if not payload and not files:
            return "Error: no fields to update."
        event_url = f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{event_id}"
        params: dict = {"sendUpdates": send_updates}
        async with _client() as client:
            attachments: list[dict] = []
            attachment_lines: list[str] = []
            attachment_names: list[str] = []
            copies: list[dict] = []
            series: tuple[set[str], set[str]] | None = None
            before: list[str] = []
            guests: list[str] = []
            if files or "attendees" in payload:
                current_resp, err = await safe_request(
                    client, "get", event_url, _PROVIDER, "get calendar event", headers=headers
                )
                if err:
                    return err
                current = current_resp.json() or {}
                existing = [a for a in current.get("attachments") or [] if isinstance(a, dict)]
                if len(existing) + len(files) > _CALENDAR_MAX_ATTACHMENTS:
                    return (
                        f"Error: the event already holds {len(existing)} attachment(s); an "
                        f"event holds at most {_CALENDAR_MAX_ATTACHMENTS}."
                    )
                before = _guest_emails(current.get("attendees"))
                guests = _guest_emails(payload.get("attendees", current.get("attendees")))
                if "attendees" in payload and existing:
                    # The copies this integration attached follow the guest list.
                    series, err = await _series_of(client, calendar_id, current)
                    if err:
                        return f"{err}\nThe event was not updated, so no attachment share changed."
                    copies, unchecked = await _event_copies(client, existing)
                    if unchecked:
                        return (
                            "Error: could not check whether these attachments are copies "
                            f"shared with the guests: {'; '.join(unchecked)}. The event was "
                            "not updated, so no removed guest keeps reading them; try again."
                        )
                if files:
                    attachments, copy_lines, attachment_names, err = await _copy_for_event(
                        client, files, guests, attachment_file_ids
                    )
                    if err:
                        return err
                    attachment_lines = copy_lines + attachment_lines
                    # A patched array replaces the stored one: keep the event's own.
                    payload["attachments"] = existing + attachments
                    params["supportsAttachments"] = "true"
            resp, err = await safe_request(
                client,
                "patch",
                event_url,
                _PROVIDER,
                "update calendar event",
                headers={**headers, "Content-Type": "application/json"},
                json=payload,
                params=params,
            )
            if err:
                if attachments:
                    return f"{err}\nThe event was not updated. {await _discard_copies(client, attachments)}"
                return err
            event = resp.json()
            if copies:
                attachment_lines += await _follow_guests(
                    client,
                    copies,
                    before,
                    guests,
                    series=series,
                )
            lines = [
                f"Event updated: '{event.get('summary', '')}' (ID: {event.get('id', '')}).",
                *attachment_lines,
            ]
            if event.get("attendees"):
                lines.append(f"Updates: {_SEND_UPDATES[send_updates]}.")
            return "\n".join([*lines, *attachment_names])

    async def delete_google_event(event_id: str, calendar_id: str = "primary") -> str:
        """Delete a Google Calendar event. Sends cancellation notices to attendees.

        The Drive copies this integration attached to it are deleted
        with it, so no former guest keeps reading them. For one occurrence of
        a recurring event, only the copies attached to that occurrence alone
        go: a copy the series links stays for the other occurrences.
        """
        event_url = f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{event_id}"
        async with _client() as client:
            current_resp, err = await safe_request(
                client, "get", event_url, _PROVIDER, "get calendar event", headers=headers
            )
            if err:
                return err
            current = current_resp.json() or {}
            series, err = await _series_of(client, calendar_id, current)
            if err:
                return f"{err}\nThe event was not deleted, so no attachment copy changed; try again."
            series_copies = series[0] if series else set()
            existing = [
                a
                for a in current.get("attachments") or []
                if isinstance(a, dict) and a.get("fileId") not in series_copies
            ]
            copies, unchecked = await _event_copies(client, existing) if existing else ([], [])
            if unchecked:
                return (
                    "Error: could not check whether these attachments are copies shared "
                    f"with the guests: {'; '.join(unchecked)}. The event was not deleted, "
                    "so no copy stays shared without its event; try again."
                )
            _, err = await safe_request(
                client,
                "delete",
                event_url,
                _PROVIDER,
                "delete calendar event",
                headers=headers,
                params={"sendUpdates": "all"},
            )
            if err:
                return err
            answer = f"Event {event_id} deleted. Cancellation notices sent to attendees."
            if copies:
                answer += f" {await _discard_copies(client, copies)}"
            return answer

    # ── Google Drive Tools ─────────────────────────────────────────────

    async def list_google_drive(folder_id: str = "root", max_results: int = 50) -> str:
        """List files in a Google Drive folder (default: root)."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_DRIVE_BASE_URL}/files",
                _PROVIDER,
                "list Drive files",
                headers=headers,
                params={
                    "q": f"'{folder_id}' in parents and trashed=false",
                    "pageSize": str(max_results),
                    "fields": "files(id,name,mimeType,modifiedTime,size,webViewLink)",
                    "orderBy": "modifiedTime desc",
                },
            )
            if err:
                return err
            files = resp.json().get("files", [])
            if not files:
                return f"No files found in folder {folder_id}."
            lines: list[str] = []
            for f in files:
                size = f.get("size")
                size_str = f"{int(size) / 1024:.1f} KB" if size else "N/A"
                is_folder = f.get("mimeType") == "application/vnd.google-apps.folder"
                prefix = "[Folder] " if is_folder else ""
                link = f" | URL: {f['webViewLink']}" if f.get("webViewLink") else ""
                lines.append(
                    f"{prefix}ID: {f.get('id', '')} | {f.get('name', '')} | "
                    f"Modified: {f.get('modifiedTime', '')} | Size: {size_str}{link}"
                )
            return "\n".join(lines)

    async def search_google_drive(query: str, max_results: int = 20) -> str:
        """Search for files in Google Drive by name or content.

        Supports Google Drive query syntax. Simple keywords search file
        names and content.  For advanced queries: ``name contains 'report'``,
        ``mimeType = 'application/pdf'``, ``modifiedTime > '2024-01-01'``.
        """
        # If query looks like a plain keyword, wrap it in name search
        if (
            "contains" not in query
            and "=" not in query
            and ">" not in query
            and "<" not in query
        ):
            search_q = f"name contains '{query}' and trashed=false"
        else:
            search_q = f"{query} and trashed=false"
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_DRIVE_BASE_URL}/files",
                _PROVIDER,
                "search Drive files",
                headers=headers,
                params={
                    "q": search_q,
                    "pageSize": str(max_results),
                    "fields": "files(id,name,mimeType,modifiedTime,size,webViewLink,parents)",
                    "orderBy": "modifiedTime desc",
                },
            )
            if err:
                return err
            files = resp.json().get("files", [])
            if not files:
                return f"No files matching '{query}'."
            lines: list[str] = []
            for f in files:
                size = f.get("size")
                size_str = f"{int(size) / 1024:.1f} KB" if size else "N/A"
                lines.append(
                    f"{f.get('name', '')} ({f.get('mimeType', '')})\n"
                    f"  ID: {f.get('id', '')} | Size: {size_str}\n"
                    f"  Modified: {f.get('modifiedTime', '')}\n"
                    f"  URL: {f.get('webViewLink', '')}"
                )
            return f"Found {len(files)} file(s):\n\n" + "\n---\n".join(lines)

    async def read_google_doc(file_id: str, include_comments: bool = True) -> str:
        """Read content of a Google Doc/Sheet/Slides by file ID.

        A Google Doc is read through the Docs API: its clean text, with no
        comment markers, and (``include_comments``) its open comment threads
        listed apart. Sheets and Slides are exported as plain text. Also works
        for non-Google files: text files (txt, csv, etc.) are downloaded
        directly, and Word / PowerPoint / PDF / Excel files have their text
        extracted. Every answer carries the file's Drive links and, when the
        file is only partly shown, the way to download all of it.
        """
        include_comments = bool_arg(include_comments, True)
        async with _client() as client:
            meta_resp, err = await safe_request(
                client,
                "get",
                f"{_DRIVE_BASE_URL}/files/{file_id}",
                _PROVIDER,
                "read file metadata",
                headers=headers,
                params={
                    "fields": "id,name,mimeType,size,webViewLink,webContentLink,exportLinks",
                    "supportsAllDrives": "true",
                },
            )
            if err:
                return err

            meta = meta_resp.json()
            mime_type = meta.get("mimeType", "")
            file_name = meta.get("name", "")
            file_size = int(meta.get("size", 0) or 0)
            link_lines = _drive_link_lines(meta)
            hint = _drive_download_hint(file_id)

            def _with_links(answer: str) -> str:
                return answer + "\n\n" + "\n".join(link_lines) if link_lines else answer

            def _not_inline(reason: str) -> str:
                return "\n".join(
                    [
                        f"File: {file_name} ({file_size} bytes, {mime_type})",
                        f"This file is {reason}.",
                        "Use the file ID to reference it in other operations.",
                        hint,
                        *link_lines,
                    ]
                )

            # Google Workspace files need export
            google_types = {
                "application/vnd.google-apps.document": "text/plain",
                "application/vnd.google-apps.spreadsheet": "text/csv",
                "application/vnd.google-apps.presentation": "text/plain",
            }

            if mime_type in google_types:
                text, why_exported = None, None
                if mime_type == GOOGLE_DOC_MIME:
                    text, why_exported = await docs.document_text(client, file_id, hint)
                if text is None:
                    resp, err = await safe_request(
                        client,
                        "get",
                        f"{_DRIVE_BASE_URL}/files/{file_id}/export",
                        _PROVIDER,
                        "export file content",
                        headers=headers,
                        params={"mimeType": google_types[mime_type]},
                    )
                    if err:
                        return err
                    text = cut_inline(resp.text, MAX_TEXT_CHARS, hint)
                    if why_exported:
                        text = f"{export_note(why_exported)}\n\n{text}"
                answer = f"File: {file_name} ({mime_type})\n\nContent:\n{text}"
                if mime_type == "application/vnd.google-apps.spreadsheet":
                    answer += (
                        "\n\n(Drive exports only the first sheet as CSV; "
                        "download_google_drive_file gives the whole workbook.)"
                    )
                if include_comments:
                    answer += "\n\n" + await docs.comments_section(
                        client, file_id, f"'{file_name}'" if file_name else f"file {file_id}"
                    )
                return _with_links(answer)

            if _is_drive_document(file_name, mime_type):
                if file_size > MAX_DOCUMENT_BYTES:
                    return _not_inline("too large to extract inline")
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{_DRIVE_BASE_URL}/files/{file_id}",
                    _PROVIDER,
                    "download document",
                    headers=headers,
                    params={"alt": "media", "supportsAllDrives": "true"},
                )
                if err:
                    return err
                text, limit, reason = await readable_text(
                    resp.content or b"", file_name, mime_type
                )
                if text is None:
                    return _not_inline(reason)
                return _with_links(
                    f"File: {file_name} ({mime_type})\n\nContent:\n"
                    + cut_inline(text, limit, hint)
                )

            text_mimes = (
                "text/",
                "application/json",
                "application/xml",
                "application/csv",
                "application/javascript",
            )
            is_text = any(mime_type.startswith(t) for t in text_mimes)
            if not is_text or file_size > 500_000:
                return _not_inline(
                    f"{'too large' if file_size > 500_000 else 'binary'} to read as text"
                )
            resp, err = await safe_request(
                client,
                "get",
                f"{_DRIVE_BASE_URL}/files/{file_id}",
                _PROVIDER,
                "download file content",
                headers=headers,
                params={"alt": "media", "supportsAllDrives": "true"},
            )
            if err:
                return err
            return _with_links(
                f"File: {file_name} ({mime_type})\n\nContent:\n"
                + cut_inline(resp.text, MAX_TEXT_CHARS, hint)
            )

    async def download_google_drive_file(
        file_id: str,
        export_format: str = "",
        filename: str = "",
        file_context: FileContext | None = None,
    ) -> ToolResult | str:
        """Download a Drive file's bytes as a reusable conversation file.

        A stored file comes back byte-for-byte; a Google Docs / Sheets /
        Slides / Drawings file is exported (to its Office twin, or to
        ``export_format``). The answer carries the file's Drive links.
        """
        fid = (file_id or "").strip()
        if not fid:
            return "Error: file_id is required (from list_google_drive or search_google_drive)."
        fid_path = _url_encode(fid, safe="")
        note = ""
        async with _client() as client:
            meta_resp, err = await safe_request(
                client,
                "get",
                f"{_DRIVE_BASE_URL}/files/{fid_path}",
                _PROVIDER,
                "read file metadata",
                headers=headers,
                params={
                    "fields": "id,name,mimeType,size,webViewLink,webContentLink,exportLinks",
                    "supportsAllDrives": "true",
                },
            )
            if err:
                return err
            meta = meta_resp.json()
            mime_type = meta.get("mimeType", "")
            file_name = meta.get("name") or "drive-file"
            requested = (export_format or "").strip().lower().lstrip(".")
            if mime_type.startswith("application/vnd.google-apps."):
                offered = meta.get("exportLinks") or {}
                if not offered and mime_type not in _WORKSPACE_DEFAULT_EXPORT:
                    # A folder, form, site or shortcut: nothing to export.
                    return _not_downloadable(meta)
                fmt = requested or _WORKSPACE_DEFAULT_EXPORT.get(mime_type, "pdf")
                if fmt not in _EXPORT_FORMATS:
                    return (
                        f"Error: unknown export_format '{fmt}'. Use one of: "
                        f"{', '.join(sorted(_EXPORT_FORMATS))}."
                    )
                candidates, extension = _EXPORT_FORMATS[fmt]
                export_mime = next(
                    (m for m in candidates if not offered or m in offered), ""
                )
                if not export_mime:
                    available = sorted(
                        name
                        for name, (mimes, _ext) in _EXPORT_FORMATS.items()
                        if any(m in offered for m in mimes)
                    )
                    if not available:
                        return _not_downloadable(meta)
                    return (
                        f"Error: this file cannot be exported as {fmt}. It can be "
                        f"exported as: {', '.join(available)}."
                    )
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{_DRIVE_BASE_URL}/files/{fid_path}/export",
                    _PROVIDER,
                    "export file",
                    headers=headers,
                    params={"mimeType": export_mime},
                )
                if err:
                    return err
                name = filename.strip() or file_name
                if not name.lower().endswith(extension):
                    name = f"{name}{extension}"
                mime = export_mime
            else:
                size = int(meta.get("size") or 0)
                if size > _GOOGLE_MAX_FILE_BYTES:
                    return (
                        f"Error: file is {round(size / 1024 / 1024, 1)} MB, over the "
                        f"{_GOOGLE_MAX_FILE_BYTES // 1024 // 1024} MB download limit."
                    )
                if requested:
                    note = (
                        " export_format applies to Google Docs, Sheets, Slides and "
                        "Drawings only: this file was downloaded as it is."
                    )
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{_DRIVE_BASE_URL}/files/{fid_path}",
                    _PROVIDER,
                    "download file",
                    headers=headers,
                    params={"alt": "media", "supportsAllDrives": "true"},
                )
                if err:
                    return err
                name = filename.strip() or file_name
                mime = mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
            data = resp.content or b""

        if not data:
            return "Error: the downloaded file is empty."
        if len(data) > _GOOGLE_MAX_FILE_BYTES:
            return (
                f"Error: file is {round(len(data) / 1024 / 1024, 1)} MB, over the "
                f"{_GOOGLE_MAX_FILE_BYTES // 1024 // 1024} MB download limit."
            )
        file_ref = await keep_file(persist_file_fn, file_context, name, data, mime)
        answer = (
            f"Downloaded a {kilobytes(len(data))} file from Google Drive as a "
            f"reusable file. [[FILE:{file_ref.file_id}]]\n\nForward it to another tool "
            f"with file_id={file_ref.file_id}, and include the [[FILE:...]] marker "
            f"verbatim so the user gets a download card.{note}\n\n"
            + untrusted_file_facts(
                source="Google Drive",
                filename=name,
                content_type=mime,
                details=_drive_link_lines(meta),
            )
        )
        return ToolResult(tool_call_id="", content=answer, files=[file_ref])

    async def upload_google_drive_file(
        file_id: str,
        folder_id: str = "",
        filename: str = "",
        file_context: FileContext | None = None,
    ) -> str:
        """Upload a conversation file (attached, generated or downloaded) to Google Drive."""
        files, err = await _resolve_platform_files(
            file_id, file_context, max_total=_GOOGLE_MAX_FILE_BYTES
        )
        if err:
            return err
        if not files:
            return "Error: file_id is required (a file attached to or generated in this conversation)."
        data, upload_name, mime = files[0]
        name = filename.strip() or upload_name
        async with _client() as client:
            item, err = await _upload_to_drive(client, data, name, mime, (folder_id or "").strip())
            if err:
                return err
        # The name and Drive's links go in the quarantine block.
        return uploaded_file_answer(
            f"Uploaded file {file_id_list(file_id)[0]} ({kilobytes(len(data))}) to "
            f"Google Drive{' folder ' + folder_id.strip() if (folder_id or '').strip() else ''}. "
            f"File ID: {item.get('id', '')}",
            target="Google Drive",
            filename=item.get("name") or name,
            content_type=item.get("mimeType"),
            details=_drive_link_lines(item),
        )

    # ── Google Chat Tools ──────────────────────────────────────────────

    async def list_google_chat_spaces(count: int = 20) -> str:
        """List Google Chat spaces (rooms and direct messages) the user belongs to.

        Returns space names, types, and display names.  Use the space
        ``name`` field (e.g. ``spaces/AAAA123``) for other Chat operations.
        """
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                "https://chat.googleapis.com/v1/spaces",
                _PROVIDER,
                "list Chat spaces",
                headers=headers,
                params={"pageSize": str(count)},
            )
            if err:
                return err
            spaces = resp.json().get("spaces", [])
            if not spaces:
                return "No Google Chat spaces found."
            lines: list[str] = []
            for s in spaces:
                space_type = s.get("spaceType", s.get("type", "unknown"))
                display = s.get("displayName") or "(Direct message)"
                lines.append(
                    f"{display}\n  Name: {s.get('name', '')}\n  Type: {space_type}"
                )
            return f"Found {len(spaces)} space(s):\n\n" + "\n---\n".join(lines)

    async def read_google_chat_messages(
        space_name: str,
        count: int = 20,
    ) -> str:
        """Read recent messages from a Google Chat space.

        ``space_name`` is the full resource name (e.g. ``spaces/AAAA123``)
        from ``list_google_chat_spaces``.
        """
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"https://chat.googleapis.com/v1/{space_name}/messages",
                _PROVIDER,
                "read Chat messages",
                headers=headers,
                params={"pageSize": str(count), "orderBy": "createTime desc"},
            )
            if err:
                return err
            messages = resp.json().get("messages", [])
            if not messages:
                return "No messages in this space."
            # Reverse to show chronological order
            messages.reverse()
            lines: list[str] = []
            for msg in messages:
                sender = msg.get("sender", {})
                sender_name = sender.get("displayName", "Unknown")
                text = msg.get("text", "")
                created = msg.get("createTime", "")
                msg_name = msg.get("name", "")
                attachments = [a for a in msg.get("attachment") or [] if isinstance(a, dict)]
                if not text and not attachments:
                    continue
                entry = f"[{created}] {sender_name}:\n{text[:500]}"
                if attachments:
                    entry += "\n" + _format_chat_attachments(attachments)
                lines.append(f"{entry}\n(Message: {msg_name})")
            if not lines:
                return "No text messages in this space."
            return f"Found {len(lines)} message(s):\n\n" + "\n---\n".join(lines)

    def _format_chat_attachments(attachments: list[dict]) -> str:
        """A Chat message's files: a Drive file, or content uploaded to Chat."""
        lines = [f"[attachments ({len(attachments)}):"]
        for att in attachments:
            line = f"- {att.get('contentName') or 'attachment'} ({att.get('contentType') or 'unknown type'})"
            if att.get("downloadUri"):
                line += f" - link: {att['downloadUri']}"
            drive_id = (att.get("driveDataRef") or {}).get("driveFileId")
            resource = (att.get("attachmentDataRef") or {}).get("resourceName")
            if drive_id:
                line += (
                    f" - Drive file_id: {drive_id} (read_google_doc / "
                    "download_google_drive_file)"
                )
            elif resource:
                line += (
                    f" - resource_name: {resource} (save it with "
                    "download_google_chat_attachment)"
                )
            lines.append(line)
        return "\n".join(lines) + "]"

    async def download_google_chat_attachment(
        resource_name: str,
        filename: str = "",
        file_context: FileContext | None = None,
    ) -> ToolResult | str:
        """Download a file uploaded to Google Chat and keep it as a conversation file.

        ``resource_name`` is the attachment's resource name listed by
        ``read_google_chat_messages``. The bytes are saved as a reusable file
        (file_id + a file chip) and the text of a readable file is returned
        inline.
        """
        media_url = chat_media_url(resource_name)
        if media_url is None:
            return (
                "Error: resource_name must be the attachment's resource name listed by "
                "read_google_chat_messages."
            )
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                media_url,
                _PROVIDER,
                "download Chat attachment",
                headers=headers,
                params={"alt": "media"},
                follow_redirects=True,
            )
            if err:
                return err
            data = resp.content or b""
            header_mime = (resp.headers.get("content-type") or "").split(";", 1)[0].strip()
        if not data:
            return "Error: the attachment is empty."
        if len(data) > _GOOGLE_MAX_FILE_BYTES:
            return (
                f"Error: the attachment is {round(len(data) / 1024 / 1024, 1)} MB, over the "
                f"{_GOOGLE_MAX_FILE_BYTES // 1024 // 1024} MB download limit."
            )
        name = filename.strip()
        mime = (
            (mimetypes.guess_type(name)[0] if name else None)
            or header_mime
            or "application/octet-stream"
        )
        if not name:
            name = "chat-attachment" + (mimetypes.guess_extension(mime) or "")
        file_ref = await keep_file(persist_file_fn, file_context, name, data, mime)
        text, limit, reason = await readable_text(data, name, mime)
        answer = [
            f"Attachment: {name} ({mime}, {kilobytes(len(data))}) from Google Chat.",
            kept_file_line(file_ref),
        ]
        if text is not None:
            answer.append("Content:\n" + cut_inline(text, limit, rest_of_file_hint(file_ref)))
        elif mime.startswith("image/"):
            answer.append("It is an image: its pixels are attached to this result.")
        else:
            answer.append(f"Its content is not shown here: it is {reason}. {rest_of_file_hint(file_ref)}")
        return ToolResult(
            tool_call_id="",
            content="\n".join(answer),
            files=[file_ref] if mime.startswith("image/") else [],
        )

    async def send_google_chat_message(
        space_name: str,
        message: str,
        attachment_file_ids: list[str] | str | None = None,
        file_context: FileContext | None = None,
    ) -> str:
        """Send a message to a Google Chat space.

        ``space_name`` is the full resource name (e.g. ``spaces/AAAA123``)
        from ``list_google_chat_spaces``. ``attachment_file_ids`` uploads
        files of this conversation to Chat and attaches them.
        """
        if len(file_id_list(attachment_file_ids)) > 1:
            return (
                "Error: a Google Chat message carries at most one attachment - send "
                "one message per file."
            )
        files, err = await _resolve_platform_files(
            attachment_file_ids, file_context, max_total=_GOOGLE_MAX_FILE_BYTES
        )
        if err:
            return err
        if files:
            refs: list[dict] = []
            async with _client() as client:
                for fid, (data, name, mime) in zip(
                    file_id_list(attachment_file_ids), files, strict=True
                ):
                    resp, err = await _multipart_upload(
                        client,
                        f"{_CHAT_UPLOAD_URL}/{space_name}/attachments:upload?uploadType=multipart",
                        {"filename": name},
                        data,
                        mime,
                        "upload Chat attachment",
                    )
                    if err:
                        return err
                    ref = (resp.json() or {}).get("attachmentDataRef")
                    if not ref:
                        return (
                            "Error: Google Chat returned no attachment reference for "
                            f"file {fid}; nothing was sent."
                        )
                    refs.append(ref)
                resp, err = await safe_request(
                    client,
                    "post",
                    f"{_CHAT_API}/{space_name}/messages",
                    _PROVIDER,
                    "send Chat message",
                    headers={**headers, "Content-Type": "application/json"},
                    json={
                        "text": message,
                        "attachment": [{"attachmentDataRef": ref} for ref in refs],
                    },
                )
                if err:
                    return err
                msg = resp.json()
                return _with_attached(
                    f"Message sent to {space_name}. Message: {msg.get('name', '')}. "
                    f"Created: {msg.get('createTime', '')}.",
                    attachment_file_ids,
                    files,
                )
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"https://chat.googleapis.com/v1/{space_name}/messages",
                _PROVIDER,
                "send Chat message",
                headers={**headers, "Content-Type": "application/json"},
                json={"text": message},
            )
            if err:
                return err
            msg = resp.json()
            return f"Message sent to {space_name}. Message: {msg.get('name', '')}. Created: {msg.get('createTime', '')}."

    async def create_google_chat_space(
        display_name: str,
        space_type: str = "SPACE",
        description: str = "",
    ) -> str:
        """Create a new Google Chat space.

        ``space_type`` can be ``SPACE`` (named space/room) or
        ``GROUP_CHAT`` (group DM).
        """
        payload: dict = {
            "spaceType": space_type,
            "displayName": display_name,
        }
        if description:
            payload["spaceDetails"] = {"description": description}
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                "https://chat.googleapis.com/v1/spaces",
                _PROVIDER,
                "create Chat space",
                headers={**headers, "Content-Type": "application/json"},
                json=payload,
            )
            if err:
                return err
            space = resp.json()
            return (
                f"Chat space created: '{space.get('displayName', '')}'. "
                f"Name: {space.get('name', '')}. "
                f"Type: {space.get('spaceType', '')}."
            )

    async def list_google_chat_members(space_name: str) -> str:
        """List members of a Google Chat space.

        ``space_name`` is the full resource name (e.g. ``spaces/AAAA123``).
        """
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"https://chat.googleapis.com/v1/{space_name}/members",
                _PROVIDER,
                "list Chat members",
                headers=headers,
                params={"pageSize": "100"},
            )
            if err:
                return err
            members = resp.json().get("memberships", [])
            return format_entity_list(
                members,
                lambda m: (
                    f"{m.get('member', {}).get('displayName', 'Unknown')} ({m.get('member', {}).get('type', '')}) — {m.get('role', 'ROLE_MEMBER')}"
                ),
                noun="member",
                empty_msg="No members found in this space.",
            )

    # ── Google Tasks Tools ─────────────────────────────────────────────

    async def list_google_task_lists() -> str:
        """List all Google Tasks task lists."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                "https://tasks.googleapis.com/tasks/v1/users/@me/lists",
                _PROVIDER,
                "list task lists",
                headers=headers,
            )
            if err:
                return err
            lists = resp.json().get("items", [])
            return format_entity_list(
                lists,
                lambda tl: f"{tl.get('title', 'Untitled')} | ID: {tl.get('id', '')} | Updated: {tl.get('updated', '')}",
                noun="task list",
                empty_msg="No task lists found.",
            )

    async def list_google_tasks(
        list_id: str = "@default",
        count: int = 20,
        show_completed: bool = False,
    ) -> str:
        """List tasks from a Google Tasks list.

        Uses the default list if no ``list_id`` provided.
        """
        params: dict = {"maxResults": str(count)}
        if show_completed:
            params["showCompleted"] = "true"
        else:
            params["showCompleted"] = "false"
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"https://tasks.googleapis.com/tasks/v1/lists/{list_id}/tasks",
                _PROVIDER,
                "list tasks",
                headers=headers,
                params=params,
            )
            if err:
                return err
            tasks = resp.json().get("items", [])
            if not tasks:
                return "No tasks in this list."
            lines: list[str] = []
            for t in tasks:
                status_icon = "done" if t.get("status") == "completed" else "pending"
                due = t.get("due", "")[:10] if t.get("due") else "none"
                notes = t.get("notes", "")
                line = f"[{status_icon}] {t.get('title', '(untitled)')}\n  ID: {t.get('id', '')} | Due: {due}"
                if notes:
                    line += f"\n  Notes: {notes[:100]}"
                lines.append(line)
            return "\n---\n".join(lines)

    async def create_google_task(
        title: str,
        list_id: str = "@default",
        notes: str = "",
        due_date: str = "",
    ) -> str:
        """Create a task in Google Tasks.

        ``due_date`` should be in YYYY-MM-DD format.
        """
        payload: dict = {"title": title}
        if notes:
            payload["notes"] = notes
        if due_date:
            payload["due"] = due_date + "T00:00:00.000Z"
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"https://tasks.googleapis.com/tasks/v1/lists/{list_id}/tasks",
                _PROVIDER,
                "create task",
                headers={**headers, "Content-Type": "application/json"},
                json=payload,
            )
            if err:
                return err
            task = resp.json()
            return (
                f"Task created: '{task.get('title', '')}' (ID: {task.get('id', '')})."
            )

    async def complete_google_task(task_id: str, list_id: str = "@default") -> str:
        """Mark a Google Task as completed."""
        async with _client() as client:
            _, err = await safe_request(
                client,
                "patch",
                f"https://tasks.googleapis.com/tasks/v1/lists/{list_id}/tasks/{task_id}",
                _PROVIDER,
                "complete task",
                headers={**headers, "Content-Type": "application/json"},
                json={"status": "completed"},
            )
            if err:
                return err
            return f"Task {task_id} marked as completed."

    async def update_google_task(
        task_id: str,
        list_id: str = "@default",
        title: str = "",
        notes: str = "",
        due_date: str = "",
    ) -> str:
        """Update a Google Task. Only provided fields are updated."""
        payload: dict = {}
        if title:
            payload["title"] = title
        if notes:
            payload["notes"] = notes
        if due_date:
            payload["due"] = due_date + "T00:00:00.000Z"
        if not payload:
            return "Error: no fields to update."
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "patch",
                f"https://tasks.googleapis.com/tasks/v1/lists/{list_id}/tasks/{task_id}",
                _PROVIDER,
                "update task",
                headers={**headers, "Content-Type": "application/json"},
                json=payload,
            )
            if err:
                return err
            task = resp.json()
            return (
                f"Task updated: '{task.get('title', '')}' (ID: {task.get('id', '')})."
            )

    async def delete_google_task(task_id: str, list_id: str = "@default") -> str:
        """Delete a Google Task."""
        async with _client() as client:
            _, err = await safe_request(
                client,
                "delete",
                f"https://tasks.googleapis.com/tasks/v1/lists/{list_id}/tasks/{task_id}",
                _PROVIDER,
                "delete task",
                headers=headers,
            )
            if err:
                return err
            return f"Task {task_id} deleted."

    # ── Register all tools ─────────────────────────────────────────────

    # Gmail
    executor.register("get_gmail_message", get_gmail_message)
    executor.register("get_gmail_attachment", get_gmail_attachment)
    executor.register("search_gmail", search_gmail)
    executor.register("list_gmail_labels", list_gmail_labels)
    executor.register("send_gmail", send_gmail)
    executor.register("create_gmail_draft", create_gmail_draft)
    executor.register("send_gmail_draft", send_gmail_draft)
    executor.register("reply_gmail", reply_gmail)
    executor.register("get_gmail_email_signature", get_gmail_email_signature)
    executor.register("modify_gmail_labels", modify_gmail_labels)
    executor.register("trash_gmail", trash_gmail)
    # Calendar
    executor.register("list_google_calendars", list_google_calendars)
    executor.register("read_google_calendar", read_google_calendar)
    executor.register("get_google_event", get_google_event)
    executor.register("create_google_event", create_google_event)
    executor.register("update_google_event", update_google_event)
    executor.register("delete_google_event", delete_google_event)
    # Drive
    executor.register("list_google_drive", list_google_drive)
    executor.register("search_google_drive", search_google_drive)
    executor.register("read_google_doc", read_google_doc)
    executor.register("download_google_drive_file", download_google_drive_file)
    executor.register("upload_google_drive_file", upload_google_drive_file)
    # Docs: comment threads, anchored comments, suggestions, edits
    for name, handler in docs.handlers().items():
        executor.register(name, handler)
    # Sheets: find, read, append, update, clear, delete rows
    for name, handler in sheets.handlers().items():
        executor.register(name, handler)
    # Chat
    executor.register("list_google_chat_spaces", list_google_chat_spaces)
    executor.register("read_google_chat_messages", read_google_chat_messages)
    executor.register("download_google_chat_attachment", download_google_chat_attachment)
    executor.register("send_google_chat_message", send_google_chat_message)
    executor.register("create_google_chat_space", create_google_chat_space)
    executor.register("list_google_chat_members", list_google_chat_members)
    # Tasks
    executor.register("list_google_task_lists", list_google_task_lists)
    executor.register("list_google_tasks", list_google_tasks)
    executor.register("create_google_task", create_google_task)
    executor.register("complete_google_task", complete_google_task)
    executor.register("update_google_task", update_google_task)
    executor.register("delete_google_task", delete_google_task)

    # What the model reads is what this session does.
    gmail_signature = signer.tool_note()
    draft_signature = (
        " Gmail shows the signature as the draft's own when the user opens it." if signer.signs else ""
    )
    send_draft_signature = (
        " A draft that carries no signature yet gets the user's signature before it is sent."
        if signer.signs
        else ""
    )

    # ── Tool definitions ───────────────────────────────────────────────

    return [
        # ── Gmail Read ──
        ToolDefinition(
            name="get_gmail_message",
            description="Read full content of a specific email by message ID. Returns full body, all recipients, labels, thread ID, and each attachment with its part_id (read or save one with get_gmail_attachment).",
            input_schema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Gmail message ID (from search_gmail results)",
                    }
                },
                "required": ["message_id"],
            },
            source="integration:google",
            priority=0,
        ),
        ToolDefinition(
            name="get_gmail_attachment",
            description="Read a Gmail attachment and keep it as a file in the conversation. Returns the file's [[FILE:...]] marker and file_id (forward it to any tool that takes a file_id, e.g. send_gmail or upload_google_drive_file) and the text of a readable file inline (text, Word, PowerPoint, PDF, Excel, an attached email). Find attachments with get_gmail_message.",
            input_schema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Gmail message ID",
                    },
                    "part_id": {
                        "type": "string",
                        "description": "The attachment's part_id listed by get_gmail_message",
                        "default": "",
                    },
                    "attachment_name": {
                        "type": "string",
                        "description": "The attachment's filename, when the part_id is unknown",
                        "default": "",
                    },
                },
                "required": ["message_id"],
            },
            source="integration:google",
            priority=0,
        ),
        ToolDefinition(
            name="search_gmail",
            description="Search and list Gmail emails. Use Gmail query syntax (e.g., 'label:INBOX', 'from:user@example.com subject:meeting', 'is:unread', 'newer_than:2d'). To list recent emails from a label, use 'label:INBOX' or 'label:SENT'. Returns ID, subject, sender, date, preview, and labels for each email.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Gmail search query (e.g., 'label:INBOX', 'is:unread newer_than:7d', 'from:boss@company.com')",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                },
                "required": ["query"],
            },
            source="integration:google",
            priority=0,
        ),
        ToolDefinition(
            name="list_gmail_labels",
            description="List all Gmail labels (folders and categories). Returns label names and IDs. Use label IDs with search_gmail and modify_gmail_labels.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source="integration:google",
            priority=2,
        ),
        # ── Gmail Write ──
        ToolDefinition(
            name="send_gmail",
            description=f"Compose and send a new email via Gmail immediately. Attach files of this conversation with attachment_file_ids. For creating a draft to review first, use create_gmail_draft. For replying to an existing email, use reply_gmail.{gmail_signature}",
            input_schema={
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "Recipient email address(es), comma-separated",
                    },
                    "subject": {"type": "string", "description": "Email subject"},
                    "body": {
                        "type": "string",
                        "description": "Email body in Markdown or plain text, sent as HTML (lists, bold, links)",
                    },
                    "cc": {
                        "type": "string",
                        "description": "CC email addresses, comma-separated",
                        "default": "",
                    },
                    "bcc": {
                        "type": "string",
                        "description": "BCC email addresses, comma-separated",
                        "default": "",
                    },
                    "body_html": {
                        "type": "string",
                        "description": "Email body as HTML (optional, overrides plain text display)",
                        "default": "",
                    },
                    "attachment_file_ids": _ATTACHMENT_FILE_IDS_PROPERTY,
                },
                "required": ["to", "subject", "body"],
            },
            # The same message to the same recipients twice is refused.
            send_guard=SendGuard(recipients=("to", "cc", "bcc"), body=("subject", "body", "body_html")),
            read_back=WriteReadBack(
                kind="email",
                id_pattern=r"Message ID: ([^\s.]+)",
                recipients=("to", "cc", "bcc"),
                body=("body", "body_html"),
                shown_arguments=("subject",),
            ),
            source="integration:google",
            priority=0,
        ),
        ToolDefinition(
            name="create_gmail_draft",
            description=f"Create a draft email in Gmail (saved in Drafts, not sent). Attach files of this conversation with attachment_file_ids. Use send_gmail_draft to send it later.{gmail_signature}{draft_signature}",
            input_schema={
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "Recipient email address(es)",
                    },
                    "subject": {"type": "string", "description": "Email subject"},
                    "body": {
                        "type": "string",
                        "description": "Email body in Markdown or plain text, sent as HTML (lists, bold, links)",
                    },
                    "cc": {
                        "type": "string",
                        "description": "CC addresses",
                        "default": "",
                    },
                    "bcc": {
                        "type": "string",
                        "description": "BCC addresses",
                        "default": "",
                    },
                    "body_html": {
                        "type": "string",
                        "description": "HTML body (optional)",
                        "default": "",
                    },
                    "attachment_file_ids": _ATTACHMENT_FILE_IDS_PROPERTY,
                },
                "required": ["to", "subject", "body"],
            },
            source="integration:google",
            priority=0,
        ),
        ToolDefinition(
            name="send_gmail_draft",
            description=f"Send an existing draft email from Gmail.{send_draft_signature}",
            input_schema={
                "type": "object",
                "properties": {
                    "draft_id": {
                        "type": "string",
                        "description": "The draft ID (from create_gmail_draft output)",
                    }
                },
                "required": ["draft_id"],
            },
            source="integration:google",
            priority=1,
        ),
        ToolDefinition(
            name="reply_gmail",
            description=f"Reply to an existing Gmail email, preserving the thread. Fetches the original message for thread context, subject, and recipients. Supports reply-all and attaching files of this conversation (attachment_file_ids). Made on the newest message of the thread, own sent replies included.{gmail_signature}",
            input_schema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Gmail message ID to reply to",
                    },
                    "body": {
                        "type": "string",
                        "description": "Reply body in Markdown or plain text, sent as HTML (lists, bold, links)",
                    },
                    "reply_all": {
                        "type": "boolean",
                        "description": "If true, reply to all recipients (default false)",
                        "default": False,
                    },
                    "body_html": {
                        "type": "string",
                        "description": "HTML reply body (optional)",
                        "default": "",
                    },
                    "attachment_file_ids": _ATTACHMENT_FILE_IDS_PROPERTY,
                    "to": {
                        "type": "string",
                        "description": "Replaces the To recipients, comma-separated",
                        "default": "",
                    },
                    "cc": {
                        "type": "string",
                        "description": "Replaces the CC recipients, comma-separated",
                        "default": "",
                    },
                    "bcc": {"type": "string", "description": "BCC recipients, comma-separated", "default": ""},
                    "reply_to_exact_message": {
                        "type": "boolean",
                        "description": "Answer message_id itself, not the newest message of its thread (default false).",
                        "default": False,
                    },
                },
                "required": ["message_id", "body"],
            },
            source="integration:google",
            priority=0,
            # Artifact markers in the body are sent as links (outbound_markers).
            outbound_text=("body", "body_html"),
        ),
        ToolDefinition(
            name="get_gmail_email_signature",
            description=(
                "Retrieve the email signature the Gmail tools add to what this account sends, and where it "
                "comes from (the integration's signature integration). When there is none, it says why and "
                "how to set one up."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source="integration:google",
            priority=2,
        ),
        ToolDefinition(
            name="modify_gmail_labels",
            description="Add or remove labels from a Gmail message. Use to mark as read (remove UNREAD), star (add STARRED), archive (remove INBOX), etc. Use list_gmail_labels to see all available labels.",
            input_schema={
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "Gmail message ID"},
                    "add_labels": {
                        "type": "string",
                        "description": "Comma-separated label IDs to add (e.g. 'STARRED,IMPORTANT')",
                        "default": "",
                    },
                    "remove_labels": {
                        "type": "string",
                        "description": "Comma-separated label IDs to remove (e.g. 'UNREAD,INBOX')",
                        "default": "",
                    },
                },
                "required": ["message_id"],
            },
            source="integration:google",
            priority=1,
        ),
        ToolDefinition(
            name="trash_gmail",
            description="Move a Gmail email to the Trash.",
            input_schema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Gmail message ID to trash",
                    }
                },
                "required": ["message_id"],
            },
            source="integration:google",
            priority=1,
        ),
        # ── Calendar ──
        ToolDefinition(
            name="list_google_calendars",
            description="List all Google Calendars the user has access to, with IDs and access roles.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source="integration:google",
            priority=2,
        ),
        ToolDefinition(
            name="read_google_calendar",
            description="Read the calendar events of a window, past or future (default: the next 7 days). Returns event IDs, titles, times, locations, and attendees; an all-day event is marked '(all day)' with its dates, the last day included.",
            input_schema={
                "type": "object",
                "properties": {
                    "days_ahead": {
                        "type": "integer",
                        "description": "Days after now (or after start_datetime) the window ends, when end_datetime is omitted (default 7)",
                        "default": 7,
                    },
                    "days_back": {
                        "type": "integer",
                        "description": "Days before now the window starts, when start_datetime is omitted (default 0)",
                        "default": 0,
                    },
                    "start_datetime": {
                        "type": "string",
                        "description": "Start of the window, ISO 8601 (e.g. '2026-11-01' or '2026-11-01T08:00:00Z'); a date starts at 00:00 UTC, a time without offset is UTC. Omit for now - days_back.",
                        "default": "",
                    },
                    "end_datetime": {
                        "type": "string",
                        "description": "End of the window, ISO 8601; a date includes its whole day. Omit for start + days_ahead.",
                        "default": "",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max events (default 50)",
                        "default": 50,
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar ID (default 'primary')",
                        "default": "primary",
                    },
                },
                "required": [],
            },
            source="integration:google",
            priority=0,
        ),
        ToolDefinition(
            name="get_google_event",
            description="Get full details of a specific calendar event by ID, including whether it is all-day (with its dates, the last day included), attendees, RSVP status, description, conference link, and its attached Drive files (link and file_id).",
            input_schema={
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "Calendar event ID"},
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar ID (default 'primary')",
                        "default": "primary",
                    },
                },
                "required": ["event_id"],
            },
            source="integration:google",
            priority=1,
        ),
        ToolDefinition(
            name="create_google_event",
            description="Create a Google Calendar event. Can include attendees (invitations sent per send_updates, to every guest by default), location, and Google Meet conference. Times in ISO 8601 format. Attach files of this conversation with attachment_file_ids: each file is copied to a folder of the connected account's Google Drive and shared read-only with the attendees, and the event links that copy.",
            input_schema={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Event title"},
                    "start_time": {
                        "type": "string",
                        "description": "Start time in ISO 8601 (e.g. '2025-03-15T10:00:00'), or the first day (YYYY-MM-DD) of an all-day event",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "End time in ISO 8601, or the last day (YYYY-MM-DD, included) of an all-day event",
                    },
                    "all_day": _ALL_DAY_PROPERTY,
                    "description": {
                        "type": "string",
                        "description": "Event description",
                        "default": "",
                    },
                    "location": {
                        "type": "string",
                        "description": "Event location",
                        "default": "",
                    },
                    "attendees": {
                        "type": "string",
                        "description": "Comma-separated email addresses",
                        "default": "",
                    },
                    "timezone": {
                        "type": "string",
                        "description": "Timezone (default 'UTC')",
                        "default": "UTC",
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar ID (default 'primary')",
                        "default": "primary",
                    },
                    "conference": {
                        "type": "boolean",
                        "description": "Create Google Meet link (default false)",
                        "default": False,
                    },
                    "send_updates": _SEND_UPDATES_PROPERTY,
                    "attachment_file_ids": _EVENT_ATTACHMENT_FILE_IDS_PROPERTY,
                },
                "required": ["summary", "start_time", "end_time"],
            },
            # The event as Google Calendar now shows it.
            read_back=WriteReadBack(
                kind="event",
                identity=("event_id", "calendar_id"),
                id_pattern=r"ID: ([^\s.]+)",
                read_tool="get_google_event",
                fields=("Subject", "Start", "End", "Location", "Status", "Organizer", "Attendees"),
                body_label="Description",
            ),
            source="integration:google",
            priority=0,
        ),
        ToolDefinition(
            name="update_google_event",
            description="Update an existing Google Calendar event. Only provided fields are updated. Guests are notified per send_updates (every guest by default). attachment_file_ids adds files of this conversation next to the event's attachments: each file is copied to a folder of the connected account's Google Drive and shared read-only with the attendees. When the attendees change, the copies this tool attached follow them: added guests can read them, removed guests no longer can.",
            input_schema={
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "Event ID to update"},
                    "summary": {
                        "type": "string",
                        "description": "New event title",
                        "default": "",
                    },
                    "start_time": {
                        "type": "string",
                        "description": "New start time (ISO 8601), or the first day (YYYY-MM-DD) of an all-day event",
                        "default": "",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "New end time (ISO 8601), or the last day (YYYY-MM-DD, included) of an all-day event",
                        "default": "",
                    },
                    "all_day": {
                        **_ALL_DAY_PROPERTY,
                        "description": (
                            "true turns the event into an all-day one over start_time..end_time "
                            "(dates, end_time the last day included); false turns it back into a "
                            "timed event (date-times). Needs both start_time and end_time. Omit "
                            "to keep the event's form."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "New description",
                        "default": "",
                    },
                    "location": {
                        "type": "string",
                        "description": "New location",
                        "default": "",
                    },
                    "attendees": {
                        "type": "string",
                        "description": "Comma-separated emails (replaces existing)",
                        "default": "",
                    },
                    "timezone": {
                        "type": "string",
                        "description": "Timezone for start/end",
                        "default": "",
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar ID",
                        "default": "primary",
                    },
                    "send_updates": _SEND_UPDATES_PROPERTY,
                    "attachment_file_ids": _EVENT_ATTACHMENT_FILE_IDS_PROPERTY,
                },
                "required": ["event_id"],
            },
            # Only provided fields are updated: two updates of one event in a
            # round are one PATCH, one guest notification.
            coalesce=WriteCoalescing(
                identity=("event_id", "calendar_id"),
                mode="patch",
                union_lists=("attachment_file_ids",),
            ),
            read_back=WriteReadBack(
                kind="event",
                identity=("event_id", "calendar_id"),
                read_tool="get_google_event",
                fields=("Subject", "Start", "End", "Location", "Status", "Organizer", "Attendees"),
                body_label="Description",
            ),
            source="integration:google",
            priority=1,
        ),
        ToolDefinition(
            name="delete_google_event",
            description="Delete a Google Calendar event. Cancellation notices are sent to attendees. The Drive copies attached by create_google_event / update_google_event are deleted with it, so no former guest keeps reading them.",
            input_schema={
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "Event ID to delete"},
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar ID",
                        "default": "primary",
                    },
                },
                "required": ["event_id"],
            },
            source="integration:google",
            priority=1,
        ),
        # ── Drive ──
        ToolDefinition(
            name="list_google_drive",
            description="List files in a Google Drive folder (default: root). Returns file IDs, names, modification dates, sizes, and web links.",
            input_schema={
                "type": "object",
                "properties": {
                    "folder_id": {
                        "type": "string",
                        "description": "Google Drive folder ID (default 'root' for top-level)",
                        "default": "root",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max results (default 50)",
                        "default": 50,
                    },
                },
                "required": [],
            },
            source="integration:google",
            priority=2,
        ),
        ToolDefinition(
            name="search_google_drive",
            description="Search and list files in Google Drive. Use simple keywords or advanced Drive query syntax (e.g., \"name contains 'report'\", \"mimeType = 'application/pdf'\", \"'FOLDER_ID' in parents\"). To list all files in a folder, use \"'root' in parents\" or \"'FOLDER_ID' in parents\". Returns file IDs, names, types, modification dates, and sizes.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Drive search query (e.g., \"name contains 'report'\", \"'root' in parents\")",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                },
                "required": ["query"],
            },
            source="integration:google",
            priority=2,
        ),
        ToolDefinition(
            name="read_google_doc",
            description="Read content of a file by its Drive ID. A Google Doc comes back as its clean text (every tab; headings, lists and tables kept; pending suggestions shown inline as {++inserted++} / {--deleted--}) followed by its open comment threads in the list_google_doc_comments format; quote that text verbatim in add_google_doc_comment and edit_google_doc. Sheets / Slides are exported as text (with their open comment threads), text files are read directly, and Word / PowerPoint / PDF / Excel files have their text extracted; other binary or large files return metadata. Every answer carries the file's web, download and export links. To bring the file itself into the conversation, use download_google_drive_file.",
            input_schema={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "Google Drive file ID"},
                    "include_comments": {
                        "type": "boolean",
                        "description": "List the open comment threads of a Google Doc / Sheet / Slides after its text (default true)",
                        "default": True,
                    },
                },
                "required": ["file_id"],
            },
            source="integration:google",
            priority=1,
        ),
        ToolDefinition(
            name="download_google_drive_file",
            workspace_ingest=True,
            description="Download a Google Drive file's bytes as a reusable platform file with a download card ([[FILE:id]]) - use it to give the user the file itself or forward it to another tool via its file_id (send_gmail, upload_microsoft_drive_file, ...). Google Docs / Sheets / Slides / Drawings are exported (to docx / xlsx / pptx / png by default, or to export_format). The answer carries the file's links.",
            input_schema={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "Google Drive file ID"},
                    "export_format": {
                        "type": "string",
                        "description": "Optional export format for a Google Docs / Sheets / Slides / Drawings file: pdf, docx, xlsx, pptx, odt, ods, odp, rtf, txt, csv, tsv, md, epub, png, jpg or svg",
                        "default": "",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Optional filename override",
                        "default": "",
                    },
                },
                "required": ["file_id"],
            },
            source="integration:google",
            priority=1,
        ),
        ToolDefinition(
            name="upload_google_drive_file",
            description="Upload a REAL file (attached to, generated in, or downloaded into this conversation - pass its file_id) to Google Drive, byte-for-byte. Returns the new Drive file ID and its links.",
            input_schema={
                "type": "object",
                "properties": {
                    "file_id": {
                        "type": "string",
                        "description": "Platform file id (from an attached, generated or downloaded file)",
                    },
                    "folder_id": {
                        "type": "string",
                        "description": "Optional Drive folder ID to upload into (default: My Drive root)",
                        "default": "",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Optional filename override",
                        "default": "",
                    },
                },
                "required": ["file_id"],
            },
            source="integration:google",
            priority=2,
        ),
        # ── Google Docs ──
        *docs.definitions(),
        # ── Google Sheets ──
        *sheets.definitions(),
        # ── Google Chat ──
        ToolDefinition(
            name="list_google_chat_spaces",
            description="List Google Chat spaces (rooms and direct messages) the user belongs to. Returns space names (resource IDs) and display names. Use the space 'name' field for other Chat operations.",
            input_schema={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Max spaces (default 20)",
                        "default": 20,
                    }
                },
                "required": [],
            },
            source="integration:google",
            priority=2,
        ),
        ToolDefinition(
            name="read_google_chat_messages",
            description="Read recent messages from a Google Chat space, with their attached files (link, and the Drive file_id or Chat resource_name to read or save them). Use list_google_chat_spaces first to get the space_name.",
            input_schema={
                "type": "object",
                "properties": {
                    "space_name": {
                        "type": "string",
                        "description": "Full space resource name (e.g. 'spaces/AAAA123')",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max messages (default 20)",
                        "default": 20,
                    },
                },
                "required": ["space_name"],
            },
            source="integration:google",
            priority=1,
        ),
        ToolDefinition(
            name="send_google_chat_message",
            description="Send a text message to a Google Chat space, optionally with files of this conversation attached (attachment_file_ids). Use list_google_chat_spaces to find the space_name.",
            input_schema={
                "type": "object",
                "properties": {
                    "space_name": {
                        "type": "string",
                        "description": "Full space resource name (e.g. 'spaces/AAAA123')",
                    },
                    "message": {
                        "type": "string",
                        "description": "Message text to send",
                    },
                    "attachment_file_ids": _ATTACHMENT_FILE_IDS_PROPERTY,
                },
                "required": ["space_name", "message"],
            },
            source="integration:google",
            priority=0,
        ),
        ToolDefinition(
            name="download_google_chat_attachment",
            workspace_ingest=True,
            description="Save a file uploaded to Google Chat in the conversation as a reusable file ([[FILE:id]] + file_id) and read its text inline when readable. Pass the resource_name listed by read_google_chat_messages (a Drive attachment is read with read_google_doc / download_google_drive_file instead).",
            input_schema={
                "type": "object",
                "properties": {
                    "resource_name": {
                        "type": "string",
                        "description": "The attachment's resource_name from read_google_chat_messages",
                    },
                    "filename": {
                        "type": "string",
                        "description": "The attachment's name as listed by read_google_chat_messages",
                        "default": "",
                    },
                },
                "required": ["resource_name"],
            },
            source="integration:google",
            priority=1,
        ),
        ToolDefinition(
            name="create_google_chat_space",
            description="Create a new Google Chat space (named room). Requires Google Workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "display_name": {
                        "type": "string",
                        "description": "Space display name",
                    },
                    "space_type": {
                        "type": "string",
                        "enum": ["SPACE", "GROUP_CHAT"],
                        "description": "Space type (default 'SPACE')",
                        "default": "SPACE",
                    },
                    "description": {
                        "type": "string",
                        "description": "Space description",
                        "default": "",
                    },
                },
                "required": ["display_name"],
            },
            source="integration:google",
            priority=1,
        ),
        ToolDefinition(
            name="list_google_chat_members",
            description="List members of a Google Chat space with their roles.",
            input_schema={
                "type": "object",
                "properties": {
                    "space_name": {
                        "type": "string",
                        "description": "Full space resource name (e.g. 'spaces/AAAA123')",
                    }
                },
                "required": ["space_name"],
            },
            source="integration:google",
            priority=2,
        ),
        # ── Google Tasks ──
        ToolDefinition(
            name="list_google_task_lists",
            description="List all Google Tasks task lists with IDs.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source="integration:google",
            priority=2,
        ),
        ToolDefinition(
            name="list_google_tasks",
            description="List tasks from a Google Tasks list. Uses default list if no list_id.",
            input_schema={
                "type": "object",
                "properties": {
                    "list_id": {
                        "type": "string",
                        "description": "Task list ID (default '@default')",
                        "default": "@default",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max tasks (default 20)",
                        "default": 20,
                    },
                    "show_completed": {
                        "type": "boolean",
                        "description": "Include completed tasks (default false)",
                        "default": False,
                    },
                },
                "required": [],
            },
            source="integration:google",
            priority=1,
        ),
        ToolDefinition(
            name="create_google_task",
            description="Create a task in Google Tasks.",
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Task title"},
                    "list_id": {
                        "type": "string",
                        "description": "Task list ID (default '@default')",
                        "default": "@default",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Task notes/body",
                        "default": "",
                    },
                    "due_date": {
                        "type": "string",
                        "description": "Due date (YYYY-MM-DD)",
                        "default": "",
                    },
                },
                "required": ["title"],
            },
            source="integration:google",
            priority=1,
        ),
        ToolDefinition(
            name="complete_google_task",
            description="Mark a Google Task as completed.",
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID"},
                    "list_id": {
                        "type": "string",
                        "description": "Task list ID (default '@default')",
                        "default": "@default",
                    },
                },
                "required": ["task_id"],
            },
            source="integration:google",
            priority=1,
        ),
        ToolDefinition(
            name="update_google_task",
            description="Update a Google Task. Only provided fields are changed.",
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID"},
                    "list_id": {
                        "type": "string",
                        "description": "Task list ID (default '@default')",
                        "default": "@default",
                    },
                    "title": {
                        "type": "string",
                        "description": "New title",
                        "default": "",
                    },
                    "notes": {
                        "type": "string",
                        "description": "New notes",
                        "default": "",
                    },
                    "due_date": {
                        "type": "string",
                        "description": "New due date (YYYY-MM-DD)",
                        "default": "",
                    },
                },
                "required": ["task_id"],
            },
            source="integration:google",
            priority=1,
        ),
        ToolDefinition(
            name="delete_google_task",
            description="Delete a Google Task.",
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID"},
                    "list_id": {
                        "type": "string",
                        "description": "Task list ID (default '@default')",
                        "default": "@default",
                    },
                },
                "required": ["task_id"],
            },
            source="integration:google",
            priority=1,
        ),
    ]
