"""The HTML body of a native Outlook reply or forward draft.

``createReply``, ``createReplyAll`` and ``createForward`` build the quoted
history themselves (the separator, the header block, the original's
formatting and images). Graph answers that draft in the body type of the
message answered: a plain-text message gives a ``text`` draft. Patching the
agent's HTML above such a body, with ``contentType: HTML``, collapsed every
line break of the quote into one paragraph.

The draft is therefore asked for in HTML (``Prefer:
outlook.body-content-type="html"``) and re-read in HTML when Graph still
answers text: the HTML is then Exchange's own conversion. When Graph answers
text twice, the quote is kept as Graph wrote it, character for character,
in a block that keeps its line breaks: no separator, header block or marker
is ever written by the tools (Sam, 1 Nov 2026: the quote is the provider's,
never rebuilt).
"""

from __future__ import annotations

import logging
from html import escape as _html_escape
from typing import Any

from ._signature_images import GraphRequest

logger = logging.getLogger(__name__)

#: The ``Prefer`` value that makes Graph answer a message's body as HTML.
HTML_BODY_PREFERENCE = 'outlook.body-content-type="html"'
#: Headers of a request whose answer carries a draft's body (laid over the session's).
PREFER_HTML_BODY = {"Prefer": HTML_BODY_PREFERENCE}


def text_body_as_html(text: str) -> str:
    """A text draft body Graph would not give in HTML, kept as it is: its text escaped, its line breaks kept.

    The last resort when Graph answers text twice. Nothing is added to the
    text and nothing is taken from it: its underscore separator and header
    lines stay the provider's own lines, so a quote can still be cut at a
    message boundary (:func:`._thread_reply.quote_boundaries`).
    """
    escaped = _html_escape((text or "").replace("\r\n", "\n").replace("\r", "\n"), quote=False)
    return (
        '<html><head><meta http-equiv="Content-Type" content="text/html; charset=utf-8"></head>'
        f'<body dir="ltr">\r\n<div style="white-space:pre-wrap">{escaped}</div>\r\n</body></html>'
    )


def is_html_body(body: dict[str, Any] | None) -> bool:
    """Whether a Graph ``body`` is HTML (a body without a type is, as Graph writes mail)."""
    return str((body or {}).get("contentType") or "html").strip().lower() == "html"


async def html_draft_body(
    request: GraphRequest,
    client: Any,
    message_url: str,
    draft: dict[str, Any],
    *,
    provider: str = "microsoft",
) -> str:
    """The HTML of a draft Graph just created (*draft*, the creation's answer), its native quote kept.

    *message_url* is the draft's Graph URL. An HTML body is returned as it is;
    a text one is read again in HTML, and kept as Graph's text
    (:func:`text_body_as_html`) when Graph still answers text (or the read fails).
    """
    body = draft.get("body") or {}
    content = str(body.get("content") or "")
    if is_html_body(body):
        return content
    resp, err = await request(
        client,
        "get",
        message_url,
        provider,
        "read draft as HTML",
        params={"$select": "body"},
        headers=PREFER_HTML_BODY,
    )
    if not err and resp is not None:
        try:
            reread = (resp.json() or {}).get("body") or {}
        except ValueError:
            reread = {}
        if is_html_body(reread) and reread.get("contentType") and reread.get("content"):
            return str(reread["content"])
    logger.info("A draft's body stays text in HTML; its quote is kept as Graph's text.")
    return text_body_as_html(content)
