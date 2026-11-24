"""Outlook mail tools: read, search, flag, draft, reply and send email (with attachments)."""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import re
import time
from html import escape as _html_escape
from urllib.parse import quote as _url_encode
from urllib.parse import unquote as _url_decode

from app.integrations.file_content import (
    cut_inline,
    keep_file,
    kept_file_line,
    kilobytes,
    readable_text,
    rest_of_file_hint,
)
from app.integrations.http import TRANSIENT_RETRY_STATUSES, AutoRefreshClient, format_entity_list
from app.services import mail_send_guard
from app.services.agent_posts import (
    graph_recipient_addresses,
    record_mail_draft,
    record_mail_send,
)
from app.services.email_html import (
    build_graph_email_body,
    close_before_signature,
    markdown_to_email_html,
    starts_as_html_markup,
)
from app.services.email_signatures import (
    NO_SIGNATURE_INTEGRATION,
    MailSigner,
    append_to_body,
    register_email_sender,
    signature_text,
)
from app.tools.base import CallableToolExecutor, FileContext, SendGuard, ToolDefinition, ToolResult, WriteReadBack

from . import _send_confirm as send_confirm
from . import _signature_images as _sig_images
from ._attachment_dedupe import duplicates_line, remove_duplicate_file_attachments, removed_note
from ._body_window import format_body_window
from ._common import (
    _BASE_URL,
    _MS_MAX_FILE_BYTES,
    _PROVIDER,
    MicrosoftContext,
    _strip_html,
)
from ._draft_body import PREFER_HTML_BODY, html_draft_body
from ._internet_headers import format_internet_headers, header_section
from ._mail_window import next_page_token, page_params, pages_line, received_filter
from ._message_type import PLAIN_MESSAGE, message_type
from ._outlook import (
    _ATTACHMENT_FILE_IDS_PROPERTY,
    _attach_files,
    _body_links,
    _build_email_search_query,
    _calendar_url,
    _clean_message_id,
    _extract_signature_block,
    _format_outlook_attachments,
    _list_outlook_attachments,
    _resolve_attachment_files,
    _validate_message_id,
    sent_mail_signature,
    settle_message_attachments,
    signature_block,
)
from ._platform_sent import (
    PLATFORM_SENT_EXPAND,
    PLATFORM_SENT_STATE,
    is_platform_sent,
    platform_sent_header,
    stamp_platform_sent,
)
from ._raw_markup import raw_markup_report
from ._signature_images import (
    cid_references,
    dangling_mime_cids,
    drop_unreferenced_inline_images,
    embed_signature_images,
    inline_signature_images,
    remote_images,
    strip_images,
    unresolved_cids,
)
from ._thread_reply import (
    drop_forward_header,
    forward_thread_headers,
    forwarded_header,
    graph_recipients,
    held_thread,
    is_forward,
    latest_message,
    message_time,
    own_reply_recipients,
    parse_addresses,
    reply_subject,
    same_domain,
    sender_address,
    thread_recipients,
    with_extra_headers,
    with_recipient_headers,
    with_thread_headers,
)

logger = logging.getLogger(__name__)

_REPLY_CONTENT_TYPES = ("text", "html", "markdown")


def _RECIPIENTS_PROPERTY(what: str) -> dict[str, str]:  # noqa: N802 - reads as the other property constants
    """A comma-separated recipients property of a mail tool's schema."""
    return {"type": "string", "description": f"{what}: comma-separated email addresses (optional)."}

#: The escape of the send guard, on every tool that sends.
_FORCE_DUPLICATE_PROPERTY = {
    "type": "boolean",
    "description": (
        "Send even though this mailbox already sent a message in the same conversation within the guard window "
        "(default false: such a send is refused as a duplicate). Pass true only when a second message is really wanted."
    ),
    "default": False,
}

#: The escape of the reply-on-latest rule, on every reply tool.
_REPLY_TO_EXACT_MESSAGE_PROPERTY = {
    "type": "boolean",
    "description": "Answer message_id itself, not the newest message of its thread (default false).",
    "default": False,
}


def _SUBJECT_OVERRIDE_PROPERTY(built: str) -> dict[str, str]:  # noqa: N802 - reads as the other property constants
    """The subject override of a reply or forward tool's schema."""
    return {
        "type": "string",
        "description": (
            f"Subject of the message, replacing the '{built}' subject Outlook builds (optional). The quoted "
            "message and the signature above it are kept."
        ),
    }


def _subject_override(subject: object) -> str | None:
    """An explicit subject, one line and trimmed; None when not given or blank."""
    text = " ".join(str(subject or "").split())
    return text or None


# Outlook mail tools send email: each integration has an Email signature setting.
register_email_sender("microsoft")

#: What a draft created with the signature says. Graph does not tell
#: whether a client has the draft open, so the warning is given at creation.
_ADDIN_NOTE = (
    " It carries the user's signature: a signature add-in in the user's Outlook (such as Letsignit) may insert"
    " its own signature again while the draft is open, adding inline images on every save. For a draft the"
    " user will open and send from Outlook, create it with include_signature=false."
)
#: What a draft created without the signature says.
_UNSIGNED_DRAFT_NOTE = (
    " It carries no signature: Outlook's signature add-in adds it when the user opens the draft,"
    " and send_microsoft_draft adds it if an agent sends it."
)

#: Where the quoted thread of a reply starts in Outlook's HTML.
_QUOTED_THREAD = re.compile(r'<div[^>]*\bid\s*=\s*["\'](?:x_)?divRplyFwdMsg["\']', re.IGNORECASE)
_THREAD_SEPARATOR = re.compile(r"<hr\b[^>]*>", re.IGNORECASE)


def _own_part_end(html: str) -> tuple[int, bool]:
    """Where a draft's own part ends, and whether a reply's quoted thread follows it.

    A reply's own part ends at the ``<hr>`` separator above the quoted
    thread (the last one before it), a new email's at its end.
    """
    thread = _QUOTED_THREAD.search(html)
    if thread is None:
        return len(html), False
    separators = list(_THREAD_SEPARATOR.finditer(html, 0, thread.start()))
    return (separators[-1].start() if separators else thread.start()), True


def _draft_is_signed(html: str) -> bool:
    """Whether a draft's own part carries a ``<div id="Signature">`` block (a quoted one does not count)."""
    end, _ = _own_part_end(html)
    return bool(_extract_signature_block(html[:end]))


def _sign_draft_body(html: str, signature: str) -> str:
    """A draft's HTML with *signature* where Outlook puts it: above a reply's quoted thread, else last."""
    end, is_reply = _own_part_end(html)
    if not is_reply:
        return sign_above_quote(html, signature)
    return close_before_signature(html[:end]) + signature + "\r\n" + html[end:]


#: A quoted history an agent wrote into a new email's body: Outlook's
#: underscore separator, an "Original Message" line, or a "From:" header line
#: followed by a "Sent:" / "Date:" / "To:" / "Subject:" one.
_QUOTE_HEADER = r"(?:From|De)\s*:\s*(?:<[^>]*>|[^<\n]){1,300}?(?:<[^>]*>|\s)*(?:Sent|Date|Envoyé|To|À|Subject|Objet)\s*:"
_TEXT_QUOTE = re.compile(
    # A tag that opens the quote: a blockquote, or the rule Markdown makes of
    # an underscore line, followed by a "From:" header.
    r"(?P<tag><blockquote\b|<hr\b[^>]*>(?=(?:<[^>]*>|\s)*" + _QUOTE_HEADER + r"))"
    # Text that opens it, inside the block holding it.
    r"|_{16,}"
    r"|-{3,}\s*(?i:Original Message)\s*-{3,}"
    # A "From:" header that opens a line or a block (not prose that says "from:").
    r"|(?:(?<=>)|(?<=\n)|\A)[ \t]*" + _QUOTE_HEADER,
)
_BLOCK_OPEN = re.compile(r"<(?:p|div|blockquote|table)\b[^>]*>", re.IGNORECASE)
_NO_TEXT = re.compile(r"<[^>]+>|\s|&nbsp;")


def _text_quote_start(html: str) -> int | None:
    """Where a quoted history written into a body starts, ``None`` without one.

    A quote opened by text starts at the block holding that text. The email's
    own text must come first: a body that opens with the quote has none.
    """
    match = _TEXT_QUOTE.search(html)
    if match is None:
        return None
    start = match.start()
    if not match.group("tag"):
        opens = list(_BLOCK_OPEN.finditer(html, 0, start))
        if opens and not _NO_TEXT.sub("", html[opens[-1].end() : start]):
            start = opens[-1].start()
    body = re.search(r"<body[^>]*>", html[:start], re.IGNORECASE)
    if not _NO_TEXT.sub("", html[body.end() if body else 0 : start]):
        return None
    return start


def sign_above_quote(html: str, signature: str) -> str:
    """*html* with *signature* after its own text: above a quoted history it carries, else at its end.

    The sign-off right above the signature keeps no bottom margin: the
    signature follows it directly, as Outlook places one.
    """
    if not signature:
        return html
    start = _text_quote_start(html)
    if start is None:
        return append_to_body(close_before_signature(html), signature)
    return close_before_signature(html[:start]) + signature + "\r\n" + html[start:]


def _compose_block(body: str, kind: str, compose_style: str | None = None) -> str:
    """The text an agent writes above a quote, rendered as a new email's body is.

    ``text`` is Markdown whose tags stay literal: escaped first, then rendered,
    so its lists, emphasis, links and paragraphs show as such while a tag it
    mentions shows as written. ``markdown`` is rendered, ``html`` sanitized.
    Its text carries the integration's compose style (*compose_style*,
    ``email_html.compose_declarations``), the markup Outlook writes for a
    message the person composes, so dark mode shows it in the colour of the
    quoted thread under it.
    """
    if kind == "text":
        rendered = markdown_to_email_html(
            _html_escape(body.strip(), quote=False), assume_html=False, compose_style=compose_style
        )
    else:
        rendered = markdown_to_email_html(body, assume_html=kind == "html", compose_style=compose_style)
    return f"<div>{rendered}</div>\r\n"


def _inject_above_quote(existing_body: str, inject_block: str) -> str:
    """A native reply or forward draft's HTML with *inject_block* before its ``<hr>`` separator."""
    hr_match = re.search(r"(<hr\b[^>]*>)", existing_body, re.IGNORECASE)
    if hr_match:
        return existing_body[: hr_match.start()] + inject_block + existing_body[hr_match.start() :]
    body_tag = re.search(r"(<body[^>]*>)", existing_body, re.IGNORECASE)
    if body_tag:
        return existing_body[: body_tag.end()] + "\r\n" + inject_block + existing_body[body_tag.end() :]
    return (
        "<html><head>"
        '<meta http-equiv="Content-Type"'
        ' content="text/html; charset=utf-8">'
        "</head>"
        f'<body dir="ltr">\r\n'
        f"{inject_block}"
        f"{existing_body}\r\n"
        "</body></html>"
    )


def format_recipient_lines(message: dict) -> str:
    """The To, CC and BCC lines of a Graph message, then Reply-To when it names someone.

    Every line is written, ``(none)`` when the message has nobody there, so
    a message without Bcc is told from a read that did not ask for it. A
    received copy never carries its Bcc; a sent item or a draft does.
    """

    def addresses(field: str) -> str:
        found = [
            str((r.get("emailAddress") or {}).get("address") or "")
            for r in message.get(field) or []
            if isinstance(r, dict)
        ]
        return ", ".join(a for a in found if a) or "(none)"

    lines = [
        f"To: {addresses('toRecipients')}",
        f"CC: {addresses('ccRecipients')}",
        f"BCC: {addresses('bccRecipients')}",
    ]
    if (reply_to := addresses("replyTo")) != "(none)":
        lines.append(f"Reply-To: {reply_to}")
    return "\n".join(lines) + "\n"


def format_flagged_email(message: dict) -> str:
    """One flagged message as the full entry: sender, subject, date, conversation id and preview."""
    sender = (message.get("from") or {}).get("emailAddress") or {}
    return (
        f"[FLAGGED]\n"
        f"ID: {message.get('id', '')}\n"
        f"From: {sender.get('name', '')} <{sender.get('address', '')}>\n"
        f"Subject: {message.get('subject', '')}\n"
        f"Date: {message.get('receivedDateTime', '')}\n"
        f"Conversation ID: {message.get('conversationId', '')}\n"
        f"Preview: {(message.get('bodyPreview') or '')[:300]}"
    )


def format_flagged_email_line(message: dict) -> str:
    """One flagged message on one line: id | sender address | subject | received date | conversation id."""
    sender = (message.get("from") or {}).get("emailAddress") or {}
    subject = " ".join(str(message.get("subject") or "").split()).replace("|", "/")
    return " | ".join(
        (
            str(message.get("id", "")),
            str(sender.get("address", "")),
            subject,
            str(message.get("receivedDateTime", "")),
            str(message.get("conversationId", "")),
        )
    )


def format_email_line(message: dict) -> str:
    """One listed message on one line: id | sender | subject | received date | conversation id | state.

    The state names ``platform-sent`` when this platform composed the message
    (read with :data:`PLATFORM_SENT_EXPAND`); a message typed in Outlook has none.
    """
    sender = (message.get("from") or {}).get("emailAddress") or {}
    subject = " ".join(str(message.get("subject") or "").split()).replace("|", "/")
    name = " ".join(str(sender.get("name") or "").split()).replace("|", "/")
    address = str(sender.get("address") or "")
    state: list[str] = []
    if not message.get("isRead"):
        state.append("unread")
    if (message.get("flag") or {}).get("flagStatus") == "flagged":
        state.append("flagged")
    if message.get("importance") == "high":
        state.append("high")
    if is_platform_sent(message):
        state.append(PLATFORM_SENT_STATE)
    if (kind := message_type(message)) != PLAIN_MESSAGE:
        state.append(kind)
    return " | ".join(
        (
            str(message.get("id", "")),
            f"{name} <{address}>" if name else address,
            subject,
            str(message.get("receivedDateTime", "")),
            str(message.get("conversationId", "")),
            ",".join(state) or "read",
        )
    )


