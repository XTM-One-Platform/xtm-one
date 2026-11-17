"""The raw markup of an Outlook message, as a diagnosis needs it.

``get_microsoft_email`` answers a message's body as text and its attachments
by name and size: enough to read an email, not to tell why a signature add-in
re-inserts its signature or why a draft's attachments multiply while it is
open in Outlook. With ``include_raw`` it adds this report: the signature
blocks of the body (their opening tags), the add-in markers it carries, the
``cid:`` references against the inline attachments, the file attachments
listed twice under one name and size, every attachment with its
content id and last modification time (oldest first, so copies added in a
burst show together), and a bounded window of the body's HTML.
"""

from __future__ import annotations

import re
from typing import Any

from ._attachment_dedupe import duplicates_line

#: Characters of the body's HTML one read shows.
RAW_WINDOW_CHARS = 12_000
#: Characters of one opening tag shown.
_TAG_CHARS = 300

_SIGNATURE_OPEN_RE = re.compile(r"<div\b[^>]*\bid\s*=\s*[\"'][^\"']*signature[^\"']*[\"'][^>]*>", re.IGNORECASE)
_CID_RE = re.compile(r"\bcid:([^\"'\s>)]+)", re.IGNORECASE)
#: Markers signature add-ins and Outlook leave in a body.
_MARKERS = ("lsi", "letsignit", "data-signature", "elementtoproof", "data-outlook", "x_signature", "appendonsend")


def _marker_counts(html: str) -> dict[str, int]:
    lowered = html.lower()
    return {m: lowered.count(m) for m in _MARKERS if m in lowered}


def raw_markup_report(html: str, attachments: list[dict[str, Any]], offset: int = 0, list_error: str | None = None) -> str:
    """The raw report of a message: *html* its body, *attachments* as Graph lists them with ``contentId``."""
    html = html or ""
    lines = ["", "Raw markup (include_raw):"]
    blocks = _SIGNATURE_OPEN_RE.findall(html)
    lines.append(f"Signature blocks: {len(blocks)}")
    lines.extend(f"- {tag[:_TAG_CHARS]}" for tag in blocks)
    markers = _marker_counts(html)
    lines.append(
        "Markers: " + (", ".join(f"{name} x{count}" for name, count in markers.items()) if markers else "none")
    )
    referenced = [cid.lower() for cid in _CID_RE.findall(html)]
    inline = [a for a in attachments if a.get("isInline")]
    inline_ids = {str(a.get("contentId") or "").strip("<>").lower() for a in inline}
    unreferenced = sum(1 for a in inline if str(a.get("contentId") or "").strip("<>").lower() not in referenced)
    missing = sorted({cid for cid in referenced if cid not in inline_ids})
    lines.append(
        f"cid references: {len(referenced)} ({len(set(referenced))} distinct); inline attachments: {len(inline)},"
        f" {unreferenced} not referenced by the body; references without an attachment: {len(missing)}"
    )
    if list_error:
        lines.append(f"Attachment details: could not be listed ({list_error})")
    else:
        lines.append(duplicates_line(attachments) or "Duplicate file attachments: none")
        ordered = sorted(attachments, key=lambda a: str(a.get("lastModifiedDateTime") or ""))
        lines.append(f"Attachment details ({len(ordered)}, oldest first):")
        for att in ordered:
            lines.append(
                f"- {att.get('lastModifiedDateTime') or '?'} {att.get('name') or 'attachment'}"
                f" ({att.get('size') or 0} B, {'inline' if att.get('isInline') else 'file'},"
                f" content id {str(att.get('contentId') or '').strip('<>') or 'none'})"
                f" - attachment_id: {att.get('id', '')}"
            )
    start = max(0, min(int(offset or 0), len(html)))
    end = min(len(html), start + RAW_WINDOW_CHARS)
    lines.append(f"Body HTML, characters {start} to {end} of {len(html)}:")
    lines.append(html[start:end])
    if end < len(html):
        lines.append(f"[more: call again with raw_offset={end}]")
    return "\n".join(lines) + "\n"
