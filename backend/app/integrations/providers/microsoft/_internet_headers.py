"""The internet headers of an Outlook message, as ``get_microsoft_email(include_headers=True)`` answers them.

Graph returns ``internetMessageHeaders`` as a list of ``{name, value}`` pairs
only when ``$select`` names it. Why a message reads as automated
(List-Unsubscribe, Precedence, Auto-Submitted) or where it came from
(Received, Authentication-Results) is only in them. A message can carry
hundreds of Received and ARC lines, so the block is bounded: at most
``MAX_HEADERS`` headers, each value cut at ``MAX_HEADER_VALUE_CHARS``, the cut
said.
"""

from __future__ import annotations

MAX_HEADERS = 60
MAX_HEADER_VALUE_CHARS = 300


def format_internet_headers(headers: object) -> str:
    """The ``Internet headers:`` block of a read, bounded; a message without any says so."""
    pairs = [h for h in headers if isinstance(h, dict)] if isinstance(headers, list) else []
    if not pairs:
        return "\nInternet headers: none returned for this message.\n"
    lines = []
    for header in pairs[:MAX_HEADERS]:
        name = str(header.get("name") or "").strip()
        value = " ".join(str(header.get("value") or "").split())
        if len(value) > MAX_HEADER_VALUE_CHARS:
            value = f"{value[:MAX_HEADER_VALUE_CHARS]}... [{len(value) - MAX_HEADER_VALUE_CHARS} chars cut]"
        lines.append(f"{name}: {value}")
    shown = len(lines)
    head = f"\nInternet headers ({shown} of {len(pairs)}"
    head += f", the first {MAX_HEADERS} shown):\n" if len(pairs) > MAX_HEADERS else "):\n"
    return head + "\n".join(lines) + "\n"
