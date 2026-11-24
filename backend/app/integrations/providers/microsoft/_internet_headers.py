"""The internet headers of an Outlook message, as ``get_microsoft_email(include_headers=True)`` answers them.

Graph returns ``internetMessageHeaders`` as a list of ``{name, value}`` pairs
only when ``$select`` names it. Why a message reads as automated
(List-Unsubscribe, Precedence, Auto-Submitted) or where it came from
(Received, Authentication-Results) is only in them. A message can carry
hundreds of Received and ARC lines, so the block is bounded: at most
``MAX_HEADERS`` headers, each value cut at ``MAX_HEADER_VALUE_CHARS``, the cut
said, with the way to every header whole: ``export_microsoft_email_mime``.
"""

from __future__ import annotations

MAX_HEADERS = 60
MAX_HEADER_VALUE_CHARS = 300

#: What a cut block adds: where the headers are read whole.
FULL_HEADERS_HINT = "Every header whole, and the raw message: export_microsoft_email_mime with this email's ID."


def header_section(mime: bytes) -> str:
    """The header section of a raw RFC 5322 message, unfolded lines kept as sent.

    It ends at the first empty line; a message without one is headers alone.
    """
    text = mime.decode("utf-8", errors="replace").replace("\r\n", "\n")
    head, _, _ = text.partition("\n\n")
    return head


def format_internet_headers(headers: object) -> str:
    """The ``Internet headers:`` block of a read, bounded; a message without any says so."""
    pairs = [h for h in headers if isinstance(h, dict)] if isinstance(headers, list) else []
    if not pairs:
        return "\nInternet headers: none returned for this message.\n"
    lines = []
    cut = len(pairs) > MAX_HEADERS
    for header in pairs[:MAX_HEADERS]:
        name = str(header.get("name") or "").strip()
        value = " ".join(str(header.get("value") or "").split())
        if len(value) > MAX_HEADER_VALUE_CHARS:
            value = f"{value[:MAX_HEADER_VALUE_CHARS]}... [{len(value) - MAX_HEADER_VALUE_CHARS} chars cut]"
            cut = True
        lines.append(f"{name}: {value}")
    shown = len(lines)
    head = f"\nInternet headers ({shown} of {len(pairs)}"
    head += f", the first {MAX_HEADERS} shown):\n" if len(pairs) > MAX_HEADERS else "):\n"
    return head + "\n".join(lines) + "\n" + (f"{FULL_HEADERS_HINT}\n" if cut else "")
