"""The text body of an Outlook message, whole or by window, never cut in silence.

``get_microsoft_email`` used to answer the first 3,000 characters of a body and
nothing else: a long message came back cut mid-sentence with no sign of the
cut, so a reader could summarise or flag an email it never read whole. A body
up to ``BODY_WINDOW_CHARS`` is answered whole; a longer one is answered one
window at a time, each window followed by a line naming the characters shown,
the characters left and the ``body_offset`` that reads the next window.
"""

from __future__ import annotations

#: Characters of the body's text one read answers.
BODY_WINDOW_CHARS = 20_000


def format_body_window(text: str, offset: int = 0) -> str:
    """The ``Body:`` block of *text* from *offset*: whole, or a window and the marker that names the rest."""
    text = text or ""
    total = len(text)
    start = max(0, min(int(offset or 0), total))
    end = min(total, start + BODY_WINDOW_CHARS)
    if start == 0 and end == total:
        return f"\nBody:\n{text}\n"
    lines = [f"\nBody (characters {start} to {end} of {total}):", text[start:end]]
    if end < total:
        lines.append(
            f"[Body truncated: {total - end} characters left; call get_microsoft_email again"
            f" with body_offset={end} to read the rest]"
        )
    elif start >= total:
        lines.append(f"[Nothing at body_offset={int(offset or 0)}: the body has {total} characters]")
    else:
        lines.append("[End of body]")
    return "\n".join(lines) + "\n"
