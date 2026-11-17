# SPDX-License-Identifier: Apache-2.0

"""Shared HTML / Markdown → DOCX rendering.

Single source of truth for turning agent-authored Markdown / HTML into a
real Word (OOXML) document via ``python-docx``. Used by:

* ``services/builtin_tools/media.py`` — the ``generate_file`` tool
  (downloadable ``.docx`` artifacts in chat / assignments).

Why this module exists: LLMs cannot emit valid OOXML ZIP binary inline, so
before this renderer existed a ``generate_file`` call targeting ``.docx``
fell through to a plain UTF-8 byte-encode — the "DOCX" the user downloaded
was actually Markdown / plain text (or even python-docx *source code* from
a code-interpreter workaround) saved under a ``.docx`` name, and Word
refused to open it. This mirrors the existing
``pdf_render`` approach: the agent authors the *content* in its natural
format (Markdown / HTML) and the backend performs the binary conversion.

The Markdown → HTML step reuses :func:`pdf_render.render_markdown_to_html`
so PDF and DOCX outputs are rendered from the exact same intermediate HTML.
The HTML is then walked with BeautifulSoup and mapped onto the built-in
styles of python-docx's default template (Heading 1-6, List Bullet/Number
1-3, Quote, Table Grid), which Word ships out of the box.

Rendering is pure CPU work; the public coroutine offloads to a worker
thread via ``asyncio.to_thread`` so large reports never block the event
loop.
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bs4 import Tag
    from docx.document import Document as DocumentObject
    from docx.text.paragraph import Paragraph
    from docx.text.run import Run

logger = logging.getLogger(__name__)

#: Canonical OOXML Word MIME type — matches ``file_storage.MIME_TYPES["docx"]``.
DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

#: Monospace font + size used for inline code and fenced code blocks.
_MONO_FONT = "Consolas"
_CODE_SIZE_PT = 9

#: Standard Office hyperlink blue (matches Word's built-in Hyperlink style).
_HYPERLINK_COLOR = (0x05, 0x63, 0xC1)

#: URL schemes allowed to become live ``w:hyperlink`` relationships. The
#: content is agent/user-influenced and the document is opened locally in
#: Word, so anything else (``file:``, ``javascript:``, custom protocol
#: handlers) degrades to plain text with the URL spelled out instead of a
#: clickable link.
_SAFE_LINK_SCHEMES = frozenset({"http", "https", "mailto"})

#: Block-level container tags that are transparently recursed into.
_CONTAINER_TAGS = frozenset(
    {"div", "section", "article", "main", "header", "footer", "figure", "span"}
)

#: Inline formatting state keys (see ``_add_run``).
_InlineFormat = dict[str, bool]


# ---------------------------------------------------------------------------
# Inline content
# ---------------------------------------------------------------------------


def _add_run(paragraph: Paragraph, text: str, fmt: _InlineFormat) -> Run:
    """Append a run with the accumulated inline formatting applied."""
    from docx.shared import Pt

    run = paragraph.add_run(text)
    if fmt.get("bold"):
        run.bold = True
    if fmt.get("italic"):
        run.italic = True
    if fmt.get("underline"):
        run.underline = True
    if fmt.get("strike"):
        run.font.strike = True
    if fmt.get("code"):
        run.font.name = _MONO_FONT
        run.font.size = Pt(_CODE_SIZE_PT)
    return run


def _is_safe_link_target(url: str) -> bool:
    """True when ``url`` carries an allow-listed scheme (see
    ``_SAFE_LINK_SCHEMES``). Scheme-less values (relative paths, bare
    anchors) are rejected too — they are meaningless in a standalone
    downloaded document."""
    from urllib.parse import urlsplit

    try:
        scheme = urlsplit(url).scheme
    except ValueError:
        return False
    return scheme.lower() in _SAFE_LINK_SCHEMES


def _add_hyperlink(
    paragraph: Paragraph, url: str, text: str, fmt: _InlineFormat
) -> None:
    """Append a live hyperlink run (real ``w:hyperlink`` element).

    Falls back to plain styled text with the URL in parentheses when the
    scheme is not allow-listed (``file:``, ``javascript:``, custom
    protocol handlers — unsafe to embed as clickable links in a document
    opened locally) or when the relationship cannot be created (malformed
    URL), so the link target is never silently lost.
    """
    from docx.opc.constants import RELATIONSHIP_TYPE
    from docx.oxml.ns import qn
    from docx.oxml.parser import OxmlElement
    from docx.shared import RGBColor

    display = text or url
    if not url:
        _add_run(paragraph, display, fmt)
        return
    if not _is_safe_link_target(url):
        suffix = f" ({url})" if display != url else ""
        _add_run(paragraph, f"{display}{suffix}", fmt)
        return

    try:
        r_id = paragraph.part.relate_to(
            url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True
        )
    except Exception:
        suffix = f" ({url})" if display != url else ""
        _add_run(paragraph, f"{display}{suffix}", fmt)
        return

    run = _add_run(paragraph, display, fmt)
    run.font.underline = True
    run.font.color.rgb = RGBColor(*_HYPERLINK_COLOR)

    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)
    run_el = run._r
    run_el.getparent().remove(run_el)
    hyperlink.append(run_el)
    paragraph._p.append(hyperlink)


#: Max embedded-image width (inches). Caps a large @2x diagram to the usable
#: width of a default A4/Letter page (~6.3"); python-docx scales height to keep
#: the aspect ratio. Small images keep their natural size.
_MAX_IMAGE_WIDTH_IN = 6.0


def _add_data_uri_image(paragraph: Paragraph, src: str) -> bool:
    """Embed a ``data:image/...;base64,...`` URI as an inline picture.

    Returns ``True`` on success, ``False`` (caller falls back to alt text) for a
    malformed/non-base64 URI or a format python-docx cannot embed (e.g. SVG).
    """
    import base64
    import io

    from docx.shared import Inches

    try:
        header, _, b64data = src.partition(",")
        if ";base64" not in header.lower() or not b64data:
            return False
        raw = base64.b64decode(b64data, validate=True)
    except (ValueError, TypeError):
        return False
    if not raw:
        return False
    # Broad guard on purpose: python-docx raises a family of image-parsing
    # errors (UnrecognizedImageError, UnexpectedEndOfFileError, ...) plus
    # ValueError on bad widths. Any failure must degrade to alt text, never
    # abort the whole document render.
    try:
        run = paragraph.add_run()
        stream = io.BytesIO(raw)
        try:
            run.add_picture(stream, width=Inches(_MAX_IMAGE_WIDTH_IN))
        except ValueError:
            # add_picture rejects a zero/oversized width on some images; retry
            # at native size before giving up.
            stream.seek(0)
            run.add_picture(stream)
        return True
    except Exception:
        # Best-effort embed - any parse/format failure degrades to alt text.
        logger.warning("Could not embed data: image in DOCX", exc_info=True)
        return False


def _add_inline(paragraph: Paragraph, node: object, fmt: _InlineFormat) -> None:
    """Recursively render an inline HTML node into ``paragraph``."""
    from bs4 import NavigableString, Tag

    if isinstance(node, NavigableString):
        text = str(node)
        if text:
            _add_run(paragraph, text, fmt)
        return
    if not isinstance(node, Tag):
        return

    name = (node.name or "").lower()
    if name == "br":
        paragraph.add_run().add_break()
        return
    if name == "a":
        _add_hyperlink(paragraph, node.get("href") or "", node.get_text(), fmt)
        return
    if name == "img":
        # Inline data: images (e.g. a diagram generate_file inlined from a
        # file handle) embed as a real picture. External images cannot be
        # fetched (and should not be — SSRF), so they degrade to the alt text.
        src = node.get("src") or ""
        if src.startswith("data:image/") and _add_data_uri_image(paragraph, src):
            return
        alt = node.get("alt")
        if alt:
            _add_run(paragraph, f"[{alt}]", fmt)
        return

    new_fmt = dict(fmt)
    if name in ("strong", "b"):
        new_fmt["bold"] = True
    elif name in ("em", "i"):
        new_fmt["italic"] = True
    elif name in ("code", "tt", "kbd", "samp"):
        new_fmt["code"] = True
    elif name in ("del", "s", "strike"):
        new_fmt["strike"] = True
    elif name == "u":
        new_fmt["underline"] = True

    for child in node.children:
        _add_inline(paragraph, child, new_fmt)


def _add_inline_children(
    paragraph: Paragraph, node: Tag, fmt: _InlineFormat | None = None
) -> None:
    for child in node.children:
        _add_inline(paragraph, child, fmt or {})


# ---------------------------------------------------------------------------
# Block content
# ---------------------------------------------------------------------------


def _styled_paragraph(doc: DocumentObject, style_name: str) -> Paragraph:
    """``add_paragraph`` with a style fallback — a missing style (custom
    template without the built-in set) must never abort the render."""
    try:
        return doc.add_paragraph(style=style_name)
    except KeyError:
        return doc.add_paragraph()


def _render_list(doc: DocumentObject, node: Tag, *, ordered: bool, depth: int) -> None:
    from bs4 import Tag as _Tag

    base = "List Number" if ordered else "List Bullet"
    # Default template ships levels 1-3 ("List Bullet", "List Bullet 2", …).
    level = min(depth, 2)
    style_name = base if level == 0 else f"{base} {level + 1}"

    for li in node.find_all("li", recursive=False):
        paragraph = _styled_paragraph(doc, style_name)
        for child in li.children:
            if isinstance(child, _Tag) and child.name in ("ul", "ol"):
                continue  # nested lists are rendered after the item text
            if isinstance(child, _Tag) and child.name == "p":
                _add_inline_children(paragraph, child)
            else:
                _add_inline(paragraph, child, {})
        for sub_list in li.find_all(["ul", "ol"], recursive=False):
            _render_list(doc, sub_list, ordered=sub_list.name == "ol", depth=depth + 1)


def _render_table(doc: DocumentObject, node: Tag) -> None:
    import contextlib

    # Scope rows to THIS table — a recursive find_all would also pick up
    # the rows of any table nested inside a cell and splice them into the
    # outer table (duplicating content and inflating the column count).
    rows = [tr for tr in node.find_all("tr") if tr.find_parent("table") is node]
    if not rows:
        return
    n_cols = max(len(r.find_all(["td", "th"], recursive=False)) for r in rows)
    if n_cols == 0:
        return

    table = doc.add_table(rows=0, cols=n_cols)
    with contextlib.suppress(KeyError):
        table.style = "Table Grid"

    for row_node in rows:
        cells = row_node.find_all(["td", "th"], recursive=False)
        row = table.add_row()
        for idx, cell_node in enumerate(cells):
            if idx >= n_cols:
                break
            cell_paragraph = row.cells[idx].paragraphs[0]
            fmt: _InlineFormat = {"bold": True} if cell_node.name == "th" else {}
            _add_inline_children(cell_paragraph, cell_node, fmt)


def _render_pre(doc: DocumentObject, node: Tag) -> None:
    text = node.get_text().rstrip("\n")
    paragraph = _styled_paragraph(doc, "No Spacing")
    lines = text.split("\n")
    for i, line in enumerate(lines):
        run = _add_run(paragraph, line, {"code": True})
        if i < len(lines) - 1:
            run.add_break()


def _render_blockquote(doc: DocumentObject, node: Tag) -> None:
    from bs4 import Tag as _Tag

    blocks = [
        child
        for child in node.children
        if isinstance(child, _Tag) and child.name in ("p", "div")
    ]
    if not blocks:
        paragraph = _styled_paragraph(doc, "Quote")
        _add_inline_children(paragraph, node)
        return
    for block in blocks:
        paragraph = _styled_paragraph(doc, "Quote")
        _add_inline_children(paragraph, block)


def _add_horizontal_rule(doc: DocumentObject) -> None:
    from docx.oxml.ns import qn
    from docx.oxml.parser import OxmlElement

    paragraph = doc.add_paragraph()
    p_pr = paragraph._p.get_or_add_pPr()
    p_bdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "auto")
    p_bdr.append(bottom)
    p_pr.append(p_bdr)


def _render_block(doc: DocumentObject, node: Tag) -> None:
    name = (node.name or "").lower()

    if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        heading = doc.add_heading(level=int(name[1]))
        _add_inline_children(heading, node)
    elif name == "p":
        paragraph = doc.add_paragraph()
        _add_inline_children(paragraph, node)
    elif name in ("ul", "ol"):
        _render_list(doc, node, ordered=name == "ol", depth=0)
    elif name == "table":
        _render_table(doc, node)
    elif name == "pre":
        _render_pre(doc, node)
    elif name == "blockquote":
        _render_blockquote(doc, node)
    elif name == "hr":
        _add_horizontal_rule(doc)
    elif name in ("script", "style", "head", "meta", "link", "title"):
        return  # non-content
    elif name in _CONTAINER_TAGS or name in ("html", "body"):
        _render_children(doc, node)
    else:
        # Unknown tag — degrade to a paragraph with the inline content so
        # text is never silently dropped.
        text = node.get_text(strip=True)
        if text:
            paragraph = doc.add_paragraph()
            _add_inline_children(paragraph, node)


def _render_children(doc: DocumentObject, node: Tag) -> None:
    from bs4 import NavigableString
    from bs4 import Tag as _Tag

    for child in node.children:
        if isinstance(child, NavigableString):
            if str(child).strip():
                paragraph = doc.add_paragraph()
                _add_inline(paragraph, child, {})
        elif isinstance(child, _Tag):
            _render_block(doc, child)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _build_docx(content: str, title: str | None) -> bytes:
    """Synchronous worker: Markdown/HTML → DOCX bytes."""
    from bs4 import BeautifulSoup
    from docx import Document

    from app.services.pdf_render import looks_like_html, render_markdown_to_html

    html: str | None
    if looks_like_html(content):
        html = content
    else:
        try:
            html = render_markdown_to_html(content)
        except Exception:
            logger.warning(
                "Markdown rendering failed; falling back to plain paragraphs",
                exc_info=True,
            )
            html = None

    doc = Document()
    if title:
        doc.core_properties.title = title

    if html is None:
        for line in content.splitlines():
            doc.add_paragraph(line)
    else:
        soup = BeautifulSoup(html, "html.parser")
        _render_children(doc, soup.body or soup)
        if not doc.paragraphs and not doc.tables and content.strip():
            # Defensive: the walk produced nothing visible — keep the raw
            # text rather than handing the user an empty document.
            for line in content.splitlines():
                doc.add_paragraph(line)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


async def render_text_to_docx(content: str, *, title: str | None = None) -> bytes:
    """Render text / Markdown / HTML content to DOCX bytes.

    Mirrors :func:`pdf_render.render_text_to_pdf`: the caller hands over
    agent-authored content in its natural format and receives a real OOXML
    Word document. Failure modes (malformed HTML, python-docx errors) raise
    a real exception that callers catch and surface as a friendly tool
    error so the agent can fall back to Markdown / HTML output.
    """
    return await asyncio.to_thread(_build_docx, content, title)