def register(ctx: MicrosoftContext, executor: CallableToolExecutor) -> list[ToolDefinition]:
    """Register the mail tools on *executor* and return their definitions."""
    _client = ctx.client
    _throttled_request = ctx.request
    persist_file_fn = ctx.persist_file_fn
    integration_id = ctx.integration_id

    # Normalize common folder name variations to Microsoft Graph well-known names
    folder_aliases: dict[str, str] = {
        "inbox": "inbox",
        "sent": "sentitems",
        "sentitems": "sentitems",
        "sent items": "sentitems",
        "drafts": "drafts",
        "deleted": "deleteditems",
        "deleteditems": "deleteditems",
        "junk": "junkemail",
        "junkemail": "junkemail",
        "archive": "archive",
    }

    # ── Send guard ────────────────────────────────────────────
    # One answer per conversation across the agent's runs: scoped to the
    # agent the tools act for. Without one (a send outside an agent's tools)
    # nothing is guarded: a person repeating a mail on purpose is not two
    # runs answering one inbound mail.
    # Keyed on the mailbox when the tools know it: two agents, or an
    # event run and a chat run, answering one thread from the same mailbox
    # share one lock and one record.
    guard_scope = (f"mailbox:{integration_id}" if integration_id else ctx.agent_id) if ctx.agent_id else None

    async def _sent_items_match(
        client: AutoRefreshClient, thread: str, *, now: float, window: int
    ) -> mail_send_guard.EarlierSend | None:
        """A message of this mailbox's Sent Items in *thread*, sent within the window.

        The listing is bounded to the window (Graph filters and orders on the
        same property, ``sentDateTime``) and matched here by conversation id
        or by the thread fingerprint (subject and recipients) of each copy. A
        failed read is logged by the request layer and reads as "nothing
        found": the guard's own record and lock still hold.
        """
        resp, err = await _throttled_request(
            client,
            "get",
            f"{_BASE_URL}/me/mailFolders/sentitems/messages",
            _PROVIDER,
            "check sent items",
            params={
                "$filter": f"sentDateTime ge {mail_send_guard.since_iso(now, window)}",
                "$orderby": "sentDateTime desc",
                "$top": 25,
                "$select": "id,subject,toRecipients,ccRecipients,sentDateTime,conversationId,webLink",
            },
        )
        if err or resp is None:
            return None
        try:
            messages = (resp.json() or {}).get("value") or []
        except ValueError:
            return None
        for message in messages:
            if not isinstance(message, dict):
                continue
            recipients = graph_recipient_addresses(message)
            keys = {
                mail_send_guard.thread_key(message.get("conversationId")),
                mail_send_guard.thread_fingerprint(message.get("subject"), recipients),
            }
            if thread not in keys:
                continue
            earlier = mail_send_guard.recent_send(
                sent_at=message.get("sentDateTime"),
                subject=message.get("subject"),
                recipients=recipients,
                message_id=message.get("id"),
                web_link=message.get("webLink"),
                conversation_id=message.get("conversationId"),
                now=now,
                window=window,
            )
            if earlier is not None:
                return earlier
        return None

    async def _inbound_since(
        client: AutoRefreshClient, conversation_id: str, earlier: mail_send_guard.EarlierSend
    ) -> bool | None:
        """Whether a message from someone else arrived in *conversation_id* after *earlier* was sent.

        One listing of the conversation's messages received after the earlier
        send; the mailbox's own address (``/me``, cached per session) is read
        only when such a message exists, to tell an inbound one from this
        mailbox's own copies. ``None`` when it cannot be told (no conversation,
        no send time, a failed read): the caller keeps the refusal.
        """
        if not conversation_id or earlier.sent_at is None:
            return None
        escaped = conversation_id.replace("'", "''")
        resp, err = await _throttled_request(
            client,
            "get",
            f"{_BASE_URL}/me/messages",
            _PROVIDER,
            "check the conversation for a newer message",
            params={
                "$filter": (
                    f"conversationId eq '{escaped}' and "
                    f"receivedDateTime gt {mail_send_guard.iso_instant(earlier.sent_at)}"
                ),
                "$top": 10,
                # Subject and headers tell an automatic (out-of-office) reply,
                # which is nothing new to answer.
                "$select": "from,sender,receivedDateTime,isDraft,subject,internetMessageHeaders",
            },
        )
        if err or resp is None:
            return None
        try:
            payload = resp.json() or {}
        except ValueError:
            return None
        messages = payload.get("value") if isinstance(payload, dict) else None
        if not isinstance(messages, list):
            return None
        if mail_send_guard.inbound_after(messages, after=earlier.sent_at, own_address="") is False:
            return False
        return mail_send_guard.inbound_after(messages, after=earlier.sent_at, own_address=await _own_address())

    async def _guard_send(
        client: AutoRefreshClient,
        *,
        tool: str,
        conversation_id: object,
        subject: object,
        recipients: list[str],
        force: bool,
    ) -> tuple[mail_send_guard.SendGuard, str | None]:
        """Take the thread's lock and look for an earlier send; ``(guard, refusal)``.

        A refusal is the tool's whole answer: nothing was sent and the lock is
        given back. ``force`` skips the lock and the checks (the send is still
        recorded, so the one after it is caught).
        """
        window = mail_send_guard.window_seconds()
        thread = mail_send_guard.thread_key(conversation_id, subject=subject, recipients=recipients)
        # A draft that could not be read names no thread: nothing to guard on
        # (every such draft would otherwise share one key).
        keyed = bool(str(conversation_id or "").strip()) or bool(recipients) or bool(str(subject or "").strip())
        guard = mail_send_guard.SendGuard(guard_scope if keyed else None, thread, window=window)
        if not guard.active or force:
            return guard, None
        if not await guard.acquire():
            return guard, mail_send_guard.contention_refusal(tool=tool)
        now = time.time()
        earlier = await guard.recorded()
        if earlier is None:
            earlier = await _sent_items_match(client, thread, now=now, window=window)
        if earlier is None:
            return guard, None
        # An earlier send is a duplicate only when nothing new arrived since:
        # a message from someone else after it is what this send answers.
        conversation = str(conversation_id or earlier.conversation_id or "").strip()
        if not conversation and earlier.source == "record":
            # A new email's record has no conversation id: its Sent Items copy does.
            copy = await _sent_items_match(client, thread, now=now, window=window)
            conversation = copy.conversation_id if copy is not None else ""
        moved_on = await _inbound_since(client, conversation, earlier)
        if moved_on:
            return guard, None
        await guard.release()
        return guard, mail_send_guard.refusal(
            earlier, window=window, tool=tool, now=now, inbound_checked=moved_on is False
        )

    async def _already_answered_note(conversation_id: object) -> str:
        """A warning for a reply draft in a conversation this agent already answered within the window.

        Read from the guard's own record only (no Graph request): the send
        itself is where the full check and the refusal happen.
        """
        if not guard_scope or not str(conversation_id or "").strip():
            return ""
        guard = mail_send_guard.SendGuard(
            guard_scope, mail_send_guard.thread_key(conversation_id), window=mail_send_guard.window_seconds()
        )
        earlier = await guard.recorded()
        if earlier is None:
            return ""
        return (
            f"\nWarning: this mailbox already sent a message in this conversation ({earlier.describe()}). "
            "send_microsoft_draft will refuse this draft as a duplicate unless a message from someone else "
            "arrived in the conversation since, or force_duplicate=true is passed."
        )

    # ── Signature helpers ─────────────────────────────────────────────

    # Per tool session: a run drafting many replies reads each signature once.
    _sig_cache: dict[str, str | None] = {"sent": None, "reply": None, "new": None, "address": None}
    # The integration's Email signature setting as resolved for this run.
    signer = MailSigner(ctx.signature_fn, ctx.signature_plan)

    async def _own_address() -> str:
        """The mailbox's address (``/me``), for the signature integration's lookup."""
        if _sig_cache["address"] is not None:
            return _sig_cache["address"]
        async with _client() as client:
            resp, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me",
                _PROVIDER,
                "read the mailbox address",
                params={"$select": "mail,userPrincipalName"},
            )
        me = resp.json() if not err and resp is not None else {}
        _sig_cache["address"] = str(me.get("mail") or me.get("userPrincipalName") or "").strip()
        return _sig_cache["address"]

    async def _latest_in_thread(message_id: str) -> tuple[str, dict | None]:
        """The message a reply to *message_id* is made on: the newest of its conversation.

        ``(id, latest)``: *latest* is the conversation's newest message (sent
        or received, any folder, drafts left out) when it is newer than
        *message_id*, else ``None`` and the id is *message_id*. A follow-up
        after this mailbox's own reply is then made on that reply, so its
        quote carries it. Any failed read keeps *message_id*: the lookup never
        stops a reply.
        """
        try:
            return await _read_latest_in_thread(message_id)
        except Exception:  # best effort: the reply goes on the message given
            logger.info("Could not read the conversation of a message to reply on its latest one", exc_info=True)
            return message_id, None

    async def _read_latest_in_thread(message_id: str) -> tuple[str, dict | None]:
        select = "id,conversationId,from,toRecipients,ccRecipients,receivedDateTime,sentDateTime,isDraft"
        async with _client() as client:
            given, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me/messages/{message_id}",
                _PROVIDER,
                "read the message answered",
                params={"$select": select},
            )
            meta = given.json() if not err and given is not None else None
            conversation = str((meta or {}).get("conversationId") or "") if isinstance(meta, dict) else ""
            if not conversation:
                return message_id, None
            listing, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me/messages",
                _PROVIDER,
                "list the conversation",
                params={
                    "$filter": f"conversationId eq '{conversation.replace(chr(39), chr(39) * 2)}'",
                    "$top": 50,
                    "$select": select,
                },
            )
        payload = listing.json() if not err and listing is not None else None
        messages = payload.get("value") if isinstance(payload, dict) else None
        latest = latest_message(messages) if isinstance(messages, list) else None
        if latest is None or latest.get("id") == message_id or message_time(latest) <= message_time(meta or {}):
            return message_id, None
        return str(latest["id"]), latest

    def _repointed_note(given_id: str, latest: dict) -> str:
        sender = sender_address(latest) or "unknown sender"
        return (
            f" Replied on the latest message of the thread ({sender}, {message_time(latest)}) instead of {given_id}: "
            "pass reply_to_exact_message=true to answer an older message."
        )

    async def _service_signature(reply: bool) -> str:
        """The person's signature from the integration's signature integration, as Outlook's block."""
        if not signer.signs:
            return ""
        found = await signer.read(await _own_address())
        return signature_block(found.for_reply(reply)) if found is not None else ""

    async def _get_signature_html(*, reply: bool = True) -> str:
        """The signature a reply (or, with a signature integration, a new email) carries.

        From the integration's signature integration when there is one:
        the person's current signature, its reply variant for
        a reply. A reply without one copies the signature of the sent mail; a
        new email without one carries none. With the integration's email
        signatures off, nothing carries one.
        """
        if not signer.enabled:
            return ""
        key = "reply" if reply else "new"
        if _sig_cache[key] is not None:
            return _sig_cache[key]
        signature = await _service_signature(reply)
        if not signature and reply:
            signature = await _sent_mail_signature()
        _sig_cache[key] = signature
        return signature

    async def _sent_mail_signature() -> str:
        """Fetch the user's Outlook signature from their recent sent mail.

        Scans up to 30 recent sent emails to find one that contains a
        Letsignit ``<div id="Signature">`` block.  A higher limit is
        needed because calendar responses, auto-replies, and test emails
        often lack the signature add-in markup.
        """
        if _sig_cache["sent"] is not None:
            return _sig_cache["sent"]

        async with _client() as client:
            resp, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me/mailFolders/sentitems/messages",
                _PROVIDER,
                "fetch sent emails for signature",
                params={
                    "$top": "30",
                    "$orderby": "sentDateTime desc",
                    "$select": "id,body",
                },
            )
            if err:
                _sig_cache["sent"] = ""
                return ""

            for msg in resp.json().get("value", []):
                body_html = msg.get("body", {}).get("content", "")
                if not body_html:
                    continue
                # Its own cid: images point at that sent email's attachments: left out.
                sig = sent_mail_signature(body_html)
                if sig:
                    _sig_cache["sent"] = sig
                    return sig

        _sig_cache["sent"] = ""
        return ""

    async def get_microsoft_email_signature() -> str:
        """Retrieve the user's Microsoft Outlook email signature, and where it comes from.

        From the integration's signature integration when there is one, else
        extracted from the most recent sent emails (Microsoft Graph has no
        signature endpoint), which replies carry. Says why there is none, and
        how to set one up, when nothing is found or signatures are off.
        """
        if not signer.enabled:
            return signer.report("", "")
        address = await _own_address() if signer.signs else ""
        from_service = await _service_signature(True)
        if from_service:
            return signer.report(address, from_service)
        sent = await _sent_mail_signature()
        why = (
            f"{signer.plan.source()} holds none for {address or 'this mailbox'}"
            if signer.signs
            else (signer.plan.missing or NO_SIGNATURE_INTEGRATION)
        ).rstrip(".")
        if sent:
            return (
                "User's email signature, copied from recent sent emails: replies carry it, "
                f"new emails carry none ({why}):\n{signature_text(sent)}"
            )
        return f"No email signature: {why}. None was found in recent sent emails either."

    async def _signed_body(body: str, content_type: str) -> tuple[dict[str, str], list[dict[str, object]]]:
        """A new email's body as Graph takes it, signed when a signature integration is read.

        The signature goes in after the body is normalized: the sanitizer would
        drop the block's id and styling, and :func:`signature_block` already
        cleaned it. It goes above a quoted history the body carries.
        Its remote images come back as the inline attachments the message's
        creation carries, each once, a draft's too.
        """
        built = build_graph_email_body(body, content_type, compose_style=ctx.compose_style)
        if not signer.signs:
            return built, []
        signature = await _get_signature_html(reply=False)
        if not signature:
            return built, []
        signed = sign_above_quote(built["content"], signature)
        content, images, _note = await inline_signature_images(signed, signature)
        return {**built, "content": content}, images

    async def _sign_unsigned_draft(client: AutoRefreshClient, draft_id: str, body: dict) -> tuple[str, str]:
        """Sign a draft whose own part carries no signature yet, before it is sent: a note, and its HTML now.

        Only from the signature integration (a draft is never given the sent
        mail's copy). A draft already signed (its own ``<div id="Signature">``
        block) is left as it is: never signed twice. A draft that cannot be
        signed is still sent.
        """
        content = str(body.get("content") or "")
        is_html = str(body.get("contentType") or "html").lower() == "html"
        if is_html and _draft_is_signed(content):
            return "", content
        if not is_html:
            content = build_graph_email_body(content, "text", compose_style=ctx.compose_style)["content"]
        _end, is_reply = _own_part_end(content)
        signature = await _service_signature(is_reply)
        if not signature:
            return "", content if is_html else ""
        signed = _sign_draft_body(content, signature)
        _, err = await _throttled_request(
            client,
            "patch",
            f"{_BASE_URL}/me/messages/{draft_id}",
            _PROVIDER,
            "sign draft",
            json={"body": {"contentType": "HTML", "content": signed}},
        )
        if err:
            return f" It was sent without the user's signature, which could not be added: {err}", ""
        return " The user's email signature was added before it was sent.", signed

    def _own_signature(content: str) -> str:
        """The ``<div id="Signature">`` block of a body's own part (a quoted one does not count)."""
        end, _ = _own_part_end(content)
        return _extract_signature_block(content[:end])

    def _needs_embedding(content: str) -> bool:
        return bool(remote_images(_own_signature(content)))

    async def _embed_and_patch_body(client: AutoRefreshClient, draft_id: str, content: str) -> tuple[str, str]:
        """Attach the remote images of a draft's signature inline once and write the body back: ``(body, note)``.

        An image already attached under its name is reused, never attached a
        second time, so a draft the tools sign then send carries one image set.
        The note is empty when nothing was left out; the body
        is the draft's as it now stands. The draft's other attachments are
        never touched.
        """
        block = _own_signature(content)
        if not remote_images(block):
            return content, ""
        message_url = f"{_BASE_URL}/me/messages/{draft_id}"
        updated, note = await embed_signature_images(_throttled_request, client, message_url, content, block)
        if updated == content:
            return content, note
        _, err = await _throttled_request(
            client,
            "patch",
            message_url,
            _PROVIDER,
            "embed signature images",
            json={"body": {"contentType": "HTML", "content": updated}},
        )
        if err:
            return content, f" Its signature images stay links: the draft could not be updated ({err})."
        return updated, note

    async def _signed_draft_html(client: AutoRefreshClient, draft_id: str) -> str | None:
        """A draft's HTML body as Exchange now holds it, or ``None`` (unreadable, or not HTML)."""
        resp, err = await _throttled_request(
            client,
            "get",
            f"{_BASE_URL}/me/messages/{draft_id}",
            _PROVIDER,
            "read signed draft",
            params={"$select": "body"},
            headers=PREFER_HTML_BODY,
        )
        if err or resp is None:
            return None
        try:
            body = (resp.json() or {}).get("body") or {}
        except ValueError:
            return None
        if str(body.get("contentType") or "html").lower() != "html":
            return None
        return str(body.get("content") or "")

    async def _own_images_unresolved(client: AutoRefreshClient, draft_id: str) -> tuple[str | None, list[str] | None]:
        """The draft's HTML and the ``cid:`` images of its own signature no inline attachment carries yet."""
        html = await _signed_draft_html(client, draft_id)
        if html is None:
            return None, None
        block = _own_signature(html)
        if not block:
            return html, []
        missing = await unresolved_cids(
            _throttled_request, client, f"{_BASE_URL}/me/messages/{draft_id}", block, provider=_PROVIDER
        )
        return html, missing

    async def _wait_for_own_images(client: AutoRefreshClient, draft_id: str) -> tuple[str | None, list[str] | None]:
        """Read the draft back until every image of its own signature resolves, pausing a few times."""
        html, missing = await _own_images_unresolved(client, draft_id)
        for delay in _sig_images.SETTLE_DELAYS:
            if missing == []:
                break
            await asyncio.sleep(delay)
            html, missing = await _own_images_unresolved(client, draft_id)
        return html, missing

    def _swap_block(html: str, old: str, new: str) -> str:
        start = html.find(old) if old else -1
        return html if start < 0 else html[:start] + new + html[start + len(old) :]

    async def _settle_signature_images(client: AutoRefreshClient, draft_id: str, content: str) -> str:
        """Hold the send until the images of the draft's own signature are on it: a note, empty when they were.

        The body PATCH and the inline-image POSTs are committed on the Exchange
        item a moment after they answer; a send issued at once went out with
        a body Exchange rebuilt without them (the signature's logo and icons
        missing). The draft is read back until every ``cid:`` of its own
        signature resolves to an inline attachment; still unresolved, the
        signature is written again and its images attached once more; still
        unresolved, the signature goes out with its images as links, never
        with references nothing resolves.
        """
        if not cid_references(_own_signature(content)):
            return ""
        html, missing = await _wait_for_own_images(client, draft_id)
        if missing == []:
            return ""
        if html is None or missing is None:
            logger.warning("The signature images of draft %s could not be checked before the send.", draft_id)
            return " Its signature images could not be checked before the send (the draft could not be read back)."
        message_url = f"{_BASE_URL}/me/messages/{draft_id}"
        _end, is_reply = _own_part_end(html)
        fresh = await _get_signature_html(reply=is_reply)
        own = _own_signature(html)
        if fresh and remote_images(fresh):
            rebuilt = _swap_block(html, own, fresh)
            _, err = await _throttled_request(
                client,
                "patch",
                message_url,
                _PROVIDER,
                "sign draft again",
                json={"body": {"contentType": "HTML", "content": rebuilt}},
            )
            if not err:
                await _embed_and_patch_body(client, draft_id, rebuilt)
                again, missing = await _wait_for_own_images(client, draft_id)
                if missing == []:
                    logger.info("The signature images of draft %s were attached again before the send.", draft_id)
                    return (
                        " Its signature images were attached again before the send:"
                        " the first set had not reached the message."
                    )
                html = again if again is not None else rebuilt
                own = _own_signature(html)
            linked = fresh
        else:
            linked = strip_images(own, missing or cid_references(own))
        final = _swap_block(html, own, linked)
        _, err = await _throttled_request(
            client,
            "patch",
            message_url,
            _PROVIDER,
            "sign draft with linked images",
            json={"body": {"contentType": "HTML", "content": final}},
        )
        logger.warning("The signature images of draft %s did not reach it; it is sent with them as links.", draft_id)
        if err:
            return (
                f" Its signature images could not be confirmed and the draft could not be updated ({err});"
                " it was sent as it stood."
            )
        await drop_unreferenced_inline_images(_throttled_request, client, message_url, final, provider=_PROVIDER)
        return (
            " Its signature images could not be confirmed on the message:"
            " the signature went out with its images as links."
        )

    # ── Email Read Tools ───────────────────────────────────────────────

    async def list_microsoft_emails(
        count: int = 10,
        folder: str = "inbox",
        include_preview: bool = True,
        received_from: str = "",
        received_to: str = "",
        page_token: str = "",
    ) -> str:
        """Read recent emails from Outlook; ``include_preview=False`` returns one line per message.

        ``received_from`` / ``received_to`` keep a received window; a page past
        the first is read with the ``page_token`` the previous answer names.
        """
        # Normalize folder name
        normalized = folder_aliases.get(folder.lower().strip(), folder)
        window, window_err = received_filter(received_from, received_to)
        if window_err:
            return window_err
        token_params: dict[str, str] = {}
        if (page_token or "").strip():
            parsed = page_params(page_token)
            if parsed is None:
                return "Error: page_token is not a page token of this listing; pass the one the previous answer names."
            token_params = parsed
        params = {
            "$top": str(max(1, int(count or 10))),
            "$orderby": "receivedDateTime desc",
            "$select": "id,subject,from,receivedDateTime,bodyPreview,isRead,importance,flag,conversationId",
            # Whether this platform composed each message, in the same call.
            "$expand": PLATFORM_SENT_EXPAND,
        }
        paged = bool(window or token_params)
        if window:
            params["$filter"] = window
        if paged:
            params["$count"] = "true"
        params.update(token_params)
        async with _client() as client:
            resp, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me/mailFolders/{normalized}/messages",
                _PROVIDER,
                "list emails",
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
            messages = data.get("value", [])
            token = next_page_token(data.get("@odata.nextLink"))
            footer: list[str] = []
            if paged or token:
                skip = int(token_params.get("$skip", "0") or 0)
                summary = pages_line(data.get("@odata.count"), skip, len(messages), int(params["$top"]))
                if summary:
                    footer.append(summary)
                if token:
                    footer.append(f'More messages: the same call with page_token="{token}".')
                elif paged:
                    footer.append("No more messages.")
            if not messages:
                return "\n".join([f"No emails found in '{normalized}' folder.", *footer])
            if not include_preview:
                header = (
                    f"{len(messages)} emails in '{normalized}': (id | from | subject | received | conversation id | state;"
                    f" {PLATFORM_SENT_STATE} = composed by this platform; meetingRequest, meetingCancelled,"
                    " meetingResponse = a meeting message)"
                )
                return "\n".join([header, *(format_email_line(m) for m in messages), *footer])
            lines = []
            for m in messages:
                fr = (m.get("from") or {}).get("emailAddress") or {}
                flag_status = (m.get("flag") or {}).get("flagStatus", "")
                tags = ""
                if not m.get("isRead"):
                    tags += "[UNREAD] "
                if m.get("importance") == "high":
                    tags += "[!] "
                if flag_status == "flagged":
                    tags += "[FLAGGED] "
                lines.append(
                    f"{tags}"
                    f"ID: {m.get('id', '')}\n"
                    f"From: {fr.get('name', '')} <{fr.get('address', '')}>\n"
                    f"Subject: {m.get('subject', '')}\n"
                    f"Date: {m.get('receivedDateTime', '')}\n"
                    f"Conversation ID: {m.get('conversationId', '')}\n"
                    f"Sent by platform: {'Yes' if is_platform_sent(m) else 'No'}\n"
                    f"Message type: {message_type(m)}\n"
                    f"Preview: {m.get('bodyPreview', '')[:200]}\n"
                )
            return "\n---\n".join(lines) + ("\n" + "\n".join(footer) if footer else "")

    async def get_microsoft_attachment(
        attachment_id: str,
        message_id: str = "",
        event_id: str = "",
        calendar_id: str = "",
        filename: str = "",
        file_context: FileContext | None = None,
    ) -> ToolResult | str:
        """Read an Outlook email or calendar-event attachment and keep it as a file.

        The bytes are saved in the conversation (a reusable file_id and a file
        chip), the text of a readable file (text, Word, PowerPoint, PDF,
        Excel, an attached email) is returned inline within the drive
        readers' budgets, and the email's or event's Outlook web link rides
        along.
        """
        att = _url_decode((attachment_id or "").strip())
        if not att:
            return (
                "Error: attachment_id is required. get_microsoft_email (or "
                "get_microsoft_calendar_event) lists each attachment with its "
                "attachment_id."
            )
        message_id = _clean_message_id(message_id or "")
        event_id = (event_id or "").strip()
        if bool(message_id) == bool(event_id):
            return (
                "Error: pass exactly one of message_id (an email's attachment) or "
                "event_id (a calendar event's attachment)."
            )
        if message_id:
            if err := _validate_message_id(message_id, "get_microsoft_attachment"):
                return err
            owner_url = f"{_BASE_URL}/me/messages/{message_id}"
            owner = "email"
        else:
            owner_url = _calendar_url(calendar_id, f"events/{_url_encode(event_id, safe='')}")
            owner = "calendar event"
        att_url = f"{owner_url}/attachments/{_url_encode(att, safe='')}"

        async with _client() as client:
            meta_resp, err = await _throttled_request(
                client,
                "get",
                att_url,
                _PROVIDER,
                "read attachment",
                params={"$select": "id,name,contentType,size,isInline"},
            )
            if err:
                return err
            meta = meta_resp.json()
            kind = meta.get("@odata.type") or ""
            name = filename.strip() or meta.get("name") or "outlook-attachment"
            if kind == "#microsoft.graph.referenceAttachment":
                return (
                    f"Attachment '{name}' is a link to a cloud file, not a copy: the "
                    f"{owner} does not hold its bytes. Its link is in the {owner}'s "
                    "body (get_microsoft_email / get_microsoft_calendar_event list "
                    "its links) - read the "
                    "file with get_microsoft_file_by_url or save it with "
                    "download_microsoft_file."
                )
            size = meta.get("size") or 0
            if size > _MS_MAX_FILE_BYTES:
                return (
                    f"Error: the attachment is {round(size / 1024 / 1024, 1)} MB, over "
                    f"the {_MS_MAX_FILE_BYTES // 1024 // 1024} MB download limit."
                )
            content_resp, err = await _throttled_request(
                client,
                "get",
                f"{att_url}/$value",
                _PROVIDER,
                "download attachment",
                follow_redirects=True,
            )
            if err:
                return err
            data = content_resp.content or b""
            link_resp, _link_err = await _throttled_request(
                client,
                "get",
                owner_url,
                _PROVIDER,
                f"read the {owner} link",
                params={"$select": "webLink"},
            )
            web_link = (link_resp.json() or {}).get("webLink", "") if link_resp else ""

        if not data:
            return "Error: the attachment is empty."
        if len(data) > _MS_MAX_FILE_BYTES:
            return (
                f"Error: the attachment is {round(len(data) / 1024 / 1024, 1)} MB, "
                f"over the {_MS_MAX_FILE_BYTES // 1024 // 1024} MB download limit."
            )
        if kind == "#microsoft.graph.itemAttachment":
            # An attached Outlook item comes back as MIME (an email), iCalendar
            # (an event) or vCard (a contact).
            head = data[:32].lstrip().upper()
            if head.startswith(b"BEGIN:VCALENDAR"):
                extension, mime = ".ics", "text/calendar"
            elif head.startswith(b"BEGIN:VCARD"):
                extension, mime = ".vcf", "text/vcard"
            else:
                extension, mime = ".eml", "message/rfc822"
            if not name.lower().endswith(extension):
                name = f"{name}{extension}"
        else:
            mime = (
                meta.get("contentType")
                or mimetypes.guess_type(name)[0]
                or "application/octet-stream"
            )

        file_ref = await keep_file(persist_file_fn, file_context, name, data, mime)
        text, limit, reason = await readable_text(data, name, mime)
        parts = [
            f"Attachment: {name} ({mime}, {kilobytes(len(data))}) from the {owner}.",
            kept_file_line(file_ref),
        ]
        if web_link:
            parts.append(f"Web link ({owner}): {web_link}")
        if text is not None:
            parts.append("Content:\n" + cut_inline(text, limit, rest_of_file_hint(file_ref)))
        elif mime.startswith("image/"):
            parts.append("It is an image: its pixels are attached to this result.")
        else:
            parts.append(f"Its content is not shown here: it is {reason}. {rest_of_file_hint(file_ref)}")
        return ToolResult(
            tool_call_id="",
            content="\n".join(parts),
            # An image reaches the model as pixels; readable text is inline above.
            files=[file_ref] if mime.startswith("image/") else [],
        )

    async def export_microsoft_email_mime(
        message_id: str,
        filename: str = "",
        file_context: FileContext | None = None,
    ) -> ToolResult | str:
        """Export an email's raw MIME (Graph ``/me/messages/{id}/$value``) as a ``.eml`` file.

        The file keeps the message exactly as Exchange holds it (every header,
        every part); the answer carries its header section whole, never cut.
        """
        message_id = _clean_message_id(message_id)
        if err := _validate_message_id(message_id, "export_microsoft_email_mime"):
            return err
        async with _client() as client:
            resp, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me/messages/{message_id}/$value",
                _PROVIDER,
                "export email MIME",
                follow_redirects=True,
            )
        if err:
            return err
        data = (resp.content if resp is not None else b"") or b""
        if not data:
            return "Error: Microsoft returned an empty message."
        if len(data) > _MS_MAX_FILE_BYTES:
            return (
                f"Error: the message is {round(len(data) / 1024 / 1024, 1)} MB, over the "
                f"{_MS_MAX_FILE_BYTES // 1024 // 1024} MB download limit."
            )
        name = (filename or "").strip() or "message"
        if not name.lower().endswith(".eml"):
            name = f"{name}.eml"
        file_ref = await keep_file(persist_file_fn, file_context, name, data, "message/rfc822")
        headers = header_section(data)
        return (
            f"Raw MIME of email {message_id}: {name} (message/rfc822, {kilobytes(len(data))}).\n"
            f"{kept_file_line(file_ref)}\n"
            f"Header section, whole ({len(headers)} characters):\n{headers}"
        )

    async def get_microsoft_email(
        message_id: str,
        include_raw: bool = False,
        raw_offset: int = 0,
        include_headers: bool = False,
        body_offset: int = 0,
    ) -> str:
        """Read a specific email by ID with full body content, recipients, and thread context.

        ``include_raw`` adds what a diagnosis of the message's markup needs:
        a window of the body's HTML from ``raw_offset``, a summary of
        its signature blocks, ``cid:`` references and add-in markers, and each
        attachment's content id and last modification time.
        ``include_headers`` adds the message's internet headers (bounded by
        ``format_internet_headers``): why a message reads as automated
        (List-Unsubscribe, Precedence, Auto-Submitted) is only in them.
        A long body is answered by window from ``body_offset``, each window
        naming the characters left and the offset of the next one
        (``format_body_window``): never cut in silence.
        """
        message_id = _clean_message_id(message_id)
        if err := _validate_message_id(message_id, "get_microsoft_email"):
            return err
        _select = "id,subject,from,toRecipients,ccRecipients,bccRecipients,replyTo,receivedDateTime,body,bodyPreview,isRead,importance,flag,conversationId,hasAttachments,webLink"
        if include_headers:
            _select += ",internetMessageHeaders"
        async with _client() as client:
            resp, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me/messages/{message_id}?$select={_select}&$expand={PLATFORM_SENT_EXPAND}",
                _PROVIDER,
                "read email",
            )
            if err:
                return err
            m = resp.json()
            fr = (m.get("from") or {}).get("emailAddress") or {}
            raw_body = (m.get("body") or {}).get("content", m.get("bodyPreview", ""))
            body_content = _strip_html(raw_body)
            tags = ""
            if not m.get("isRead"):
                tags += "[UNREAD] "
            if m.get("importance") == "high":
                tags += "[IMPORTANT] "
            flag_status = m.get("flag", {}).get("flagStatus", "")
            if flag_status == "flagged":
                tags += "[FLAGGED] "
            # ``hasAttachments`` ignores inline attachments (a pasted
            # screenshot): a ``cid:`` reference in the body is the only sign.
            attachments_block = ""
            if m.get("hasAttachments") or "cid:" in (raw_body or ""):
                attachments, att_err = await _list_outlook_attachments(
                    ctx,
                    client, f"{_BASE_URL}/me/messages/{message_id}"
                )
                if att_err:
                    attachments_block = f"Attachments: could not be listed ({att_err})\n"
                elif attachments:
                    duplicates = duplicates_line(attachments)
                    attachments_block = (
                        _format_outlook_attachments(
                            attachments,
                            "Read one inline and save it as a file with "
                            "get_microsoft_attachment, passing this email's ID as "
                            "message_id and the attachment_id.",
                        )
                        + "\n"
                        + (f"{duplicates}; send_microsoft_draft removes the extra copies.\n" if duplicates else "")
                    )
            raw_block = await _raw_email_block(client, message_id, raw_body, raw_offset) if include_raw else ""
            kind = await _meeting_kind(client, message_id, m)
            web_link = m.get("webLink") or ""
            links = _body_links(raw_body)
            links_block = (
                "Links in the body:\n" + "\n".join(f"- {url}" for url in links) + "\n"
                if links
                else ""
            )
            return (
                f"{tags}\n"
                f"ID: {m.get('id', '')}\n"
                f"From: {fr.get('name', '')} <{fr.get('address', '')}>\n"
                + format_recipient_lines(m)
                + f"Subject: {m.get('subject', '')}\n"
                f"Date: {m.get('receivedDateTime', '')}\n"
                f"Has Attachments: {'Yes' if m.get('hasAttachments') else 'No'}\n"
                f"Sent by platform: {'Yes' if is_platform_sent(m) else 'No'}\n"
                f"Message type: {kind}\n"
                + (f"Web link: {web_link}\n" if web_link else "")
                + attachments_block
                + links_block
                + format_body_window(body_content, body_offset)
                + raw_block
                + (format_internet_headers(m.get("internetMessageHeaders")) if include_headers else "")
            )

    async def _meeting_kind(client: AutoRefreshClient, message_id: str, m: dict) -> str:
        """The message type of *m*; a meeting response is read whole once for its verdict.

        ``meetingMessageType`` belongs to the derived event message type, which a
        ``$select`` on messages cannot name: a request and a cancellation are told
        by ``@odata.type`` alone, an answer (accepted, tentative, declined) costs
        one more read, on that message only.
        """
        kind = message_type(m)
        if kind != "meetingResponse":
            return kind
        resp, err = await _throttled_request(
            client, "get", f"{_BASE_URL}/me/messages/{message_id}", _PROVIDER, "read the meeting response"
        )
        if err or resp is None:
            return kind
        return message_type({**m, "meetingMessageType": resp.json().get("meetingMessageType")})

    async def _raw_email_block(client: AutoRefreshClient, message_id: str, raw_body: str, offset: int) -> str:
        """The raw part of ``get_microsoft_email(include_raw=True)``: markup summary, attachment details, HTML window."""
        url = f"{_BASE_URL}/me/messages/{message_id}/attachments"
        resp, err = await _throttled_request(
            client, "get", url, _PROVIDER, "list attachment details",
            params={"$select": "id,name,contentType,size,isInline,lastModifiedDateTime"},
        )
        listed = [] if err or resp is None else (resp.json() or {}).get("value", [])
        details: list[dict] = []
        for att in listed:
            cid = ""
            if att.get("isInline") and att.get("id"):
                # Graph gives the content id on a file attachment read alone, and
                # without $select: contentId is a fileAttachment property, which a
                # $select on the attachment base type cannot name (the read failed
                # and the report said "content id none" on every image).
                one, one_err = await _throttled_request(
                    client, "get", f"{url}/{_url_encode(str(att['id']), safe='')}", _PROVIDER,
                    "read attachment content id",
                )
                if not one_err and one is not None:
                    cid = str((one.json() or {}).get("contentId") or "")
            details.append({**att, "contentId": cid})
        return raw_markup_report(raw_body, details, offset, err)

    async def search_microsoft_emails(query: str, count: int = 10) -> str:
        """Search emails by subject or body content across ALL folders (inbox, sent, drafts, etc.).

        WARNING: This searches every folder including Drafts. To check
        only sent emails, use list_microsoft_emails(folder='sentitems') instead.
        """
        if count < 1:
            count = 10
        _select = "id,subject,from,receivedDateTime,bodyPreview"
        async with _client() as client:
            resp, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me/messages",
                _PROVIDER,
                "search emails",
                params={
                    "$search": _build_email_search_query(query),
                    "$top": str(count),
                    "$select": _select,
                },
            )
            if err:
                return err
            messages = resp.json().get("value", [])
            if not messages:
                return f"No emails matching '{query}'."
            lines = []
            for m in messages:
                fr = (m.get("from") or {}).get("emailAddress") or {}
                lines.append(
                    f"ID: {m.get('id', '')}\n"
                    f"From: {fr.get('name', '')} <{fr.get('address', '')}>\n"
                    f"Subject: {m.get('subject', '')}\n"
                    f"Date: {m.get('receivedDateTime', '')}\n"
                    f"Message type: {message_type(m)}\n"
                    f"Preview: {m.get('bodyPreview', '')[:200]}"
                )
            return "\n---\n".join(lines)

    async def list_microsoft_flagged_emails(count: int = 50, include_preview: bool = True) -> str:
        """Get all emails flagged for follow-up in Outlook. The triage assignment flags emails needing a reply, and the drafting assignment processes them. Uses pagination to retrieve all matching emails, sorted oldest first for queue processing."""
        async with _client() as client:
            all_messages: list[dict] = []
            url: str | None = f"{_BASE_URL}/me/messages"
            params: dict | None = {
                "$filter": "flag/flagStatus eq 'flagged'",
                "$top": str(min(count, 50)),
                "$select": "id,subject,from,receivedDateTime,bodyPreview,flag,conversationId",
            }
            # Note: $orderby is incompatible with flag/flagStatus filter — sort client-side
            while url and len(all_messages) < count:
                kw: dict = {}
                if params:
                    kw["params"] = params
                resp, err = await _throttled_request(
                    client,
                    "get",
                    url,
                    _PROVIDER,
                    "get flagged emails",
                    **kw,
                )
                if err:
                    return err
                data = resp.json()
                all_messages.extend(data.get("value", []))
                url = data.get("@odata.nextLink")
                params = None  # nextLink already includes query parameters

            if not all_messages:
                return "No flagged emails found."

            # Sort oldest first — process oldest emails first
            all_messages.sort(key=lambda m: m.get("receivedDateTime", ""))
            result_messages = all_messages[:count]

            listing = format_entity_list(
                result_messages,
                format_flagged_email if include_preview else format_flagged_email_line,
                noun="flagged email",
                total=len(all_messages),
                empty_msg="No flagged emails found.",
            )
            if include_preview:
                return listing
            header, _, body = listing.partition("\n")
            return f"{header} (id | from | subject | received | conversation id)\n{body}"

    # ── Email Write Tools ──────────────────────────────────────────────

    async def _set_flag_status(message_id: str, status: str, tool_name: str, op: str) -> tuple[str, str]:
        """PATCH a message's follow-up flag, then read it back from the mailbox.

        Graph can answer a PATCH with success on an id form that does not
        address the message the mailbox lists (a short id taken from another
        account's change notification), leaving the flag unchanged. The flag
        is therefore read back on the id Graph returned, and a write that did
        not land is an error, never a success. Returns ``(error, mailbox_id)``.
        """
        async with _client() as client:
            resp, err = await _throttled_request(
                client,
                "patch",
                f"{_BASE_URL}/me/messages/{message_id}",
                _PROVIDER,
                op,
                json={"flag": {"flagStatus": status}},
            )
            if err:
                return err, message_id
            try:
                patched = resp.json() if resp is not None else {}
            except ValueError:
                patched = {}
            mailbox_id = str((patched or {}).get("id") or message_id)
            back, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me/messages/{mailbox_id}",
                _PROVIDER,
                f"read back {op}",
                params={"$select": "id,flag"},
            )
            if err:
                return (
                    f"Error: {tool_name} sent the change but could not read message {message_id} back "
                    f"to confirm it ({err}). Use the id search_microsoft_emails or list_microsoft_emails "
                    "returns and call again."
                ), mailbox_id
            try:
                read = back.json() if back is not None else {}
            except ValueError:
                read = {}
            got = str(((read or {}).get("flag") or {}).get("flagStatus") or "")
            if got != status:
                return (
                    f"Error: {tool_name} was accepted by Microsoft Graph but message {message_id} reads back "
                    f"with flag status {got or 'unknown'!r}, not {status!r}: the id does not address the "
                    "message this mailbox lists (a short or foreign id form). Use the id "
                    "search_microsoft_emails or list_microsoft_emails returns and call again."
                ), mailbox_id
            return "", mailbox_id

    async def flag_microsoft_email(message_id: str) -> str:
        """Flag an email in Outlook for follow-up. Used by the triage assignment to mark emails that need a reply — the drafting assignment then picks them up via list_microsoft_flagged_emails. Also useful for manually queuing emails for reply drafting."""
        message_id = _clean_message_id(message_id)
        if err := _validate_message_id(message_id, "flag_microsoft_email"):
            return err
        err, mailbox_id = await _set_flag_status(message_id, "flagged", "flag_microsoft_email", "flag email")
        if err:
            return err
        return f"Email {mailbox_id} flagged for follow-up in Outlook (read back: flagged)."

    async def unflag_microsoft_email(message_id: str) -> str:
        """Remove the flag from an email after a draft reply has been created. Clears the Outlook follow-up flag so it no longer appears in the flagged queue."""
        message_id = _clean_message_id(message_id)
        if err := _validate_message_id(message_id, "unflag_microsoft_email"):
            return err
        err, mailbox_id = await _set_flag_status(message_id, "notFlagged", "unflag_microsoft_email", "unflag email")
        if err:
            return err
        return f"Email {mailbox_id} unflagged (flag cleared, read back: notFlagged)."

    async def mark_microsoft_email_read(message_id: str) -> str:
        """Mark an email as read."""
        message_id = _clean_message_id(message_id)
        if err := _validate_message_id(message_id, "mark_microsoft_email_read"):
            return err
        async with _client() as client:
            _, err = await _throttled_request(
                client,
                "patch",
                f"{_BASE_URL}/me/messages/{message_id}",
                _PROVIDER,
                "mark email read",
                json={"isRead": True},
            )
            if err:
                return err
            return f"Email {message_id} marked as read."

    async def delete_microsoft_draft(draft_id: str) -> str:
        """Delete a draft from the mailbox, and only a draft.

        The message is read first: anything Graph does not report as
        ``isDraft`` (a received or sent email) is refused untouched. A deleted
        draft goes to Deleted Items, where Outlook keeps it until it is emptied.
        """
        draft_id = _clean_message_id(draft_id)
        if err := _validate_message_id(draft_id, "delete_microsoft_draft"):
            return err
        message_url = f"{_BASE_URL}/me/messages/{draft_id}"
        async with _client() as client:
            resp, err = await _throttled_request(
                client,
                "get",
                message_url,
                _PROVIDER,
                "read draft",
                params={"$select": "id,isDraft,subject,createdDateTime"},
            )
            if err or resp is None:
                return err or f"Error: draft {draft_id} could not be read; nothing was deleted."
            message = resp.json()
            subject = message.get("subject") or "(no subject)"
            created = message.get("createdDateTime") or "unknown time"
            if message.get("isDraft") is not True:
                return (
                    f"Error: message {draft_id} ('{subject}') is not a draft; delete_microsoft_draft "
                    "deletes drafts only. Nothing was deleted."
                )
            _, err = await _throttled_request(client, "delete", message_url, _PROVIDER, "delete draft")
            if err:
                return err
            return (
                f"Draft {draft_id} deleted. Subject: '{subject}'. Created: {created}. "
                "It is in Deleted Items until that folder is emptied."
            )

    async def create_microsoft_draft_reply(
        message_id: str,
        body: str,
        reply_all: bool = False,
        content_type: str = "text",
        attachment_file_ids: list[str] | str | None = None,
        file_context: FileContext | None = None,
        to: str | list[str] | None = None,
        cc: str | list[str] | None = None,
        bcc: str | list[str] | None = None,
        reply_to_exact_message: bool = False,
        subject: str | None = None,
    ) -> str:
        """Create a draft reply to an email (createReply + PATCH); see :func:`_draft_reply`.

        The reply is made on the newest message of the conversation (sent or
        received) unless ``reply_to_exact_message``: a follow-up after this
        mailbox's own reply quotes that reply. Made on this mailbox's own sent
        message, it goes to that message's recipients (To and Cc), never to
        the mailbox itself. ``subject`` replaces the ``RE:`` subject Outlook
        built; the quote and the signature above it are kept.
        """
        given_id = _clean_message_id(message_id)
        latest = None
        final_to = parse_addresses(to) or None
        final_cc = parse_addresses(cc) if cc is not None else None
        known_kind = str(content_type or "text").strip().lower() in _REPLY_CONTENT_TYPES
        if (
            not reply_to_exact_message
            and known_kind
            and not _validate_message_id(given_id, "create_microsoft_draft_reply")
        ):
            message_id, latest = await _latest_in_thread(given_id)
            own = await _own_address() if latest is not None else ""
            if latest is not None and own and sender_address(latest).lower() == own.lower():
                own_to, own_cc = own_reply_recipients(latest, own_address=own)
                final_to = final_to or own_to or None
                if final_cc is None and own_cc:
                    final_cc = own_cc
        draft_id, answer = await _draft_reply(
            message_id,
            body,
            reply_all=reply_all,
            content_type=content_type,
            attachment_file_ids=attachment_file_ids,
            file_context=file_context,
            to=final_to,
            cc=final_cc,
            bcc=parse_addresses(bcc) if bcc is not None else None,
            subject=_subject_override(subject),
        )
        if draft_id and latest is not None:
            answer += _repointed_note(given_id, latest)
        return answer

    async def _draft_reply(
        message_id: str,
        body: str,
        *,
        reply_all: bool = False,
        content_type: str = "text",
        attachment_file_ids: list[str] | str | None = None,
        file_context: FileContext | None = None,
        to: list[str] | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        subject: str | None = None,
        keep_forward_header: bool = True,
    ) -> tuple[str, str]:
        """Create a draft reply to an email (createReply + PATCH): ``(draft id or "", the tool's answer)``.

        Uses a three-step approach that preserves Outlook's native HTML
        structure and correctly positions the email signature for
        compatibility with signature add-ins (e.g. Letsignit):

        1. ``createReply`` with an empty body to obtain the native draft
           structure including the quoted conversation thread.
        2. Fetch the user's email signature: the reply signature of the
           signature integration bound alongside when there is one,
           else the ``<div id="Signature">`` block of recent sent mail.
        3. Inject reply text + signature before the ``<hr>`` separator
           and ``PATCH`` the draft.

        The signature is included WITH its ``id="Signature"`` wrapper so
        that Letsignit recognises it on open and updates it in place
        rather than appending a duplicate at the bottom.

        ``content_type`` says how ``body`` is rendered. ``"text"`` (the
        default) wraps each reply line, escaped, in its own ``<div>`` with
        Outlook's default font to match native composition behaviour.
        ``"html"`` and ``"markdown"`` go through the normalizer
        ``send_microsoft_email`` uses (Markdown converted, the result
        sanitized), in one ``<div>`` with that font. Either way the
        ``createReply`` draft keeps its conversation and quoted thread: only
        its body is patched.

        Do NOT include the signature in ``body``.

        ``attachment_file_ids`` attaches files of this conversation
        byte-for-byte to the reply draft.

        ``to`` replaces the reply's To; ``cc`` and ``bcc``, when given (an
        empty list clears them), replace its Cc and Bcc. ``subject``
        replaces its subject; ``keep_forward_header=False`` drops the header
        and comment of a forward from the quote.
        """
        message_id = _clean_message_id(message_id)
        if err := _validate_message_id(message_id, "create_microsoft_draft_reply"):
            return "", err
        kind = str(content_type or "text").strip().lower()
        if kind not in _REPLY_CONTENT_TYPES:
            return "", (
                f"Error: content_type must be 'text', 'html' or 'markdown', not {content_type!r}. "
                "No draft was created."
            )
        files, err = await _resolve_attachment_files(attachment_file_ids, file_context)
        if err:
            return "", err
        async with _client() as client:
            endpoint = "createReplyAll" if reply_all else "createReply"

            # ── Step 1: Create an empty reply draft ─────────────────────
            create_resp, err = await _throttled_request(
                client,
                "post",
                f"{_BASE_URL}/me/messages/{message_id}/{endpoint}",
                _PROVIDER,
                "create draft reply",
                json={},
                headers=PREFER_HTML_BODY,
            )
            if err or create_resp is None:
                return "", err or "Error: Outlook answered nothing; no draft was created."
            draft = create_resp.json()
            draft_id = draft.get("id", "")
            if not draft_id:
                return "", f"Error: Draft creation returned no ID for email {message_id}."
            await record_mail_draft(draft_id)

            # The quote Outlook built, always as HTML: a draft answering a
            # plain-text message comes back as text, whose quote collapsed
            # into one line under an HTML body.
            existing_body = await html_draft_body(
                _throttled_request, client, f"{_BASE_URL}/me/messages/{draft_id}", draft
            )

            # ── Step 2: Build reply HTML ─────────────────────────────────
            reply_block = _compose_block(body, kind, ctx.compose_style)

            # ── Step 3: Fetch signature & inject before <hr> ─────────────
            # The signature is the full <div id="Signature">...</div> block,
            # from the signature integration or extracted from recent sent
            # emails.  Including it with id="Signature" ensures:
            #   - Graph API preserves it in place during PATCH
            #   - Letsignit recognises it on open and updates in-place
            #     (instead of appending a duplicate at the bottom)
            sig_html = await _get_signature_html()

            inject_block = reply_block
            if sig_html:
                # The sign-off keeps no bottom margin: the signature follows it directly.
                inject_block = close_before_signature(reply_block) + sig_html + "\r\n"

            quote_cut = False
            if not keep_forward_header:
                cut = drop_forward_header(existing_body)
                quote_cut = cut != existing_body
                existing_body = cut
            new_body = _inject_above_quote(existing_body, inject_block)

            # ── Step 4: PATCH the draft ──────────────────────────────────
            # The signature's images are attached inline once right after:
            # a draft left with the service's links had them embedded
            # again by the client on each save, up to twenty copies of each.
            patch: dict[str, object] = stamp_platform_sent({"body": {"contentType": "HTML", "content": new_body}})
            if to:
                patch["toRecipients"] = graph_recipients(to)
                draft["toRecipients"] = patch["toRecipients"]
            if cc is not None:
                patch["ccRecipients"] = graph_recipients(cc)
            if bcc is not None:
                patch["bccRecipients"] = graph_recipients(bcc)
            if subject:
                patch["subject"] = subject
            _, err = await _throttled_request(
                client,
                "patch",
                f"{_BASE_URL}/me/messages/{draft_id}",
                _PROVIDER,
                "patch draft reply",
                json=patch,
            )
            if err:
                return "", err
            image_note = ""
            if sig_html and _needs_embedding(new_body):
                new_body, image_note = await _embed_and_patch_body(client, draft_id, new_body)
            if quote_cut:
                # createReply copied the inline images of the forward note it
                # quoted (the forwarder's signature): the note is gone, so are
                # they, or the reply carries a second image set.
                await drop_unreferenced_inline_images(
                    _throttled_request, client, f"{_BASE_URL}/me/messages/{draft_id}", new_body, prefix=""
                )

            attached_note = ""
            if files:
                attached, err = await _attach_files_to_message(client, draft_id, files)
                if err:
                    return "", _attach_failure(err, draft_id, attached, sent=False)
                attached_note = f" Attachments: {', '.join(attached)}."
                attached_note += await settle_message_attachments(ctx, client, f"{_BASE_URL}/me/messages/{draft_id}")
            attached_note += image_note

            to_recipients = draft.get("toRecipients", [])
            to_str = ", ".join(
                r.get("emailAddress", {}).get("address", "") for r in to_recipients[:3]
            )
            literal_note = (
                "\nWarning: body looks like HTML but content_type=text; its tags are sent "
                "literally and show as text in the draft. Pass content_type='html' to render it."
                if kind == "text" and starts_as_html_markup(body)
                else ""
            )
            cc_note = f" Cc: {', '.join(cc)}." if cc else ""
            already_note = await _already_answered_note(draft.get("conversationId"))
            return draft_id, (
                f"Draft reply created for email {message_id}. Draft ID: {draft_id}. "
                f"To: {to_str}.{cc_note} Full conversation thread preserved in draft. "
                f"Draft is saved in Drafts folder — NOT sent.{attached_note}{literal_note}{already_note}"
            )

    # ── Email Send Tools ───────────────────────────────────────────────

    async def _attach_files_to_message(
        client: AutoRefreshClient,
        message_id: str,
        files: list[tuple[str, bytes, str, str]],
    ) -> tuple[list[str], str | None]:
        """Attach *files* byte-for-byte to a draft. Returns ``(attached, error)``."""
        return await _attach_files(ctx, client, f"{_BASE_URL}/me/messages/{message_id}", files)

    def _attach_failure(err: str, draft_id: str, attached: list[str], *, sent: bool) -> str:
        so_far = ", ".join(attached) if attached else "none"
        status = "The email was NOT sent: its draft" if sent else "The draft"
        return (
            f"{err}\n{status} (Draft ID: {draft_id}) is in the Drafts folder with "
            f"these attachments so far: {so_far}."
        )

    async def _settle_send(
        client: AutoRefreshClient,
        err: str,
        *,
        started: float,
        subject: object,
        recipients: list[str],
        internet_message_id: object = "",
    ) -> tuple[str, str | None]:
        """After a send answered by *err*: ``(note, error)``; see :mod:`._send_confirm`.

        A 5xx or a timeout on the send leaves open that Graph delivered it:
        Sent Items is read (never the send retried). A copy found answers
        ``(note, None)``, the send then counted as made; none answers the
        explicit "NOT sent", an unreadable Sent Items "Send state UNKNOWN".
        Any other error is answered as it is.
        """
        if not send_confirm.is_ambiguous_send_error(err):
            return "", err
        if not send_confirm.can_match(subject=subject, recipients=recipients, internet_message_id=internet_message_id):
            return "", send_confirm.unknown_answer(err, "the message has no id, subject or recipient to find it by")
        read_ok = False
        for delay in send_confirm.READ_DELAYS_SECONDS:
            await asyncio.sleep(delay)
            resp, read_err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me/mailFolders/sentitems/messages",
                _PROVIDER,
                "confirm the send in sent items",
                params={
                    "$filter": f"sentDateTime ge {mail_send_guard.since_iso(started, send_confirm.WINDOW_SKEW_SECONDS)}",
                    "$orderby": "sentDateTime desc",
                    "$top": 25,
                    "$select": send_confirm.SENT_COPY_SELECT,
                },
                retry_statuses=TRANSIENT_RETRY_STATUSES,
            )
            if read_err or resp is None:
                continue
            try:
                messages = (resp.json() or {}).get("value") or []
            except (ValueError, AttributeError):
                continue
            read_ok = True
            copy = send_confirm.find_sent_copy(
                messages, subject=subject, recipients=recipients, internet_message_id=internet_message_id
            )
            if copy is not None:
                logger.warning("An Outlook send answered by an error is in Sent Items: counted as sent (%s)", err)
                return send_confirm.sent_note(err, copy), None
        if read_ok:
            return "", send_confirm.not_sent_answer(err)
        return "", send_confirm.unknown_answer(err, "Sent Items could not be read to confirm it")

    async def send_microsoft_email(
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        bcc: str = "",
        content_type: str = "text",
        importance: str = "normal",
        attachment_file_ids: list[str] | str | None = None,
        force_duplicate: bool = False,
        file_context: FileContext | None = None,
    ) -> str:
        """Compose and send a new email through Outlook.

        This sends the email immediately (not a draft).  For
        drafting-then-reviewing, use ``create_draft_email`` instead.

        ``to``, ``cc``, ``bcc`` accept comma-separated email addresses.
        ``attachment_file_ids`` attaches files of this conversation
        byte-for-byte (the email then goes through a draft: create, attach,
        send).

        The send guard refuses a second mail of the same subject to
        the same To / Cc within the window when this agent already sent one;
        ``force_duplicate`` sends it anyway.
        """
        to_list = [a.strip() for a in to.split(",") if a.strip()]
        if not to_list:
            return "Error: at least one recipient is required in 'to'."
        files, err = await _resolve_attachment_files(attachment_file_ids, file_context)
        if err:
            return err

        signed, signature_images = await _signed_body(body, content_type)
        payload: dict = {
            "message": {
                "subject": subject,
                # Always deliver well-formed HTML: agent bodies are authored in
                # Markdown, which Outlook renders as literal characters. The
                # shared normalizer converts Markdown/plain text -> sanitized
                # HTML (and passes existing HTML through). See services/email_html.
                "body": signed,
                "toRecipients": [
                    {"emailAddress": {"address": addr}} for addr in to_list
                ],
                "importance": importance,
            },
            "saveToSentItems": True,
        }
        stamp_platform_sent(payload["message"])
        if signature_images:
            payload["message"]["attachments"] = signature_images
        if cc:
            cc_list = [a.strip() for a in cc.split(",") if a.strip()]
            payload["message"]["ccRecipients"] = [
                {"emailAddress": {"address": addr}} for addr in cc_list
            ]
        if bcc:
            bcc_list = [a.strip() for a in bcc.split(",") if a.strip()]
            payload["message"]["bccRecipients"] = [
                {"emailAddress": {"address": addr}} for addr in bcc_list
            ]

        recipients = graph_recipient_addresses(payload["message"])
        async with _client() as client:
            guard, refused = await _guard_send(
                client,
                tool="send_microsoft_email",
                conversation_id=None,
                subject=subject,
                recipients=recipients,
                force=bool(force_duplicate),
            )
            if refused:
                return refused
            try:
                if files:
                    draft_resp, err = await _throttled_request(
                        client,
                        "post",
                        f"{_BASE_URL}/me/messages",
                        _PROVIDER,
                        "create email draft",
                        json=payload["message"],
                    )
                    if err:
                        return err
                    created = draft_resp.json() or {}
                    draft_id = created.get("id", "")
                    if not draft_id:
                        return "Error: Outlook returned no draft ID for the email; nothing was sent."
                    await record_mail_draft(draft_id)
                    attached, err = await _attach_files_to_message(client, draft_id, files)
                    if err:
                        return _attach_failure(err, draft_id, attached, sent=True)
                    # Each file once on what is sent, whatever a retried upload left.
                    read_back = await settle_message_attachments(ctx, client, f"{_BASE_URL}/me/messages/{draft_id}")
                    if signature_images:
                        # Sent only once Exchange holds every image the signature references.
                        read_back += await _settle_signature_images(client, draft_id, str(signed.get("content") or ""))
                    started = time.time()
                    _, err = await _throttled_request(
                        client,
                        "post",
                        f"{_BASE_URL}/me/messages/{draft_id}/send",
                        _PROVIDER,
                        "send email",
                    )
                    confirmed, send_err = "", err
                    if err:
                        confirmed, err = await _settle_send(
                            client,
                            err,
                            started=started,
                            subject=subject,
                            recipients=recipients,
                            internet_message_id=created.get("internetMessageId"),
                        )
                    if err:
                        await ctx.log_account_send(
                            "microsoft_outlook",
                            "send_microsoft_email",
                            conversation_id=created.get("conversationId"),
                            conversation_name=subject,
                            text=body,
                            recipients=recipients,
                            error=err,
                        )
                        if send_confirm.is_ambiguous_send_error(send_err):
                            # The answer already says sent, not sent or unknown.
                            return f"{err}\nDraft ID: {draft_id}."
                        return _attach_failure(err, draft_id, attached, sent=True)
                    await record_mail_send(integration_id, subject, recipients)
                    await ctx.log_account_send(
                        "microsoft_outlook",
                        "send_microsoft_email",
                        conversation_id=created.get("conversationId"),
                        conversation_name=subject,
                        text=body,
                        recipients=recipients,
                        message_id=draft_id,
                    )
                    await guard.sent(
                        subject=subject,
                        recipients=recipients,
                        message_id=draft_id,
                        conversation_id=created.get("conversationId"),
                    )
                    return (
                        f"Email sent to {', '.join(to_list)}. Subject: '{subject}'. "
                        f"Attachments: {', '.join(attached)}.{read_back}{confirmed}"
                    )
                started = time.time()
                _, err = await _throttled_request(
                    client,
                    "post",
                    f"{_BASE_URL}/me/sendMail",
                    _PROVIDER,
                    "send email",
                    json=payload,
                )
                confirmed = ""
                if err:
                    confirmed, err = await _settle_send(
                        client, err, started=started, subject=subject, recipients=recipients
                    )
                if err:
                    await ctx.log_account_send(
                        "microsoft_outlook",
                        "send_microsoft_email",
                        conversation_id=None,
                        conversation_name=subject,
                        text=body,
                        recipients=recipients,
                        error=err,
                    )
                    return err
                await record_mail_send(integration_id, subject, recipients)
                await ctx.log_account_send(
                    "microsoft_outlook",
                    "send_microsoft_email",
                    conversation_id=None,
                    conversation_name=subject,
                    text=body,
                    recipients=recipients,
                )
                await guard.sent(subject=subject, recipients=recipients)
                return f"Email sent to {', '.join(to_list)}. Subject: '{subject}'.{confirmed}"
            finally:
                await guard.release()

    async def create_microsoft_draft_email(
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        bcc: str = "",
        content_type: str = "text",
        importance: str = "normal",
        attachment_file_ids: list[str] | str | None = None,
        file_context: FileContext | None = None,
        include_signature: bool = True,
    ) -> str:
        """Create a new draft email in Outlook (not a reply, a brand-new email).

        The draft is saved in the Drafts folder for the user to review
        and send manually.  For replying to an existing email, use
        ``create_draft_reply`` instead.

        ``to``, ``cc``, ``bcc`` accept comma-separated email addresses.
        ``attachment_file_ids`` attaches files of this conversation
        byte-for-byte. ``include_signature=False`` leaves the signature out:
        a person who opens the draft in Outlook with a signature
        add-in gets the add-in's signature once; ``send_microsoft_draft``
        still signs it when an agent sends it.
        """
        to_list = [a.strip() for a in to.split(",") if a.strip()]
        if not to_list:
            return "Error: at least one recipient is required in 'to'."
        files, err = await _resolve_attachment_files(attachment_file_ids, file_context)
        if err:
            return err

        # The signature's images ride inline on the draft's creation, each once:
        # one request, so a retried call never adds a second set.
        if include_signature:
            signed, signature_images = await _signed_body(body, content_type)
        else:
            signed, signature_images = build_graph_email_body(body, content_type, compose_style=ctx.compose_style), []
        payload: dict = {
            "subject": subject,
            # Always deliver well-formed HTML (see send_microsoft_email).
            "body": signed,
            "toRecipients": [{"emailAddress": {"address": addr}} for addr in to_list],
            "importance": importance,
        }
        stamp_platform_sent(payload)
        if signature_images:
            payload["attachments"] = signature_images
        if cc:
            cc_list = [a.strip() for a in cc.split(",") if a.strip()]
            payload["ccRecipients"] = [
                {"emailAddress": {"address": addr}} for addr in cc_list
            ]
        if bcc:
            bcc_list = [a.strip() for a in bcc.split(",") if a.strip()]
            payload["bccRecipients"] = [
                {"emailAddress": {"address": addr}} for addr in bcc_list
            ]

        async with _client() as client:
            resp, err = await _throttled_request(
                client,
                "post",
                f"{_BASE_URL}/me/messages",
                _PROVIDER,
                "create draft email",
                json=payload,
            )
            if err:
                return err
            draft = resp.json()
            await record_mail_draft(draft.get("id"))
            attached_note = ""
            draft_id = draft.get("id", "")
            if files:
                if not draft_id:
                    return "Error: Outlook returned no draft ID, so the files could not be attached."
                attached, err = await _attach_files_to_message(client, draft_id, files)
                if err:
                    return _attach_failure(err, draft_id, attached, sent=False)
                attached_note = f" Attachments: {', '.join(attached)}."
                attached_note += await settle_message_attachments(ctx, client, f"{_BASE_URL}/me/messages/{draft_id}")
            return (
                f"Draft email created. Draft ID: {draft.get('id', '')}. "
                f"To: {', '.join(to_list)}. Subject: '{subject}'. "
                f"Saved in Drafts folder — NOT sent.{attached_note}"
                + (_ADDIN_NOTE if include_signature and _draft_is_signed(str(signed.get("content") or "")) else "")
                + ("" if include_signature or not signer.signs else _UNSIGNED_DRAFT_NOTE)
            )

    async def send_microsoft_draft(
        draft_id: str,
        force_duplicate: bool = False,
        to: str | list[str] | None = None,
        cc: str | list[str] | None = None,
        bcc: str | list[str] | None = None,
    ) -> str:
        """Send an existing draft email from the Drafts folder; see :func:`_send_draft`."""
        return await _send_draft(
            draft_id,
            force_duplicate=force_duplicate,
            to=parse_addresses(to) or None,
            cc=parse_addresses(cc) if cc is not None else None,
            bcc=parse_addresses(bcc) if bcc is not None else None,
        )

    async def _send_draft_mime(
        client: AutoRefreshClient,
        draft_id: str,
        *,
        thread: tuple[str, str] | None = None,
        headers: list[dict[str, str]] | None = None,
        recipients: tuple[list[str], list[str], list[str] | None] | None = None,
    ) -> tuple[str | None, str]:
        """Send a draft as its own MIME with headers Graph's JSON cannot set: ``(error, note)``.

        ``error`` is ``None`` once sent. ``note`` is empty unless the sent
        draft could not be deleted from Drafts (tried twice): it then names
        the draft and says it must not be sent again, since sending it would
        duplicate the message.

        Graph sets a reply's ``In-Reply-To`` / ``References`` to the message
        the mailbox holds (a forwarded copy) and lets no JSON request change
        them; ``createForward`` takes no ``internetMessageHeaders`` and a
        draft's PATCH refuses them. The draft, built natively (quote,
        signature, inline images, attachments), is read as MIME, given
        *thread* (``(In-Reply-To, References)``) and the custom *headers*,
        sent through ``sendMail`` (Graph saves the copy in Sent Items), then
        deleted from Drafts. *recipients* (``(to, cc, bcc)``, the draft's as
        the tools set them) are written into the MIME's ``To``, ``Cc`` and
        ``Bcc``; a ``None`` bcc (not known) leaves the MIME's ``Bcc`` as is.
        """
        resp, err = await _throttled_request(
            client, "get", f"{_BASE_URL}/me/messages/{draft_id}/$value", _PROVIDER, "read draft MIME"
        )
        if err or resp is None:
            return err or "Error: Outlook returned no MIME for the draft; nothing was sent.", ""
        mime = resp.content
        for delay in _sig_images.SETTLE_DELAYS:
            # The MIME is what is sent: an inline image it references must be in it.
            if not dangling_mime_cids(mime):
                break
            await asyncio.sleep(delay)
            again, again_err = await _throttled_request(
                client, "get", f"{_BASE_URL}/me/messages/{draft_id}/$value", _PROVIDER, "read draft MIME"
            )
            if not again_err and again is not None:
                mime = again.content
        if thread is not None:
            mime = with_thread_headers(mime, in_reply_to=thread[0], references=thread[1])
        # The mark of a message this platform composed rides the MIME too.
        mime = with_extra_headers(mime, [*(headers or []), platform_sent_header()])
        if recipients is not None and recipients[0]:
            mime = with_recipient_headers(mime, to=recipients[0], cc=recipients[1], bcc=recipients[2])
        _, err = await _throttled_request(
            client,
            "post",
            f"{_BASE_URL}/me/sendMail",
            _PROVIDER,
            "send draft MIME",
            content=base64.b64encode(mime),
            headers={**ctx.headers, "Content-Type": "text/plain"},
        )
        if err:
            return err, ""
        del_err: str | None = None
        for _attempt in range(2):
            _, del_err = await _throttled_request(
                client, "delete", f"{_BASE_URL}/me/messages/{draft_id}", _PROVIDER, "delete sent draft"
            )
            if not del_err:
                return None, ""
        logger.warning("A draft sent as MIME stays in Drafts: %s", del_err)
        return None, (
            f" The message was sent, but its draft {draft_id} could not be deleted and stays in Drafts:"
            " do not send that draft again, it would duplicate the message; delete it instead."
        )

    async def _send_draft(
        draft_id: str,
        *,
        force_duplicate: bool = False,
        to: list[str] | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        thread: tuple[str, str] | None = None,
        mime_headers: list[dict[str, str]] | None = None,
        tool: str = "send_microsoft_draft",
    ) -> str:
        """Send an existing draft email from the Drafts folder.

        Use ``create_draft_email`` or ``create_draft_reply`` to create
        a draft first, then call this to send it.

        The send guard refuses the send when this agent already sent
        a message in the draft's conversation within the window (a reply
        draft carries its thread's ``conversationId``); ``force_duplicate``
        sends it anyway.

        ``to`` replaces the draft's To; ``cc`` and ``bcc``, when given (an
        empty list clears them), its Cc and Bcc. ``thread`` is the
        ``(In-Reply-To, References)`` of another mailbox's thread the draft
        answers; ``mime_headers`` are custom headers the sent message carries.
        With either, the draft is sent as its MIME (:func:`_send_draft_mime`).

        A file attachment the draft holds twice (same name, size and bytes,
        e.g. re-attached by an Outlook add-in at a later open) is sent once:
        the later copies are removed before the send and counted in the
        answer.
        """
        draft_id = _clean_message_id(draft_id)
        if err := _validate_message_id(
            draft_id,
            "send_microsoft_draft",
            field_name="draft_id",
            recovery_hint=(
                "Use create_microsoft_draft_email or create_microsoft_draft_reply "
                "and pass the Draft ID they return"
            ),
        ):
            return err
        async with _client() as client:
            # What the sent copy will repeat (and the body to sign), read before
            # the draft leaves Drafts. A failed read records nothing and never
            # holds the send back.
            sent: dict | None = None
            signed_note = ""
            # Why the draft could not be read before the send, if it could not:
            # its log entry then says so instead of an empty subject and body.
            meta_failure: str | None = None
            # The body is read whenever the tools sign at all: a draft signed with
            # the Sent Items copy (no signature integration) has linked images too.
            if integration_id or guard_scope or signer.enabled:
                # The body too: the text logged with the send in an agent account's activity log.
                fields = "subject,toRecipients,ccRecipients,bccRecipients,conversationId,body,hasAttachments,internetMessageId"
                meta, meta_err = await _throttled_request(
                    client,
                    "get",
                    f"{_BASE_URL}/me/messages/{draft_id}",
                    _PROVIDER,
                    "read draft",
                    params={"$select": fields},
                )
                if not meta_err and meta is not None:
                    try:
                        sent = meta.json()
                    except ValueError:
                        sent = None
                if not isinstance(sent, dict):
                    meta_failure = str(meta_err or "the draft could not be read")[:300]
            draft_meta = sent if isinstance(sent, dict) else {}
            overrides: dict[str, object] = {}
            if to:
                overrides["toRecipients"] = graph_recipients(to)
            if cc is not None:
                overrides["ccRecipients"] = graph_recipients(cc)
            if bcc is not None:
                overrides["bccRecipients"] = graph_recipients(bcc)
            if overrides:
                _, err = await _throttled_request(
                    client,
                    "patch",
                    f"{_BASE_URL}/me/messages/{draft_id}",
                    _PROVIDER,
                    "set draft recipients",
                    json=overrides,
                )
                if err:
                    return f"{err} Nothing was sent."
                draft_meta = {**draft_meta, **overrides}
            logged_text = (
                f"Draft {draft_id}: content unavailable, its metadata could not be read ({meta_failure})."
                if meta_failure
                else str((draft_meta.get("body") or {}).get("content") or "")
            )
            recipients = graph_recipient_addresses(draft_meta)
            guard, refused = await _guard_send(
                client,
                tool=tool,
                conversation_id=draft_meta.get("conversationId"),
                subject=draft_meta.get("subject"),
                recipients=recipients,
                force=bool(force_duplicate),
            )
            if refused:
                return refused
            try:
                body = draft_meta.get("body")
                if isinstance(body, dict):
                    # A signed draft is sent as it is: only an unsigned
                    # one is signed, and the remote images of its signature are
                    # attached inline once, its other attachments untouched.
                    content = str(body.get("content") or "")
                    if str(body.get("contentType") or "html").lower() != "html":
                        content = ""
                    if signer.signs:
                        signed_note, content = await _sign_unsigned_draft(client, draft_id, body)
                    if content and _needs_embedding(content):
                        content, image_note = await _embed_and_patch_body(client, draft_id, content)
                        signed_note += image_note
                    if content and signer.enabled:
                        # Sent only once Exchange holds every image the signature references.
                        signed_note += await _settle_signature_images(client, draft_id, content)
                if draft_meta.get("hasAttachments"):
                    # A file an Outlook add-in attached again at a later open
                    # travels twice: one copy of each goes.
                    signed_note += removed_note(
                        await remove_duplicate_file_attachments(
                            _throttled_request, client, f"{_BASE_URL}/me/messages/{draft_id}", provider=_PROVIDER
                        )
                    )
                cleanup_note = ""
                if thread is not None or mime_headers:
                    # The recipients the draft carries (overrides included) are
                    # the ones its MIME names; left as Graph wrote them
                    # when the draft could not be read and was not readdressed.
                    known = isinstance(sent, dict) or {"toRecipients", "ccRecipients"} <= overrides.keys()
                    mime_recipients = (
                        (
                            graph_recipient_addresses({"toRecipients": draft_meta.get("toRecipients") or []}),
                            graph_recipient_addresses({"ccRecipients": draft_meta.get("ccRecipients") or []}),
                            # The Bcc travels in the MIME too; unknown (the draft
                            # unread and not readdressed): left as Graph wrote it.
                            graph_recipient_addresses({"toRecipients": draft_meta.get("bccRecipients") or []})
                            if "bccRecipients" in draft_meta
                            else None,
                        )
                        if known
                        else None
                    )
                    started = time.time()
                    err, cleanup_note = await _send_draft_mime(
                        client, draft_id, thread=thread, headers=mime_headers, recipients=mime_recipients
                    )
                else:
                    started = time.time()
                    _, err = await _throttled_request(
                        client,
                        "post",
                        f"{_BASE_URL}/me/messages/{draft_id}/send",
                        _PROVIDER,
                        "send draft",
                    )
                if err:
                    confirmed, err = await _settle_send(
                        client,
                        err,
                        started=started,
                        subject=draft_meta.get("subject"),
                        recipients=recipients,
                        internet_message_id=draft_meta.get("internetMessageId"),
                    )
                    if not err and (thread is not None or mime_headers):
                        # Sent as its MIME: the draft was not deleted after the error.
                        cleanup_note = (
                            f" Its draft {draft_id} may still be in Drafts: do not send it again, "
                            "it would duplicate the message; delete it instead."
                        )
                    cleanup_note = confirmed + cleanup_note
                if err:
                    await ctx.log_account_send(
                        "microsoft_outlook",
                        "send_microsoft_draft",
                        conversation_id=draft_meta.get("conversationId"),
                        conversation_name=draft_meta.get("subject"),
                        text=logged_text,
                        recipients=recipients,
                        error=err,
                    )
                    return err
                if integration_id and isinstance(sent, dict):
                    await record_mail_send(integration_id, sent.get("subject"), recipients)
                await ctx.log_account_send(
                    "microsoft_outlook",
                    "send_microsoft_draft",
                    conversation_id=draft_meta.get("conversationId"),
                    conversation_name=draft_meta.get("subject"),
                    text=logged_text,
                    recipients=recipients,
                    message_id=draft_id,
                )
                await guard.sent(
                    subject=draft_meta.get("subject"),
                    recipients=recipients,
                    message_id=draft_id,
                    conversation_id=draft_meta.get("conversationId"),
                )
                return f"Draft {draft_id} has been sent.{signed_note}{cleanup_note}"
            finally:
                await guard.release()

    async def forward_microsoft_email(
        message_id: str,
        to: str | list[str],
        cc: str | list[str] | None = None,
        comment: str = "",
        content_type: str = "text",
        force_duplicate: bool = False,
        subject: str | None = None,
    ) -> str:
        """Forward an email with an optional comment above it (createForward + PATCH + send).

        ``subject`` replaces the ``FW:`` subject Outlook built.

        The forward carries the whole original (body, inline images,
        attachments) as Outlook does; the comment is signed like a reply, its
        signature images attached inline once at the send. The forward also
        carries the original's thread (``X-Xtm-Thread-*`` headers), so the
        mailbox it reaches can answer that thread with
        ``reply_microsoft_email_in_thread``: ``createForward`` takes no
        ``internetMessageHeaders`` and a PATCH refuses them, so the headers are
        written into the forward's MIME at the send (:func:`_send_draft_mime`).
        """
        message_id = _clean_message_id(message_id)
        if err := _validate_message_id(message_id, "forward_microsoft_email"):
            return err
        recipients_to = parse_addresses(to)
        if not recipients_to:
            return "Error: forward_microsoft_email needs at least one address in to. Nothing was forwarded."
        kind = str(content_type or "text").strip().lower()
        if kind not in _REPLY_CONTENT_TYPES:
            return f"Error: content_type must be 'text', 'html' or 'markdown', not {content_type!r}. Nothing was forwarded."
        async with _client() as client:
            original, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me/messages/{message_id}",
                _PROVIDER,
                "read message to forward",
                params={"$select": "internetMessageId,internetMessageHeaders,subject"},
            )
            if err or original is None:
                return err or "Error: Outlook returned nothing for the message; nothing was forwarded."
            meta = original.json() or {}
            headers = {
                str(h.get("name") or "").lower(): str(h.get("value") or "")
                for h in meta.get("internetMessageHeaders") or []
            }
            thread_headers = forward_thread_headers(
                str(meta.get("internetMessageId") or ""), headers.get("references", "")
            )
            # createForward takes no internetMessageHeaders and a draft's PATCH
            # refuses them: the thread headers go into the MIME at the send.
            created, err = await _throttled_request(
                client,
                "post",
                f"{_BASE_URL}/me/messages/{message_id}/createForward",
                _PROVIDER,
                "create forward",
                json={},
                headers=PREFER_HTML_BODY,
            )
            if err or created is None:
                return f"{err or 'Error: Outlook answered nothing.'} Nothing was forwarded."
            draft = created.json() or {}
            draft_id = str(draft.get("id") or "")
            if not draft_id:
                return "Error: Outlook returned no forward draft; nothing was forwarded."
            await record_mail_draft(draft_id)
            existing = await html_draft_body(_throttled_request, client, f"{_BASE_URL}/me/messages/{draft_id}", draft)
            inject = _compose_block(comment, kind, ctx.compose_style) if comment.strip() else ""
            if signer.enabled:
                signature = await _get_signature_html(reply=True)
                if signature:
                    inject = close_before_signature(inject) + signature + "\r\n"
            patch: dict[str, object] = {
                "body": {"contentType": "HTML", "content": _inject_above_quote(existing, inject) if inject else existing},
                "toRecipients": graph_recipients(recipients_to),
            }
            stamp_platform_sent(patch)
            if cc is not None:
                patch["ccRecipients"] = graph_recipients(parse_addresses(cc))
            if new_subject := _subject_override(subject):
                patch["subject"] = new_subject
            _, err = await _throttled_request(
                client,
                "patch",
                f"{_BASE_URL}/me/messages/{draft_id}",
                _PROVIDER,
                "patch forward",
                json=patch,
            )
            if err:
                return f"{err} The forward stays in Drafts ({draft_id}); nothing was sent."
        sent = await _send_draft(
            draft_id,
            force_duplicate=force_duplicate,
            mime_headers=thread_headers or None,
            tool="forward_microsoft_email",
        )
        if not sent.startswith(f"Draft {draft_id} has been sent."):
            return sent
        note = sent[len(f"Draft {draft_id} has been sent.") :]
        return f"Email {message_id} forwarded to {', '.join(recipients_to)}.{note}"

    async def reply_microsoft_email_in_thread(
        message_id: str,
        body: str,
        to: str | list[str] | None = None,
        cc: str | list[str] | None = None,
        bcc: str | list[str] | None = None,
        send: bool = False,
        keep_forward_header: bool = False,
        content_type: str = "text",
        force_duplicate: bool = False,
        reply_to_exact_message: bool = False,
        subject: str | None = None,
    ) -> str:
        """Answer, from this mailbox, the thread of a message it holds: often a forwarded copy.

        ``subject``, when given, replaces the reply's subject (the one Outlook
        built, or the original's read from a forward).

        The reply is made on the newest message of the conversation (sent or
        received) unless ``reply_to_exact_message``: a follow-up after this
        mailbox's own reply quotes that reply and goes to its recipients.

        The quote opens on the original message the forward carries: the
        forward's own header and note are left out (and their inline images
        with them) unless ``keep_forward_header``.

        The reply goes to the ORIGINAL thread's participants (read from the
        header block the forward quotes: its sender in To, its other
        participants in Cc, this mailbox left out), not to the forwarder;
        ``to`` / ``cc`` / ``bcc`` replace them. Outlook quotes the history
        natively under the text and the signature. Sent at once with
        ``send=true`` (``In-Reply-To`` / ``References`` set to the original
        thread), else left as a draft for ``send_microsoft_draft``, which
        threads it on the held copy: only ``send=true`` keeps the original
        thread. A forward from outside this mailbox's domain is never threaded
        on the ids its headers name.
        """
        message_id = _clean_message_id(message_id)
        if err := _validate_message_id(message_id, "reply_microsoft_email_in_thread"):
            return err
        given_id, latest = message_id, None
        if not reply_to_exact_message:
            message_id, latest = await _latest_in_thread(message_id)
        repointed = _repointed_note(given_id, latest) if latest is not None else ""
        async with _client() as client:
            held, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me/messages/{message_id}",
                _PROVIDER,
                "read message",
                params={"$select": "subject,body,from,toRecipients,ccRecipients,internetMessageHeaders"},
            )
        if err or held is None:
            return err or "Error: Outlook returned nothing for the message; no reply was made."
        meta = held.json() or {}
        held_subject = str(meta.get("subject") or "")
        forwarded = is_forward(held_subject)
        header = forwarded_header(str((meta.get("body") or {}).get("content") or "")) if forwarded else None
        own = await _own_address()
        sender = str(((meta.get("from") or {}).get("emailAddress") or {}).get("address") or "")
        # The quoted header is body text anyone can write: it names the thread's
        # participants only on a forward from this mailbox's own domain (its
        # Graph sender, not the body). Any other message is answered to its sender.
        untrusted_forward = header is not None and not same_domain(sender, own)
        if untrusted_forward:
            header = None
        if header is not None:
            thread_to, thread_cc = thread_recipients(header, own_address=own)
        elif sender and own and sender.lower() == own.lower():
            # This mailbox's own sent message (a follow-up to its own reply):
            # to the people it went to, never to the mailbox itself.
            thread_to, thread_cc = own_reply_recipients(meta, own_address=own)
        else:
            thread_to = [sender] if sender and sender.lower() != own.lower() else []
            others = graph_recipient_addresses({"toRecipients": meta.get("toRecipients") or []}) + graph_recipient_addresses(
                {"ccRecipients": meta.get("ccRecipients") or []}
            )
            thread_cc = [a for a in others if a.lower() not in {own.lower(), *(t.lower() for t in thread_to)}]
        final_to = parse_addresses(to) or thread_to
        if not final_to:
            return (
                "Error: no recipient found for the reply (the message names no original sender other than "
                "this mailbox); pass to. No reply was made."
            )
        # An untrusted forward's real Cc is chosen by its external sender: never copied.
        final_cc = parse_addresses(cc) if cc is not None else ([] if untrusted_forward else thread_cc)
        # The thread headers of a forward from outside this mailbox's domain are
        # its external sender's to write: never used to thread the reply.
        thread = (
            held_thread(meta.get("internetMessageHeaders") or [], held_subject)
            if forwarded and same_domain(sender, own)
            else None
        )
        draft_id, answer = await _draft_reply(
            message_id,
            body,
            content_type=content_type,
            to=final_to,
            cc=final_cc,
            bcc=parse_addresses(bcc) if bcc is not None else None,
            # The held message's Subject property (plain text), its FW: / RE:
            # prefixes dropped: never the quoted header's text, which is HTML
            # (a trailing &nbsp; reached the subject from there). An explicit
            # subject wins over both.
            subject=_subject_override(subject)
            or (
                reply_subject(held_subject if held_subject.strip() else header.subject) if header is not None else None
            ),
            # Only a forward has a note to leave out: a reply to any other
            # message keeps its whole quote, the message answered first.
            keep_forward_header=keep_forward_header or not forwarded,
        )
        if draft_id and untrusted_forward:
            answer += (
                f" The forward comes from {sender or 'an unknown sender'}, outside this mailbox's domain: its "
                "quoted header was not trusted and the reply is addressed to that sender."
            )
            if send and not parse_addresses(to):
                return answer + " It was NOT sent: pass to=... to send it to the recipients you confirm."
        if draft_id:
            answer += repointed
        if not draft_id or not send:
            if draft_id and forwarded and thread is None:
                answer += (
                    " The forward carries no thread headers it can trust: sent, the reply threads on the "
                    "forwarded copy, not on the original thread."
                )
            elif draft_id and thread is not None:
                # The original thread's ids live in this call only: a draft keeps
                # the held copy's, and send_microsoft_draft sends it that way.
                answer += (
                    f" Sent later with send_microsoft_draft, this draft threads on the forwarded copy, not on "
                    f"the original thread ({thread[0]}): to keep the original thread, delete draft {draft_id} "
                    "and call reply_microsoft_email_in_thread again with send=true once the user gave the go."
                )
            return answer
        sent = await _send_draft(
            draft_id, force_duplicate=force_duplicate, thread=thread, tool="reply_microsoft_email_in_thread"
        )
        if not sent.startswith(f"Draft {draft_id} has been sent."):
            return sent
        note = sent[len(f"Draft {draft_id} has been sent.") :]
        threaded = (
            f" In-Reply-To {thread[0]} (the original thread)."
            if thread is not None
            else " Threaded on the message this mailbox holds."
        )
        cc_note = f" Cc: {', '.join(final_cc)}." if final_cc else ""
        return f"Reply sent in thread to {', '.join(final_to)}.{cc_note}{threaded}{note}{repointed}"

    executor.register("forward_microsoft_email", forward_microsoft_email)
    executor.register("reply_microsoft_email_in_thread", reply_microsoft_email_in_thread)
    executor.register("list_microsoft_emails", list_microsoft_emails)
    executor.register("get_microsoft_email", get_microsoft_email)
    executor.register("get_microsoft_attachment", get_microsoft_attachment)
    executor.register("export_microsoft_email_mime", export_microsoft_email_mime)
    executor.register("search_microsoft_emails", search_microsoft_emails)
    executor.register("flag_microsoft_email", flag_microsoft_email)
    executor.register("unflag_microsoft_email", unflag_microsoft_email)
    executor.register("list_microsoft_flagged_emails", list_microsoft_flagged_emails)
    executor.register("mark_microsoft_email_read", mark_microsoft_email_read)
    executor.register("create_microsoft_draft_reply", create_microsoft_draft_reply)
    executor.register("get_microsoft_email_signature", get_microsoft_email_signature)
    executor.register("send_microsoft_email", send_microsoft_email)
    executor.register("create_microsoft_draft_email", create_microsoft_draft_email)
    executor.register("send_microsoft_draft", send_microsoft_draft)
    executor.register("delete_microsoft_draft", delete_microsoft_draft)

    # What the model reads is what this session does.
    reply_signature_from = (
        f"read from {signer.plan.source()} (the person's current signature), "
        "else extracted from recent sent emails"
        if signer.signs
        else "extracted from recent sent emails"
    )
    reply_signature = (
        " The user's email signature is inserted automatically between the reply and the quoted thread "
        f"({reply_signature_from}). Do NOT include the email signature in the body — it is inserted automatically."
        if signer.enabled
        else " Email signatures are off for this integration: no signature is inserted."
    )
    reply_body_signature = (
        " Do NOT include the email signature — it is inserted automatically in the correct position above the quoted thread."
        if signer.enabled
        else ""
    )
    new_email_signature = signer.tool_note()
    signature_lookup = (
        f"Retrieve the user's email signature and where it comes from: {reply_signature_from} "
        "(the Graph API has no signature endpoint). When there is none, it says why and how to set one up."
        if signer.enabled
        else "Say why the emails of this integration carry no signature (email signatures are off) and how to turn them on."
    )
    draft_signature = (
        " A draft whose own part carries no signature yet gets the user's signature before it is sent."
        if signer.signs
        else ""
    )
    guard_window = mail_send_guard.window_seconds()
    send_guard_note = (
        f" Refused (nothing sent) when this mailbox (any agent or run) already sent a message in the same conversation within the last {guard_window} s and no message from anyone else arrived in it since, naming that message: a second answer to one thread with nothing new to answer is a duplicate unless force_duplicate=true is passed on purpose. A newer inbound message in the conversation lets the send go out."
        if guard_scope and guard_window > 0
        else ""
    )

    return [
        # ── Email Read ──
        ToolDefinition(
            name="list_microsoft_emails",
            description="List recent emails from an Outlook mail folder. Returns ID, subject, sender, date, conversation ID, preview, and flag status. include_preview=false returns one compact line per message (id, sender, subject, received date, conversation id, read/flag/importance state) for large reads. Use folder='inbox' (default), 'sentitems', 'drafts', 'deleteditems', or 'archive'. Use the returned ID with get_microsoft_email, flag_microsoft_email, or create_microsoft_draft_reply.",
            input_schema={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Number of emails (default 10)",
                        "default": 10,
                    },
                    "folder": {
                        "type": "string",
                        "description": "Mail folder: 'inbox' (default), 'sentitems' (sent mail), 'drafts', 'deleteditems', 'archive'",
                        "default": "inbox",
                    },
                    "include_preview": {
                        "type": "boolean",
                        "description": "false: one line per message (id | sender | subject | received date | conversation id | state), no preview. Default true.",
                        "default": True,
                    },
                    "received_from": {
                        "type": "string",
                        "description": "ISO-8601: only messages received at or after it; the answer counts the window and names the next page_token.",
                    },
                    "received_to": {"type": "string", "description": "ISO-8601: only messages received before it."},
                    "page_token": {
                        "type": "string",
                        "description": "The next page, as the previous answer names it (same folder, window and count).",
                    },
                },
                "required": [],
            },
            source="integration:microsoft",
            priority=0,
        ),
        ToolDefinition(
            name="get_microsoft_email",
            description="Get full content of a specific Outlook email by message ID. Returns complete body, all recipients (To/CC/BCC), the email's Outlook web link, each attachment with its attachment_id (read or save one with get_microsoft_attachment), and conversation thread ID. Use this before drafting a reply to understand the full context.",
            input_schema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The Microsoft Graph message ID (from list_microsoft_emails/search_microsoft_emails/list_microsoft_flagged_emails 'ID' field). This is a long alphanumeric string, NOT an email address.",
                    },
                    "include_raw": {
                        "type": "boolean",
                        "description": "Also return the body's raw HTML (a window), its signature blocks and add-in markers, its cid: references, and every attachment's content id and last modification time, oldest first. For diagnosing how a message or draft is built; off by default.",
                        "default": False,
                    },
                    "raw_offset": {
                        "type": "integer",
                        "description": "With include_raw: the character of the body's HTML the window starts at (default 0).",
                        "default": 0,
                    },
                    "include_headers": {
                        "type": "boolean",
                        "description": "Also return the internet headers (List-Unsubscribe, Auto-Submitted, Received...), bounded; off by default.",
                        "default": False,
                    },
                    "body_offset": {
                        "type": "integer",
                        "description": "The character of the body text the answer starts at, for a long body the previous answer says was truncated (default 0).",
                        "default": 0,
                    },
                },
                "required": ["message_id"],
            },
            source="integration:microsoft",
            priority=0,
        ),
        ToolDefinition(
            name="get_microsoft_attachment",
            description="Read an attachment of an Outlook email (message_id) or calendar event (event_id) and keep it as a file in the conversation. Returns the file's [[FILE:...]] marker and file_id (forward it to any tool that takes a file_id, e.g. send_microsoft_email or upload_microsoft_drive_file), the text of a readable file inline (text, Word, PowerPoint, PDF, Excel, an attached email), and the email's or event's Outlook web link. Find attachment ids with get_microsoft_email or get_microsoft_calendar_event.",
            input_schema={
                "type": "object",
                "properties": {
                    "attachment_id": {
                        "type": "string",
                        "description": "The attachment_id listed by get_microsoft_email or get_microsoft_calendar_event",
                    },
                    "message_id": {
                        "type": "string",
                        "description": "The email's Microsoft Graph message ID (for an email attachment)",
                        "default": "",
                    },
                    "event_id": {
                        "type": "string",
                        "description": "The calendar event ID (for an event attachment)",
                        "default": "",
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "Optional calendar id of the event, when it is in a shared or secondary calendar",
                        "default": "",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Optional filename override for the saved file",
                        "default": "",
                    },
                },
                "required": ["attachment_id"],
            },
            source="integration:microsoft",
            priority=0,
        ),
        ToolDefinition(
            name="export_microsoft_email_mime",
            description=(
                "Export an Outlook email's raw MIME as a .eml file kept in the conversation "
                "(file_id and [[FILE:...]] marker), and return its header section whole, "
                "never cut (Received, Authentication-Results, DKIM, ARC). Use it when "
                "get_microsoft_email(include_headers=true) says headers were cut, or to hand "
                "the original message on."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The email's Microsoft Graph message ID",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Optional filename of the saved .eml (default message.eml)",
                        "default": "",
                    },
                },
                "required": ["message_id"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="search_microsoft_emails",
            description="Search across all Outlook mailboxes by keyword. Returns matching emails with subject, sender, date, and preview. Use this for cross-folder keyword searches; use list_microsoft_emails for browsing a specific folder.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "count": {
                        "type": "integer",
                        "description": "Max results",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
            source="integration:microsoft",
            priority=0,
        ),
        ToolDefinition(
            name="list_microsoft_flagged_emails",
            description="List all Outlook emails flagged for follow-up. Returns email IDs, senders, subjects, dates, conversation IDs and previews, sorted oldest first. include_preview=false returns one compact line per flag (id, sender address, subject, received date, conversation id) for large sweeps. The triage assignment flags emails needing a reply; the drafting assignment processes them. Use get_microsoft_email for full body before drafting. Supports automatic pagination.",
            input_schema={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Max number of flagged emails to return (default 50)",
                        "default": 50,
                    },
                    "include_preview": {
                        "type": "boolean",
                        "description": "false: one line per flag (id | sender address | subject | received date | conversation id), no preview. Default true.",
                        "default": True,
                    },
                },
                "required": [],
            },
            source="integration:microsoft",
            priority=0,
        ),
        # ── Email Write ──
        ToolDefinition(
            name="flag_microsoft_email",
            description="Flag an email in Outlook for follow-up. Used by the triage assignment to mark emails needing a reply — the drafting assignment picks them up via list_microsoft_flagged_emails. Also works for manually queuing emails for reply drafting.",
            input_schema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The Microsoft Graph message ID (from list_microsoft_emails/search_microsoft_emails 'ID' field). This is a long alphanumeric string, NOT an email address.",
                    }
                },
                "required": ["message_id"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="unflag_microsoft_email",
            description="Remove the follow-up flag from an email after a draft reply has been created. Call this after successfully creating a draft reply so the email exits the flagged queue.",
            input_schema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The Microsoft Graph message ID (from list_microsoft_emails/list_microsoft_flagged_emails 'ID' field). This is a long alphanumeric string, NOT an email address.",
                    }
                },
                "required": ["message_id"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="mark_microsoft_email_read",
            description="Mark an email as read in Outlook.",
            input_schema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The Microsoft Graph message ID (from list_microsoft_emails 'ID' field). This is a long alphanumeric string, NOT an email address.",
                    }
                },
                "required": ["message_id"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="create_microsoft_draft_reply",
            description=f"Create a draft reply to an email. Preserves the full conversation thread (like replying in Outlook). Uses the proper two-step approach (createReply + PATCH) matching Outlook's native reply HTML structure. The body is rendered according to content_type: 'text' (default) is sent as plain text, so HTML passed with content_type='text' shows its raw tags; 'html' renders headings, tables and links, and 'markdown' is converted to HTML, as send_microsoft_email does. Example: content_type='html', body='<h3>Status</h3><table><tr><th>Item</th><th>State</th></tr><tr><td>Patch</td><td>Done</td></tr></table>'. The draft is saved in Drafts folder — NOT sent. The user reviews and sends manually. Supports reply-all and attaching files of this conversation (attachment_file_ids). Made on the newest message of the thread, own sent replies included.{reply_signature}",
            input_schema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The Microsoft Graph message ID to reply to (from list_microsoft_emails/list_microsoft_flagged_emails 'ID' field). This is a long alphanumeric string, NOT an email address.",
                    },
                    "body": {
                        "type": "string",
                        "description": f"The reply content only (greeting, message, sign-off), rendered according to content_type.{reply_body_signature}",
                    },
                    "reply_all": {
                        "type": "boolean",
                        "description": "If true, reply to all recipients instead of just the sender (default false).",
                        "default": False,
                    },
                    "content_type": {
                        "type": "string",
                        "enum": list(_REPLY_CONTENT_TYPES),
                        "description": "How the body is rendered: 'text' (default) sends it as plain text, line by line, so HTML tags in it show literally; 'html' renders an HTML body (sanitized like send_microsoft_email); 'markdown' converts Markdown (tables, links, bold, headings) to HTML.",
                        "default": "text",
                    },
                    "attachment_file_ids": _ATTACHMENT_FILE_IDS_PROPERTY,
                    "to": _RECIPIENTS_PROPERTY("Replaces the reply's To"),
                    "cc": _RECIPIENTS_PROPERTY("Replaces the reply's Cc; an empty string clears it"),
                    "bcc": _RECIPIENTS_PROPERTY("Replaces the reply's Bcc"),
                    "reply_to_exact_message": _REPLY_TO_EXACT_MESSAGE_PROPERTY,
                    "subject": _SUBJECT_OVERRIDE_PROPERTY("RE: original subject"),
                },
                "required": ["message_id", "body"],
            },
            source="integration:microsoft",
            priority=0,
        ),
        ToolDefinition(
            name="forward_microsoft_email",
            description=(
                "Forward an email, with an optional comment above it, and send it at once. The forward carries "
                "the whole original (body, inline images, attachments) and its thread, so the mailbox it "
                "reaches can answer the original correspondents with reply_microsoft_email_in_thread "
                f"(for example: forward to an assistant's mailbox, which then replies in the thread).{reply_body_signature}{send_guard_note}"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The Microsoft Graph message ID to forward (the 'ID' field of list/search/get).",
                    },
                    "to": _RECIPIENTS_PROPERTY("Who the forward goes to"),
                    "cc": _RECIPIENTS_PROPERTY("Cc of the forward"),
                    "comment": {
                        "type": "string",
                        "description": "A short note written above the forwarded message (optional).",
                        "default": "",
                    },
                    "content_type": {
                        "type": "string",
                        "enum": list(_REPLY_CONTENT_TYPES),
                        "description": "How the comment is rendered: 'text' (default), 'html' or 'markdown'.",
                        "default": "text",
                    },
                    "force_duplicate": _FORCE_DUPLICATE_PROPERTY,
                    "subject": _SUBJECT_OVERRIDE_PROPERTY("FW: original subject"),
                },
                "required": ["message_id", "to"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="reply_microsoft_email_in_thread",
            description=(
                "Reply from this mailbox in the thread of a message it holds, typically a message someone "
                "forwarded to it: the reply goes to the ORIGINAL correspondents (the forwarded message's "
                "sender in To, its other participants in Cc, this mailbox left out), not to the forwarder, with "
                "the whole thread quoted under the text as Outlook does. to/cc/bcc replace the recipients. "
                "A draft by default (send it with send_microsoft_draft: it then threads on the forwarded copy); "
                "send=true sends at once, in the original thread (In-Reply-To/References), once the user gave "
                "the go: only send=true keeps the original thread. Made on the newest message of the thread, own sent "
                f"replies included.{reply_body_signature}{send_guard_note}"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The Microsoft Graph message ID this mailbox holds (the forwarded copy, or any message of the thread).",
                    },
                    "body": {
                        "type": "string",
                        "description": f"The reply content only (greeting, message, sign-off), rendered according to content_type.{reply_body_signature}",
                    },
                    "to": _RECIPIENTS_PROPERTY("Replaces the To read from the thread"),
                    "cc": _RECIPIENTS_PROPERTY("Replaces the Cc read from the thread; an empty string clears it"),
                    "bcc": _RECIPIENTS_PROPERTY("Bcc of the reply"),
                    "send": {
                        "type": "boolean",
                        "description": "Send at once (default false: a draft).",
                        "default": False,
                    },
                    "keep_forward_header": {
                        "type": "boolean",
                        "description": "Keep the forward's own header and comment (the internal note) in the quote (default false: the quote opens on the original message the forward carries).",
                        "default": False,
                    },
                    "content_type": {
                        "type": "string",
                        "enum": list(_REPLY_CONTENT_TYPES),
                        "description": "How the body is rendered: 'text' (default), 'html' or 'markdown'.",
                        "default": "text",
                    },
                    "force_duplicate": _FORCE_DUPLICATE_PROPERTY,
                    "reply_to_exact_message": _REPLY_TO_EXACT_MESSAGE_PROPERTY,
                    "subject": _SUBJECT_OVERRIDE_PROPERTY("RE: original subject"),
                },
                "required": ["message_id", "body"],
            },
            source="integration:microsoft",
            priority=1,
            # Artifact markers in the body are sent as links (outbound_markers).
            outbound_text=("body",),
        ),
        ToolDefinition(
            name="get_microsoft_email_signature",
            description=signature_lookup,
            input_schema={"type": "object", "properties": {}, "required": []},
            source="integration:microsoft",
            priority=2,
        ),
        # ── Email Send ──
        ToolDefinition(
            name="send_microsoft_email",
            description=f"Compose and send a new email through Outlook immediately. Attach files of this conversation with attachment_file_ids. For composing a draft to review before sending, use create_draft_email. For replying to an existing email, use create_draft_reply.{new_email_signature}{send_guard_note}",
            input_schema={
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "Comma-separated recipient email addresses",
                    },
                    "subject": {"type": "string", "description": "Email subject line"},
                    "body": {
                        "type": "string",
                        "description": "Email body content. Markdown is supported and automatically rendered to HTML (tables, links, bold) before sending.",
                    },
                    "cc": {
                        "type": "string",
                        "description": "Comma-separated CC email addresses",
                        "default": "",
                    },
                    "bcc": {
                        "type": "string",
                        "description": "Comma-separated BCC email addresses",
                        "default": "",
                    },
                    "content_type": {
                        "type": "string",
                        "enum": ["text", "html"],
                        "description": "Body content type. Markdown/plain text is auto-rendered to HTML on send; set 'html' only when the body is already HTML.",
                        "default": "text",
                    },
                    "importance": {
                        "type": "string",
                        "enum": ["low", "normal", "high"],
                        "description": "Email importance (default 'normal')",
                        "default": "normal",
                    },
                    "attachment_file_ids": _ATTACHMENT_FILE_IDS_PROPERTY,
                    "force_duplicate": _FORCE_DUPLICATE_PROPERTY,
                },
                "required": ["to", "subject", "body"],
            },
            # The same message to the same recipients twice is refused.
            send_guard=SendGuard(recipients=("to", "cc", "bcc"), body=("subject", "body")),
            read_back=WriteReadBack(
                kind="email",
                recipients=("to", "cc", "bcc"),
                body=("body",),
                shown_arguments=("subject",),
            ),
            source="integration:microsoft",
            priority=0,
        ),
        ToolDefinition(
            name="create_microsoft_draft_email",
            description=f"Create a new draft email in Outlook (not a reply). Saved in Drafts folder for the user to review and send manually. Attach files of this conversation with attachment_file_ids. Use send_draft to send it programmatically, or the user can open and send it from Outlook.{new_email_signature}",
            input_schema={
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "Comma-separated recipient email addresses",
                    },
                    "subject": {"type": "string", "description": "Email subject line"},
                    "body": {
                        "type": "string",
                        "description": "Email body content. Markdown is supported and automatically rendered to HTML (tables, links, bold) before sending.",
                    },
                    "cc": {
                        "type": "string",
                        "description": "Comma-separated CC email addresses",
                        "default": "",
                    },
                    "bcc": {
                        "type": "string",
                        "description": "Comma-separated BCC email addresses",
                        "default": "",
                    },
                    "content_type": {
                        "type": "string",
                        "enum": ["text", "html"],
                        "description": "Body content type. Markdown/plain text is auto-rendered to HTML on send; set 'html' only when the body is already HTML.",
                        "default": "text",
                    },
                    "importance": {
                        "type": "string",
                        "enum": ["low", "normal", "high"],
                        "description": "Email importance (default 'normal')",
                        "default": "normal",
                    },
                    "attachment_file_ids": _ATTACHMENT_FILE_IDS_PROPERTY,
                    "include_signature": {
                        "type": "boolean",
                        "description": "Add the user's signature to the draft (default true). Set false for a draft the user will open and send from Outlook: a signature add-in then inserts its signature exactly once; send_microsoft_draft still signs it if an agent sends it.",
                        "default": True,
                    },
                },
                "required": ["to", "subject", "body"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="send_microsoft_draft",
            description=f"Send an existing draft email from the Drafts folder. Use after create_draft_email or create_draft_reply to send the draft without user intervention.{draft_signature}{send_guard_note}",
            input_schema={
                "type": "object",
                "properties": {
                    "draft_id": {
                        "type": "string",
                        "description": "The draft message ID (from create_draft_email or create_draft_reply output)",
                    },
                    "force_duplicate": _FORCE_DUPLICATE_PROPERTY,
                    "to": _RECIPIENTS_PROPERTY("Replaces the draft's To before it is sent"),
                    "cc": _RECIPIENTS_PROPERTY("Replaces the draft's Cc before it is sent; an empty string clears it"),
                    "bcc": _RECIPIENTS_PROPERTY("Replaces the draft's Bcc before it is sent"),
                },
                "required": ["draft_id"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="delete_microsoft_draft",
            description=(
                "Delete a draft email from the Outlook Drafts folder (e.g. an obsolete draft replaced by a new one). "
                "Drafts only: a message that is not a draft (received or sent) is refused and left untouched. "
                "The draft moves to Deleted Items. Answers the subject and creation time of what was deleted."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "draft_id": {
                        "type": "string",
                        "description": "The draft message ID (from create_microsoft_draft_email, create_microsoft_draft_reply or list_microsoft_emails on the Drafts folder)",
                    },
                },
                "required": ["draft_id"],
            },
            source="integration:microsoft",
            priority=1,
        ),
    ]
