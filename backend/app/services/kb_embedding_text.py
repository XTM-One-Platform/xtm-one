# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""What a knowledge-base document is chunked and embedded as.

A document's stored ``content`` is its SOURCE: what ``get_knowledge_document``
returns verbatim and what an update replaces. What gets chunked and embedded
is derived from it here, at processing time, by the knowledge base's
``html_ingestion`` setting:

- ``source`` (the default, and every knowledge base created before the
  setting): the stored content as is - byte-identical to the behaviour before
  the setting existed.
- ``rendered``: an HTML document is embedded as the text a reader gets for it,
  as ``attachment_extract.extract_html_document`` reads it (styles, scripts
  and SVG geometry left out): its title, the data its scripts embed
  summarised, then the page text in reading order. A self-contained HTML
  export is otherwise mostly ``<path d=...>`` markup before its first
  sentence.

Rendering never cuts a page's text: a page the reader could not read whole
keeps its source, like a page with neither visible text nor data and a
rendering error. Its data is a summary, within the reader's bounds (the first
``MAX_EMBEDDED_BLOCKS`` blocks, each summary bounded): that is how the data
reads on the page, and never a reason to lose the page's text.

A PDF a knowledge base with ``describe_figures`` described (``kb_figures``) is
embedded as its text layer with each page's figure description inserted where
that page's text ends: the caller passes the document's ``figure_reading`` as
*figures* when its knowledge base has the setting on, and a reading made for
other content inserts nothing.

Every place that chunks a knowledge-base document goes through
:func:`resolve_embedding_text` - the document processor (upload, URL crawl,
sync, update, reprocess all end there), the agent's ``store_knowledge`` and
the workset chunk addressing, which re-chunks a stored document to recover
chunk ordinals and must see the text that was embedded. Because the choice is
made when a document is processed, changing the setting re-embeds nothing on
its own: it applies to documents processed afterwards, and an explicit
reprocess applies it to the rest.

The workset derives that text again on every search and every chunk read, so
a rendered reading is memoised per content in the attachment reader's memo,
under the bound its renderings already share; and a page longer than the
reader reads keeps its source without being parsed at all.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from app.core.utils import format_exception_message

if TYPE_CHECKING:
    from app.services.attachment_extract import HtmlExtraction

logger = logging.getLogger(__name__)

HTML_INGESTION_SOURCE = "source"
HTML_INGESTION_RENDERED = "rendered"
HTML_INGESTION_MODES: tuple[str, ...] = (HTML_INGESTION_SOURCE, HTML_INGESTION_RENDERED)

#: How much of a document the HTML sniff reads.
_SNIFF_CHARS = 4096

#: What may come before a whole HTML document's first tag: a byte-order mark,
#: whitespace, comments and an XML declaration - never text.
_HTML_PROLOGUE_RE = re.compile(r"(?:\ufeff|\s|<!--.*?-->|<\?xml[^>]*>)*", re.DOTALL)
#: A whole HTML document: a doctype or one of the document-level elements.
_HTML_DOCUMENT_RE = re.compile(r"<(?:!doctype\s+html|html[\s>]|head[\s>]|body[\s>])", re.IGNORECASE)
#: Any element tag, for a file NAMED as HTML that holds a fragment.
_ANY_TAG_RE = re.compile(r"<[a-zA-Z][a-zA-Z0-9-]*(?:\s[^<>]*)?/?>")
#: File types that are never an HTML page, even when they quote one near
#: their start (a JSON export of e-mails, a Markdown page about HTML, a script
#: that builds markup): the text and code formats ``builtin_tools.media``
#: reads as source text, HTML itself excepted (a test keeps the two aligned).
_NOT_HTML_EXTENSIONS = frozenset(
    {
        "json", "jsonl", "ndjson", "geojson", "jsonld", "csv", "tsv", "md", "markdown", "mdx",
        "txt", "text", "rst", "xml", "svg", "py", "js", "mjs", "cjs", "ts", "tsx", "jsx",
        "yaml", "yml", "toml", "ini", "cfg", "conf", "env", "log", "sh", "bash", "ps1",
        "bat", "sql", "css", "scss", "less", "yar", "yara", "rules", "sigma", "stix", "tex",
        "rtf", "srt", "vtt", "ics", "eml", "mbox", "gitignore", "dockerfile",
    }
)

_RENDERED_NOTE = "as its rendered page text"
_NO_CONTENT_NOTE = "from its HTML source: the page has no visible text or data"
_PARTIAL_NOTE = "from its HTML source: the page is too large to render whole"
_ERROR_NOTE = "from its HTML source: the page could not be rendered"


