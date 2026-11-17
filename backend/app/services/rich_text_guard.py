# SPDX-License-Identifier: Apache-2.0

"""One definition of "unsafe markup" for every agent-authored ``rich_text``.

``rich_text`` is the single field an agent may fill with markup in a report.
The spec rejects the constructs listed here, and the fragment is
sanitized again with the DOMPurify strict profile at render time
(``frontend/src/components/presentation/sanitize.ts``).

Decks used to share the field through their v1 layouts; those layouts
are retired and a deck now has NO HTML field at all - every string is
flat inline markup. The guard therefore serves reports only, but keeps its one
list: a tag added to :data:`BANNED_RICH_TEXT_TAGS` tightens every ``rich_text``
consumer in the same commit, and a second consumer of the field tomorrow reads
this list rather than growing its own.

Rejecting rather than stripping is deliberate: the agent is told exactly what to
remove, which produces a correct retry instead of a document whose content
silently disappeared three layers later. Sanitization at render time stays the
enforcing layer — this is defence in depth, not a replacement for it.
"""

from __future__ import annotations

import re

#: Constructs banned from any ``rich_text`` fragment. Mirrors the DOMPurify
#: strict profile on the frontend (block ``script`` / ``iframe`` / ``object`` /
#: ``form`` / ``input`` / ``button`` and every ``on*`` handler + ``srcdoc``), so
#: the two layers agree on what "unsafe" means.
BANNED_RICH_TEXT_TAGS: tuple[str, ...] = (
    "script",
    "iframe",
    "object",
    "embed",
    "form",
    "input",
    "button",
    "textarea",
    "select",
    "style",
    "link",
    "meta",
    "base",
    "svg",
)

_BANNED_TAG_RE = re.compile(
    r"<\s*/?\s*(" + "|".join(BANNED_RICH_TEXT_TAGS) + r")\b", re.IGNORECASE
)
#: ``on…=`` event handlers, plus the attributes that can smuggle a document in.
_BANNED_ATTR_RE = re.compile(
    r"(?:\son[a-z]+\s*=)|(?:\bsrcdoc\s*=)|(?:javascript\s*:)|(?:data\s*:\s*text/html)",
    re.IGNORECASE,
)

#: Paragraph-level markup an agent may use. Quoted verbatim in both tool
#: descriptions and in the rejection message, so the prompt can never advertise
#: a tag the guard refuses.
ALLOWED_RICH_TEXT_SUMMARY = "p, strong, em, ul/ol/li, a, code, br, h3/h4, table"


def check_rich_text(value: str | None) -> str | None:
    """Reject a ``rich_text`` fragment carrying script-capable markup.

    Raises ``ValueError`` (surfaced by Pydantic as a field error) rather than
    stripping: the agent gets told exactly what to remove, which produces a
    correct retry instead of content that silently disappeared.
    """
    if value is None:
        return None
    if _BANNED_TAG_RE.search(value):
        raise ValueError(
            "rich_text must not contain any of these tags: "
            f"{', '.join(BANNED_RICH_TEXT_TAGS)}. Use plain paragraph markup "
            f"({ALLOWED_RICH_TEXT_SUMMARY}) only."
        )
    if _BANNED_ATTR_RE.search(value):
        raise ValueError(
            "rich_text must not contain event-handler attributes (onclick, "
            "onerror, onload, …), srcdoc, javascript: or data:text/html URLs."
        )
    return value
