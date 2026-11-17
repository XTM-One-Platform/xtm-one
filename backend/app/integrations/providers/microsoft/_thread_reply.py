"""Answering a thread held in another mailbox: forward, then reply in thread.

A person forwards a message to an assistant's mailbox; the assistant answers
the ORIGINAL correspondents from her own mailbox, the thread quoted under her
text. The forwarded copy names its forwarder as sender; the original's
participants are in the header block the forward quotes (Outlook's
``From: / Sent: / To: / Cc: / Subject:`` block, Gmail's ``---------- Forwarded
message ---------`` one, English or French labels). These helpers read that
block and work out the reply's recipients; the mail tools do the Graph
requests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape as _html_unescape

_ADDRESS = re.compile(r"[A-Za-z0-9._%+'\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_FORWARD_PREFIX = re.compile(r"^\s*(?:(?:fw|fwd|tr|wg|rv)\s*:\s*)+", re.IGNORECASE)
_LABELS = {
    "from": ("from", "de", "von"),
    "to": ("to", "à", "a", "an"),
    "cc": ("cc",),
    "subject": ("subject", "objet", "betreff"),
    "sent": ("sent", "date", "envoyé", "gesendet"),
}
_LABEL_LINE = re.compile(r"^\s*([A-Za-zÀ-ÿ]+)\s*:\s*(.*)$")


@dataclass
class ForwardedHeader:
    """The participants and subject of the message a forward quotes."""

    sender: list[str] = field(default_factory=list)
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    subject: str = ""


def _header_lines(html: str) -> list[str]:
    """The non-blank text lines of *html* as a reader sees them: tags dropped, entities read.

    ``&nbsp;`` (and U+00A0) reads as a space and ``&amp;`` / ``&lt;`` / ``&gt;``
    as their characters, so a value read from a quoted header block never
    carries an entity into a subject or an address.
    """
    lines = (_html_unescape(line).replace("\xa0", " ").strip() for line in _text_lines(html))
    return [line for line in lines if line]


def _label(word: str) -> str | None:
    lowered = word.lower()
    for key, names in _LABELS.items():
        if lowered in names:
            return key
    return None


def forwarded_header(html: str) -> ForwardedHeader | None:
    """The first quoted header block of *html* that is no forward's (the original), ``None`` without one.

    A block is a ``From:`` line carrying an address followed, within a few
    lines, by a ``Sent:`` / ``Date:`` or ``Subject:`` one; a field that wraps
    onto the next line keeps its addresses; the block ends at its subject or
    at a blank line.
    """
    lines = _header_lines(html)
    for index, line in enumerate(lines):
        match = _LABEL_LINE.match(line)
        if not match or _label(match.group(1)) != "from" or not _ADDRESS.search(match.group(2)):
            continue
        window = lines[index : index + 12]
        if not any(
            (m := _LABEL_LINE.match(other)) and _label(m.group(1)) in ("sent", "subject") for other in window[1:]
        ):
            continue
        header = ForwardedHeader()
        current: str | None = None
        for other in window:
            m = _LABEL_LINE.match(other)
            key = _label(m.group(1)) if m else None
            if key is not None and m is not None:
                current = key
                value = m.group(2)
            elif not other:
                break
            else:
                value = other
            if current == "from":
                header.sender += _ADDRESS.findall(value)
            elif current == "to":
                header.to += _ADDRESS.findall(value)
            elif current == "cc":
                header.cc += _ADDRESS.findall(value)
            elif current == "subject" and key is not None:
                header.subject = value.strip()
                break
        # A forward's own header (a forward of a forward, or the quote of a
        # reply to a forward) names the forwarder: the original is further down.
        if header.sender and not is_forward(header.subject):
            return header
    return None


def _dedupe(addresses: list[str], *, drop: set[str]) -> list[str]:
    seen: set[str] = set()
    kept: list[str] = []
    for address in addresses:
        lowered = address.lower()
        if lowered in seen or lowered in drop:
            continue
        seen.add(lowered)
        kept.append(address)
    return kept


def thread_recipients(header: ForwardedHeader, *, own_address: str) -> tuple[list[str], list[str]]:
    """``(to, cc)`` of a reply to the forwarded original: its sender, then its other participants.

    The sending mailbox is left out; the forwarder stays when the original
    named them (they were in the thread).
    """
    own = {own_address.lower()} if own_address else set()
    to = _dedupe(header.sender, drop=own)
    cc = _dedupe(header.to + header.cc, drop=own | {a.lower() for a in to})
    return to, cc


_REPLY_PREFIX = re.compile(r"^\s*(?:(?:re|aw|sv)\s*:\s*)+", re.IGNORECASE)
#: What opens a quoted message in Outlook's HTML, however the quote was written:
#: its header block (``divRplyFwdMsg``, ``x_divRplyFwdMsg`` once quoted again),
#: its ``<hr>`` rule, or the line of underscores Exchange writes when it turns a
#: plain-text message into HTML. Each opens a message only with a header under it.
_QUOTE_SEPARATOR = re.compile(
    r"<div\b[^>]*\bid\s*=\s*[\"']?(?:x_)?divRplyFwdMsg[\"']?[^>]*>|<hr\b[^>]*>|_{16,}",
    re.IGNORECASE,
)
#: What may sit between a separator and the header's From label: tags, blanks, more underscores.
_SEPARATOR_GAP = re.compile(r"(?:<[^>]*>|\s|&nbsp;|&#160;|\xa0|_)*", re.IGNORECASE)
_HEADER_FROM = re.compile(r"(?:From|De|Von)\s*:", re.IGNORECASE)
_HEADER_OTHER = re.compile(
    r"\s*(?:Sent|Date|To|Cc|Subject|Envoyé|À|Objet|Gesendet|An|Betreff|Enviado|Para|Asunto)\s*:",
    re.IGNORECASE,
)
#: What ends a line in a quote's HTML: a line break, the end of a block, a newline.
_LINE_BREAK = re.compile(r"<br\b[^>]*>|</(?:div|p|li|tr|h[1-6])\s*>|\r?\n", re.IGNORECASE)
_BLANK = re.compile(r"(?:\s|&nbsp;|&#160;|\xa0)*", re.IGNORECASE)
#: How far (in HTML) after a From label the header's other fields are looked for.
_HEADER_SPAN = 2000
#: An opening tag (not a closing one, not ``<hr>``) right before a position, blanks after it.
_OPENING_TAG_BEFORE = re.compile(r"<(?!/|hr\b)[a-z][^>]*>\s*$", re.IGNORECASE)


def reply_subject(subject: str) -> str:
    """``RE:`` and the thread's subject, its forward and reply prefixes dropped.

    *subject* is a message's ``subject`` property (plain text): it is
    never HTML-escaped, and the blanks around it (U+00A0 included) are dropped.
    """
    bare = subject or ""
    while True:
        stripped = _REPLY_PREFIX.sub("", _FORWARD_PREFIX.sub("", bare))
        if stripped == bare:
            break
        bare = stripped
    return f"RE: {bare.strip()}"


def _text_lines(html: str) -> list[str]:
    """The non-blank text lines of an HTML fragment, its tags dropped."""
    lines = (re.sub(r"<[^>]*>", "", line) for line in _LINE_BREAK.split(html))
    return [line for line in lines if not _BLANK.fullmatch(line)]


def quote_boundaries(html: str) -> list[int]:
    """Where each quoted message of *html* opens: the offset of its first separator, in order.

    A separator (a ``divRplyFwdMsg`` block, an ``<hr>``, a line of underscores)
    opens a quoted message only when a header follows it: a From label (with
    nothing but tags and blanks between), then a Sent / To / Subject one on the
    next line. An
    ``<hr>`` and the header block right under it open one message, at the
    ``<hr>``. A rule or an underscore divider in a note or a signature, with no
    header under it, opens nothing.
    """
    html = html or ""
    opened: dict[int, int] = {}
    for separator in _QUOTE_SEPARATOR.finditer(html):
        gap = _SEPARATOR_GAP.match(html, separator.end())
        label_at = gap.end() if gap else separator.end()
        label = _HEADER_FROM.match(html, label_at)
        if not label:
            continue
        # The header's next field is on the line right under the From line: a
        # From label in a note's prose, a Subject further down, opens nothing.
        fields = _text_lines(html[label.end() : label.end() + _HEADER_SPAN])
        if len(fields) < 2 or not _HEADER_OTHER.match(fields[1]):
            continue
        opened.setdefault(label_at, separator.start())
    return sorted(opened.values())


def drop_forward_header(html: str) -> str:
    """A reply draft's HTML without the forward it answers: its header and its comment.

    The reply to a forwarded copy quotes the forward first (its header block,
    the forwarder's comment and signature), then the forwarded message's own
    header block. Everything from the first quoted message's separator up to
    the second's is dropped, so the quote opens on the original message,
    whatever layout Outlook or Exchange gave the separators
    (:func:`quote_boundaries`). A draft quoting a single message is unchanged.
    """
    boundaries = quote_boundaries(html)
    if len(boundaries) < 2:
        return html
    # The original's quote keeps the element that wraps its separator (an
    # underscore line sits inside the block Exchange opened for its text).
    keep_from = boundaries[1]
    while opening := _OPENING_TAG_BEFORE.search(html, 0, keep_from):
        if opening.end() != keep_from or opening.start() <= boundaries[0]:
            break
        keep_from = opening.start()
    return html[: boundaries[0]] + html[keep_from:]


#: Headers the forward tool stamps on a forward: the thread of the message it
#: forwards, so the mailbox it reaches can reply in that thread.
THREAD_IN_REPLY_TO_HEADER = "x-xtm-thread-in-reply-to"
THREAD_REFERENCES_HEADER = "x-xtm-thread-references"
_MESSAGE_ID = re.compile(r"<[^<>\s]+@[^<>\s]+>")


def message_ids(value: str) -> list[str]:
    """The ``<id@host>`` message ids of a header value, in order."""
    return _MESSAGE_ID.findall(value or "")


def forward_thread_headers(internet_message_id: str, references: str) -> list[dict[str, str]]:
    """The custom headers a forward of a message carries: its id, and the thread it belongs to."""
    ids = list(dict.fromkeys(message_ids(references) + message_ids(internet_message_id)))
    if not ids:
        return []
    return [
        {"name": THREAD_IN_REPLY_TO_HEADER, "value": ids[-1]},
        {"name": THREAD_REFERENCES_HEADER, "value": " ".join(ids)},
    ]


def held_thread(headers: list[dict[str, str]] | None, subject: str) -> tuple[str, str] | None:
    """``(In-Reply-To, References)`` of the thread a message this mailbox holds was forwarded from.

    Read from the headers the forward tool stamps; else, on a forward, from
    the forward's own ``References`` (the forwarded message's thread).
    ``None`` when the message is no forward of a thread.
    """
    found = {str(h.get("name") or "").lower(): str(h.get("value") or "") for h in headers or []}
    stamped = message_ids(found.get(THREAD_IN_REPLY_TO_HEADER, ""))
    if stamped:
        refs = message_ids(found.get(THREAD_REFERENCES_HEADER, "")) or stamped
        return stamped[-1], " ".join(refs)
    if not is_forward(subject):
        return None
    refs = message_ids(found.get("references", "")) or message_ids(found.get("in-reply-to", ""))
    if not refs:
        return None
    return refs[-1], " ".join(refs)


_HEADER_END = re.compile(rb"\r?\n\r?\n")
_THREAD_HEADER = re.compile(rb"^(?:in-reply-to|references)\s*:", re.IGNORECASE)


def with_thread_headers(mime: bytes, *, in_reply_to: str, references: str) -> bytes:
    """*mime* (a message as Graph's ``$value`` gives it) answering another thread.

    Its ``In-Reply-To`` and ``References`` (folded lines included) are
    replaced by *in_reply_to* and *references*; the body is not touched.
    """
    end = _HEADER_END.search(mime)
    head, rest = (mime[: end.start()], mime[end.start() :]) if end else (mime, b"")
    newline = b"\r\n" if b"\r\n" in head else b"\n"
    kept: list[bytes] = []
    dropping = False
    for line in head.split(newline):
        if line[:1] in (b" ", b"\t"):
            if not dropping:
                kept.append(line)
            continue
        dropping = bool(_THREAD_HEADER.match(line))
        if not dropping:
            kept.append(line)
    kept.append(b"In-Reply-To: " + in_reply_to.encode("ascii", "ignore"))
    kept.append(b"References: " + references.encode("ascii", "ignore"))
    return newline.join(kept) + rest


def with_extra_headers(mime: bytes, headers: list[dict[str, str]]) -> bytes:
    """*mime* (a message as Graph's ``$value`` gives it) with *headers* added to its header block.

    A header of the same name already there (folded lines included) is
    replaced; the body is not touched. This is how a forward carries the
    ``X-Xtm-Thread-*`` headers: Graph's ``createForward`` takes no
    ``internetMessageHeaders``, a draft's PATCH refuses them, and a MIME
    ``sendMail`` keeps them.
    """
    names = {str(h.get("name") or "").strip().lower().encode("ascii", "ignore") for h in headers}
    names.discard(b"")
    if not names:
        return mime
    end = _HEADER_END.search(mime)
    head, rest = (mime[: end.start()], mime[end.start() :]) if end else (mime, b"")
    newline = b"\r\n" if b"\r\n" in head else b"\n"
    kept: list[bytes] = []
    dropping = False
    for line in head.split(newline):
        if line[:1] in (b" ", b"\t"):
            if not dropping:
                kept.append(line)
            continue
        dropping = line.partition(b":")[0].strip().lower() in names
        if not dropping:
            kept.append(line)
    for h in headers:
        name = str(h.get("name") or "").strip()
        value = " ".join(str(h.get("value") or "").split())
        if name:
            kept.append(name.encode("ascii", "ignore") + b": " + value.encode("ascii", "ignore"))
    return newline.join(kept) + rest


def with_recipient_headers(mime: bytes, *, to: list[str], cc: list[str], bcc: list[str] | None = None) -> bytes:
    """*mime* (a draft as Graph's ``$value`` gives it) addressed to *to*, *cc* and *bcc*.

    A draft sent as its MIME through ``sendMail`` reaches whom its ``To``,
    ``Cc`` and ``Bcc`` headers name: they are written from the recipients the
    tools set on the draft, so an override always reaches the sent message's
    headers. An empty *cc* or *bcc* removes that header; a ``None`` *bcc*
    (not known) leaves the MIME's ``Bcc`` as it is; the body is not touched.
    """
    headers = [{"name": "To", "value": ", ".join(to)}]
    if cc:
        headers.append({"name": "Cc", "value": ", ".join(cc)})
    if bcc:
        headers.append({"name": "Bcc", "value": ", ".join(bcc)})
    addressed = with_extra_headers(mime, headers)
    if not cc:
        addressed = _without_header(addressed, b"cc")
    if bcc is not None and not bcc:
        addressed = _without_header(addressed, b"bcc")
    return addressed


def _without_header(mime: bytes, name: bytes) -> bytes:
    """*mime* without its header *name* (lower case), folded lines included."""
    end = _HEADER_END.search(mime)
    head, rest = (mime[: end.start()], mime[end.start() :]) if end else (mime, b"")
    newline = b"\r\n" if b"\r\n" in head else b"\n"
    kept: list[bytes] = []
    dropping = False
    for line in head.split(newline):
        if line[:1] in (b" ", b"\t"):
            if not dropping:
                kept.append(line)
            continue
        dropping = line.partition(b":")[0].strip().lower() == name
        if not dropping:
            kept.append(line)
    return newline.join(kept) + rest


def same_domain(address: str, own_address: str) -> bool:
    """Whether *address* is on the domain of *own_address* (the mailbox's): a forward to trust.

    A message's quoted forward header is body text any sender can write, and
    a custom header can be set by any sender too: only the message's real
    sender, on the mailbox owner's own domain, makes the header trustworthy.
    """
    domain = (address or "").strip().lower().rpartition("@")[2]
    own = (own_address or "").strip().lower().rpartition("@")[2]
    return bool(domain and own and "@" in (address or "") and domain == own)


def is_forward(subject: str) -> bool:
    """Whether *subject* is a forward's (``FW:``, ``Fwd:``, ``TR:``...)."""
    return bool(_FORWARD_PREFIX.match(subject or ""))


def parse_addresses(value: str | list[str] | None) -> list[str]:
    """Comma- or semicolon-separated addresses (or a list of them), blanks dropped."""
    if value is None:
        return []
    items = value if isinstance(value, list) else re.split(r"[,;]", value)
    return [item.strip() for item in items if item and item.strip()]


def graph_recipients(addresses: list[str]) -> list[dict[str, dict[str, str]]]:
    """Addresses as Graph ``recipient`` objects."""
    return [{"emailAddress": {"address": address}} for address in addresses]


def message_time(message: dict) -> str:
    """When a Graph message entered the mailbox: ``receivedDateTime``, else ``sentDateTime`` (ISO-8601 UTC)."""
    return str(message.get("receivedDateTime") or message.get("sentDateTime") or "")


def latest_message(messages: list[dict]) -> dict | None:
    """The newest message of a conversation listing, sent or received, drafts left out.

    A reply is made on it: a follow-up after this mailbox's own reply quotes
    that reply, not the older message it answered. Graph writes every time in
    UTC with the same layout, so the ISO strings compare as times. ``None``
    when the listing holds no message with an id.
    """
    best: dict | None = None
    best_at = ""
    for message in messages:
        if not isinstance(message, dict) or message.get("isDraft") is True or not message.get("id"):
            continue
        at = message_time(message)
        if best is None or at > best_at:
            best, best_at = message, at
    return best


def _graph_addresses(recipients: object) -> list[str]:
    if not isinstance(recipients, list):
        return []
    found = (((r or {}).get("emailAddress") or {}).get("address") if isinstance(r, dict) else None for r in recipients)
    return [str(a).strip() for a in found if a and str(a).strip()]


def sender_address(message: dict) -> str:
    """The address a Graph message's ``from`` names (``""`` when none)."""
    addresses = _graph_addresses([message.get("from")])
    return addresses[0] if addresses else ""


def own_reply_recipients(message: dict, *, own_address: str) -> tuple[list[str], list[str]]:
    """``(to, cc)`` of a follow-up made on this mailbox's own sent *message*.

    Outlook's ``createReply`` on one's own sent message addresses the mailbox
    itself: the follow-up goes instead to the people that message went to, its
    To kept in To and its Cc in Cc, the mailbox left out.
    """
    own = {own_address.lower()} if own_address else set()
    to = _dedupe(_graph_addresses(message.get("toRecipients")), drop=own)
    cc = _dedupe(_graph_addresses(message.get("ccRecipients")), drop=own | {a.lower() for a in to})
    return to, cc