@dataclass(frozen=True)
class EmbeddingText:
    """What a document is chunked and embedded as.

    ``html_note`` says which reading of an HTML document a ``rendered``
    knowledge base embedded (its rendered page text, or its source and why) -
    the clause the document processor adds to the document's
    ``embedding_completed`` activity. ``None`` wherever the setting does not
    apply (a ``source`` knowledge base, a document that is not HTML), so the
    activity of those documents reads as it always did.
    """

    text: str
    html_note: str | None = None
    #: The figure pass's clause for the ``embedding_completed`` activity of a
    #: document whose figures were (or could not all be) described.
    figure_note: str | None = None


def _file_name(filename: str | None) -> str:
    """*filename* as its type is read from: a URL's path, without its query or
    fragment (``page.html?download=1`` is an ``.html`` file); any other name as is."""
    name = (filename or "").strip().lower()
    if not name.startswith(("http://", "https://")):
        return name
    try:
        return urlsplit(name).path
    except ValueError:  # a malformed host, e.g. an unclosed "[": read as written
        return name


def _is_text_or_data_file(filename: str | None) -> bool:
    name = _file_name(filename)
    return "." in name and name.rsplit(".", 1)[-1] in _NOT_HTML_EXTENSIONS


def is_html_document(content: str | None, filename: str | None) -> bool:
    """Whether *content* is HTML markup worth rendering.

    A knowledge-base document carries no MIME type, and its filename can be a
    page title or a URL, so the content decides: a whole HTML document (one
    that STARTS with a doctype, ``<html>``, ``<head>`` or ``<body>``, after at
    most a byte-order mark, whitespace, comments and an XML declaration), or,
    in a file named ``.html`` / ``.htm`` / ``.xhtml``, a fragment that starts
    with a tag after that same prologue. Text that
    quotes a page - a note showing its markup, a JSON value holding one - is
    not one. A file named as a data, text or code format (JSON, CSV, Markdown,
    plain text, a script...) is never HTML, whatever it quotes, and text a sync
    source has already converted (Markdown from a crawl, a Notion page) is
    never re-rendered. A URL name is typed by its path, so a query string or
    a fragment never hides an extension.
    """
    if not content or _is_text_or_data_file(filename):
        return False
    head = content[:_SNIFF_CHARS]
    start = _HTML_PROLOGUE_RE.match(head).end()
    if _HTML_DOCUMENT_RE.match(head, start):
        return True
    from app.services.attachment_extract import is_html_attachment

    # A fragment starts with a tag too: Markdown a crawl or a sync source
    # stored under an ``.html`` URL starts with text, whatever tags it quotes.
    return is_html_attachment(_file_name(filename)) and bool(_ANY_TAG_RE.match(head, start))


def normalize_html_ingestion(value: str | None) -> str:
    """The stored mode, ``source`` for anything unknown or unset."""
    return value if value in HTML_INGESTION_MODES else HTML_INGESTION_SOURCE


def renders_html(content: str, filename: str | None, html_ingestion: str | None) -> bool:
    """Whether a knowledge base with *html_ingestion* embeds this document rendered.

    The file name is part of the answer (a fragment in a ``.html`` file, a
    JSON or Markdown file quoting HTML), so a rename can change it.
    """
    return normalize_html_ingestion(html_ingestion) == HTML_INGESTION_RENDERED and is_html_document(
        content, filename
    )


def _indexed_page(doc: HtmlExtraction) -> str:
    """The page as a knowledge base indexes it.

    The reading an HTML attachment gets, without what only a reader of a
    conversation FILE can act on: the account of what was left out and the
    pointers to ``query_file_json`` / ``read_file_content``. A knowledge-base
    document is not a conversation file, so those pointers would send an
    agent that finds the passage to tools that cannot open it. Every data
    block is summarised within its own bound, in document order.

    Like every attachment rendering, the result is UTF-8-encodable: a data
    block's escape can parse to half a UTF-16 pair, which the embedding call
    and the insert of the chunk both refuse.
    """
    from app.services.attachment_extract import JSON_BLOCK_SUMMARY_MAX_CHARS, _encodable, summarize_json

    parts = [f"# {doc.title}"] if doc.title else []
    data = [
        f"### {block.label}\n"
        + summarize_json(block.data, max_chars=JSON_BLOCK_SUMMARY_MAX_CHARS, query_hint=False)
        for block in doc.json_blocks
        if block.error is None
    ]
    if data:
        parts.append("## Embedded data\n\n" + "\n\n".join(data))
        if doc.text.strip():
            parts.append("## Page text\n\n" + doc.text)
    else:
        parts.append(doc.text)
    return _encodable("\n\n".join(parts))


def _read_in_part(content: str) -> bool:
    """Whether the reader would tokenize only part of *content*.

    Such a page keeps its source whatever it holds, so its length decides:
    tokenizing its first ``HTML_RENDER_MAX_CHARS`` characters - seconds of the
    parse pool on hostile markup - only to discard them would be the whole
    cost of reading it.
    """
    from app.services.attachment_extract import HTML_RENDER_MAX_CHARS

    return len(content) > HTML_RENDER_MAX_CHARS


def _memo_key(content: str) -> tuple:
    """The attachment reader's memo key for the rendered reading of *content*,
    which depends on nothing else: the file name and the setting only decide
    WHETHER a document is rendered."""
    from app.services.attachment_extract import RENDER_VERSION

    data = content.encode("utf-8", "surrogatepass")
    return ("kb-page", RENDER_VERSION, len(data), hashlib.blake2b(data, digest_size=16).digest())


def _with_figures(content: str, figures: object) -> EmbeddingText | None:
    """*content* with its figure descriptions, when *figures* has some for it."""
    if figures is None:
        return None
    from app.services.kb_figures import activity_note, apply_figure_reading, content_digest, figure_state

    digest = content_digest(content)  # once: a large document is hashed once
    if figure_state(figures, content, digest=digest)[0] is None:
        return None
    return EmbeddingText(
        apply_figure_reading(content, figures, digest=digest),
        figure_note=activity_note(figures, content, digest=digest),
    )


def resolve_embedding_text(
    content: str, filename: str | None, html_ingestion: str | None, *, figures: object = None
) -> EmbeddingText:
    """The text a document is chunked and embedded as (see the module docstring).

    Rendering never fails a document and never cuts its text: a page read
    only in part (past ``HTML_RENDER_MAX_CHARS`` of HTML - decided on its
    length, before anything is parsed - or ``HTML_TEXT_MAX_CHARS`` of text), a
    page with neither visible text nor data it could read, and a rendering
    error all fall back to the stored source, which is exactly what a
    ``source`` knowledge base embeds. A data block past the reader's bounds is
    left out of the summary, like the rows a summary does not show. A rendered
    reading is memoised per content (see the module docstring).

    *figures* is the document's ``figure_reading`` when its knowledge base
    describes figures: a PDF is never HTML, so its descriptions decide.
    """
    if (described := _with_figures(content, figures)) is not None:
        return described
    if not renders_html(content, filename, html_ingestion):
        return EmbeddingText(content)
    if _read_in_part(content):
        return EmbeddingText(content, _PARTIAL_NOTE)
    from app.services.attachment_extract import _cache_get, _cache_put, extract_html_document

    key = _memo_key(content)
    page = _cache_get(key)
    if page is not None:
        return EmbeddingText(page, _RENDERED_NOTE)
    try:
        doc = extract_html_document(content)
        if doc.unread_source_chars or doc.text_cut_chars:
            return EmbeddingText(content, _PARTIAL_NOTE)
        if not doc.text.strip() and all(block.error is not None for block in doc.json_blocks):
            return EmbeddingText(content, _NO_CONTENT_NOTE)
        page = _indexed_page(doc)
    except Exception as exc:
        logger.warning(
            "Rendering HTML document %r for embedding failed, embedding its source: %s",
            filename,
            format_exception_message(exc),
        )
        return EmbeddingText(content, _ERROR_NOTE)
    _cache_put(key, page)
    return EmbeddingText(page, _RENDERED_NOTE)


async def resolve_embedding_text_async(
    content: str, filename: str | None, html_ingestion: str | None, *, figures: object = None
) -> EmbeddingText:
    """:func:`resolve_embedding_text` off the event loop when it has to render.

    Rendering a large page is CPU-bound, so it runs on the parse executor;
    every other document - a page too long to read whole
    included - returns at once. Inserting figure descriptions is a string
    splice: it runs here.
    """
    if (described := _with_figures(content, figures)) is not None:
        return described
    if not renders_html(content, filename, html_ingestion):
        return EmbeddingText(content)
    if _read_in_part(content):
        return EmbeddingText(content, _PARTIAL_NOTE)
    from app.core.parse_offload import run_parse

    return await run_parse(resolve_embedding_text, content, filename, html_ingestion)


def embedding_text_for(
    content: str, filename: str | None, html_ingestion: str | None, *, figures: object = None
) -> str:
    """The text of :func:`resolve_embedding_text`."""
    return resolve_embedding_text(content, filename, html_ingestion, figures=figures).text


async def embedding_text_for_async(
    content: str, filename: str | None, html_ingestion: str | None, *, figures: object = None
) -> str:
    """The text of :func:`resolve_embedding_text_async`."""
    return (await resolve_embedding_text_async(content, filename, html_ingestion, figures=figures)).text
