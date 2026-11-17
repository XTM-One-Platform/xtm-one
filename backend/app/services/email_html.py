# SPDX-License-Identifier: Apache-2.0

"""Email body normalization — Markdown / HTML / plain text -> safe HTML.

Agents author report bodies in Markdown (the correct format for chat and
Teams, which render it). The same Markdown was previously handed verbatim to
the email-send tools (Microsoft Graph ``sendMail`` / draft compose). Outlook
and Outlook/Copilot render mail as **HTML** and do not interpret Markdown, so
GFM tables (``| a | b |``), emphasis (``**bold**``) and links
(``[label](url)``) were delivered as literal characters on a single line —
reports were unreadable in email.

This module is the single, platform-side normalization point: every email
body is turned into a well-formed, sanitized HTML fragment **regardless** of
whether the caller passed Markdown, HTML or plain text. No per-agent prompt
changes are required — it works for all current and future agents.

Pipeline (``markdown_to_email_html``):

1. **Detect** the incoming content type. If the body already contains
   block-level HTML (``<table``, ``<p``, ``<div``, ``<h1>``-``<h6>``, ``<ul``,
   ...) it is treated as HTML and passed through (idempotent — never
   double-converted). Otherwise it is treated as Markdown.
2. **Convert** Markdown -> HTML with the battle-tested ``markdown`` library
   (GFM ``tables``, ``fenced_code``, ``sane_lists``, ``nl2br``). Plain text
   with no Markdown still yields valid HTML (paragraphs + ``<br>``).
3. **Sanitize** with ``nh3`` (Rust ``ammonia`` bindings) against a strict
   allow-list — scripts, event handlers and unsafe URL schemes are stripped.
4. **Filter** every ``style`` attribute: a declaration survives when its
   property is on :data:`_ALLOWED_STYLE_PROPS` and its value carries no
   ``url(``, ``expression(``, script scheme, escape or comment; a style whose
   declarations all survive is kept byte for byte.
5. **Inline-style** generated ``table`` / ``th`` / ``td`` / ``a`` elements so
   tables actually render in Outlook (Outlook ignores ``<style>`` blocks —
   styling must live in ``style=`` attributes on the elements themselves),
   and give every paragraph, list, heading, quote, code block and table
   without a margin of its own a ``margin-bottom:12pt``, so Outlook's
   ``p {margin:0}`` head rule on a reply cannot glue the paragraphs together.

6. **Compose style**: the text of every paragraph, list item, cell and
   heading is wrapped in a ``<span>`` carrying the compose style, and each
   such paragraph gets Outlook's ``elementToProof`` class, the markup Outlook
   writes for a message a person composes (:data:`DEFAULT_COMPOSE_STYLE`:
   Aptos 11pt, ``color:rgb(0,0,0)``). Outlook's dark mode inverts a declared
   black to white, as it does the quoted thread under a reply; text with no
   colour of its own rendered grey there. Only what the author left unset is
   added: a declaration an element or one of its ancestors already sets is
   kept, a heading keeps its size and preformatted text its font. The style is
   set per integration (``compose_style``); ``none`` keeps the theme-neutral
   output.

Inline styles the **author** wrote on an HTML body are theirs and are kept:
colours, backgrounds, fonts, borders, padding, margins, widths. ``<font>``
tags are still dropped. A signature block is added after this normalization
and keeps its own styles.

The reverse — stripping HTML back to text — is intentionally NOT done here;
the goal is to always emit HTML for the email send path.
"""

from __future__ import annotations

import html as _html
import logging
import re

import markdown as _markdown
import nh3
from bs4 import BeautifulSoup
from bs4.element import NavigableString, PageElement, Tag

from app.services.markdown_guards import add_missing_table_delimiters, escape_glued_hash

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Baseline inline styles (Outlook ignores <style> blocks — must be inline)
# ---------------------------------------------------------------------------

#: ``<table>`` — collapse borders, full width; the client's font and colours.
EMAIL_TABLE_STYLE = "border-collapse:collapse;width:100%"
#: ``<td>`` (and the base for ``<th>``) — visible cell borders + padding.
EMAIL_CELL_STYLE = "border:1px solid #d0d0d0;padding:6px 10px;text-align:left"
#: ``<th>`` — cell style plus a bold header, no shade: a light shade under the
#: light text of a dark theme is unreadable.
EMAIL_HEADER_STYLE = f"{EMAIL_CELL_STYLE};font-weight:600"
#: ``<a>`` — underlined, in the client's link colour.
EMAIL_LINK_STYLE = "text-decoration:underline"
#: Block spacing: one blank line under each paragraph, list, heading,
#: quote, code block and table. Inline, because Graph ``createReply`` puts
#: Outlook's ``p {margin-top:0; margin-bottom:0}`` in the message head, which
#: glued every paragraph of a reply to the next one; an inline style wins over
#: it, so a reply and a direct send look the same.
EMAIL_BLOCK_SPACING_STYLE = "margin-top:0;margin-bottom:12pt"
#: The blocks that get :data:`EMAIL_BLOCK_SPACING_STYLE` when they carry no margin of their own.
_SPACED_BLOCK_TAGS = ("p", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "table")
#: A block inside one of these keeps its spacing to its container (a loose list item, a cell).
_UNSPACED_CONTAINERS = ("li", "td", "th")
_MARGIN_RE = re.compile(r"(?:^|;)\s*margin(?:-(?:top|right|bottom|left))?\s*:", re.IGNORECASE)
#: The style Outlook writes on the text a person composes: what dark mode inverts like the quoted thread.
DEFAULT_COMPOSE_STYLE = "font-family:Aptos,Calibri,Arial,sans-serif;font-size:11pt;color:rgb(0,0,0)"
#: The class Outlook puts on each paragraph a person composes.
COMPOSE_CLASS = "elementToProof"
#: A ``compose_style`` setting that keeps the generated markup theme-neutral.
NO_COMPOSE_STYLE = "none"
#: The blocks whose inline content is wrapped in the compose style span.
_COMPOSED_BLOCK_TAGS = ("p", "li", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "div", "blockquote")
#: Inline content: wrapped together with the text around it.
_INLINE_TAGS = frozenset({"a", "b", "strong", "i", "em", "u", "code", "span", "br"})
#: Inside these the compose style sets no size (a heading keeps its own).
_KEEP_SIZE_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
#: Inside these the compose style sets no font (code stays monospace).
_KEEP_FONT_TAGS = ("pre", "code")

# ---------------------------------------------------------------------------
# Sanitizer allow-list
# ---------------------------------------------------------------------------

#: Tags kept after sanitization. Mirrors the agreed email allow-list plus the
#: structural tags the Markdown renderer legitimately emits (``div``,
#: ``blockquote``). Everything else is stripped.
_ALLOWED_TAGS: set[str] = {
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "a",
    "b",
    "strong",
    "i",
    "em",
    "u",
    "ul",
    "ol",
    "li",
    "p",
    "div",
    "blockquote",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "code",
    "pre",
    "br",
    "hr",
    "span",
}

#: ``<script>`` / ``<style>`` are removed *with their content* (otherwise the
#: raw JS / CSS text would leak into the visible email body).
_CLEAN_CONTENT_TAGS: set[str] = {"script", "style", "iframe", "object", "embed"}

#: CSS properties allowed to survive on author-supplied ``style`` attributes.
#: Every other property (``position``, ``behavior``, ``float``...) is dropped
#: by :func:`_filter_style`. The author's colours and fonts are kept: the
#: platform adds none of its own.
_ALLOWED_STYLE_PROPS: frozenset[str] = frozenset(
    {
        "background",
        "background-color",
        "border",
        "border-bottom",
        "border-collapse",
        "border-color",
        "border-left",
        "border-radius",
        "border-right",
        "border-spacing",
        "border-style",
        "border-top",
        "border-width",
        "color",
        "display",
        "font-family",
        "font-size",
        "font-style",
        "font-weight",
        "height",
        "line-height",
        "margin",
        "margin-bottom",
        "margin-left",
        "margin-right",
        "margin-top",
        "max-width",
        "padding",
        "padding-bottom",
        "padding-left",
        "padding-right",
        "padding-top",
        "text-align",
        "text-decoration",
        "vertical-align",
        "white-space",
        "width",
    }
)
#: A declaration value carrying one of these is dropped whatever its property:
#: a resource load, a script, an IE expression or binding, a CSS escape or a
#: comment that could hide one.
_UNSAFE_STYLE_VALUE_RE = re.compile(
    r"url\s*\(|expression\s*\(|javascript\s*:|vbscript\s*:|data\s*:|behavior|-moz-binding|@import|\\|/\*|[<>]",
    re.IGNORECASE,
)

#: Per-tag attribute allow-list. ``style`` is allowed on layout tags so
#: hand-authored HTML keeps its styling (filtered by ``_ALLOWED_STYLE_PROPS``);
#: links keep ``href`` / ``title`` exactly (so Defender / Drata / Grafana /
#: GitHub URLs are never mangled).
_ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    # ``rel`` is managed by nh3's ``link_rel`` (do not list it here).
    "a": {"href", "title", "target"},
    "table": {"style", "border", "cellpadding", "cellspacing", "width", "align"},
    "thead": {"style"},
    "tbody": {"style"},
    "tr": {"style"},
    "th": {"style", "align", "valign", "colspan", "rowspan", "width", "scope"},
    "td": {"style", "align", "valign", "colspan", "rowspan", "width"},
    "div": {"style"},
    # ``class`` is kept for Outlook's ``elementToProof`` only (:func:`_apply_inline_styles` drops any other).
    "p": {"style", "class"},
    "h1": {"style"},
    "h2": {"style"},
    "h3": {"style"},
    "h4": {"style"},
    "h5": {"style"},
    "h6": {"style"},
    "span": {"style"},
    "blockquote": {"style"},
    "ul": {"style"},
    "ol": {"style", "start", "type"},
    "li": {"style"},
    "code": {"style"},
    "pre": {"style"},
}

#: URL schemes permitted on ``href``. Absolute links (https / mailto / tel)
#: are preserved verbatim; everything else (``javascript:``, ``data:``) drops.
_ALLOWED_URL_SCHEMES: set[str] = {"http", "https", "mailto", "tel"}

#: Block-level tags whose presence means the body is already HTML. Inline-only
#: markup (``<b>``, ``<a>``, ``<br>``) does NOT trigger HTML treatment, so
#: Markdown that merely embeds the odd inline tag is still converted.
_BLOCK_HTML_RE = re.compile(
    r"<\s*(?:table|thead|tbody|tr|th|td|p|div|h[1-6]|ul|ol|li|blockquote|pre)\b",
    re.IGNORECASE,
)

_CLOSING_TAG_RE = re.compile(r"</\s*[a-z][a-z0-9]*\s*>", re.IGNORECASE)

_MARKDOWN_EXTENSIONS = ["tables", "fenced_code", "sane_lists", "nl2br"]


def looks_like_html_email(body: str) -> bool:
    """Return ``True`` when *body* already contains block-level HTML.

    Used to decide whether to skip Markdown conversion (idempotency: an HTML
    body must never be double-converted).
    """
    return bool(_BLOCK_HTML_RE.search(body))


def starts_as_html_markup(body: str) -> bool:
    """Return ``True`` when *body* opens with a tag and closes one: HTML written to be rendered.

    Text that only mentions a tag (``use the <table> element``, ``<team>``) does not count.
    """
    return body.lstrip().startswith("<") and bool(_CLOSING_TAG_RE.search(body))


#: A Markdown list item: ``- ``, ``* ``, ``+ `` or ``1. `` / ``1) `` at the start of a line.
_LIST_ITEM_RE = re.compile(r"^\s{0,3}(?:[-*+]|\d{1,9}[.)])\s+\S")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


def _fenced_lines(lines: list[str]) -> list[bool]:
    """Flag each line inside (or delimiting) a fenced code block (CommonMark closing rule)."""
    mask: list[bool] = []
    fence = ""
    for line in lines:
        marker = _FENCE_RE.match(line)
        if fence:
            mask.append(True)
            if (
                marker
                and marker.group(1)[0] == fence[0]
                and len(marker.group(1)) >= len(fence)
                and not line[marker.end() :].strip()
            ):
                fence = ""
            continue
        mask.append(bool(marker))
        if marker:
            fence = marker.group(1)
    return mask


def _separate_lists(text: str) -> str:
    """*text* with a blank line around each list that touches a line of text.

    Python-Markdown opens a list only after a blank line, and keeps a line of
    text right under a list inside its last item. Agents write mail the way
    people do (``Slots:`` then ``- Monday`` on the next line, the sign-off right
    under the last item), which came out as literal dashes. A line-start ``#``
    glued to its text (``#9195 is ...``), which Python-Markdown renders as a
    heading, is escaped (:func:`escape_glued_hash`), and a pipe table written
    without its separator row gets one (:func:`add_missing_table_delimiters`).
    Fenced code is left as it is.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines, _code = add_missing_table_delimiters(lines, _fenced_lines(lines))
    out: list[str] = []
    # The fence that opened the current code block: it closes only on the same
    # character, at least as long (CommonMark), so a ``~~~`` inside a ``` block is code.
    fence = ""
    previous = ""
    for line in lines:
        marker = _FENCE_RE.match(line)
        if fence:
            if (
                marker
                and marker.group(1)[0] == fence[0]
                and len(marker.group(1)) >= len(fence)
                and not line[marker.end() :].strip()
            ):
                fence = ""
            out.append(line)
            previous = ""
            continue
        if marker:
            fence = marker.group(1)
        else:
            line = escape_glued_hash(line)
        if not marker and line.strip() and previous.strip():
            item, after_item = bool(_LIST_ITEM_RE.match(line)), bool(_LIST_ITEM_RE.match(previous))
            continues_item = line[:1].isspace()
            if (item and not after_item and not previous[:1].isspace()) or (
                after_item and not item and not continues_item
            ):
                out.append("")
        out.append(line)
        previous = line
    return "\n".join(out)


def _markdown_to_html(text: str) -> str:
    """Render Markdown to an HTML fragment with GFM tables enabled, its lists opened like a person writes them."""
    return _markdown.markdown(
        _separate_lists(text),
        extensions=_MARKDOWN_EXTENSIONS,
        output_format="html",
    )


def _sanitize(html: str) -> str:
    """Strip scripts / event handlers / unsafe URLs against the allow-list.

    ``style`` values are filtered afterwards by :func:`_filter_style`, which
    also checks each value, where nh3 filters by property name only and
    re-serializes every style it touches.
    """
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        clean_content_tags=_CLEAN_CONTENT_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_ALLOWED_URL_SCHEMES,
        link_rel="noopener noreferrer",
    )


def _filter_style(style: str) -> str:
    """*style* with only its allowed, safe declarations; kept byte for byte when every one is.

    A declaration survives when its property is in :data:`_ALLOWED_STYLE_PROPS`
    and its value matches nothing of :data:`_UNSAFE_STYLE_VALUE_RE`. A value
    split by a ``;`` inside parentheses (``url(data:a;b)``) leaves fragments
    that fail one of the two tests, so it is dropped whole.
    """
    kept: list[str] = []
    dropped = False
    for declaration in style.split(";"):
        if not declaration.strip():
            continue
        prop, colon, value = declaration.partition(":")
        if (
            colon
            and prop.strip().lower() in _ALLOWED_STYLE_PROPS
            and value.strip()
            and not _UNSAFE_STYLE_VALUE_RE.search(value)
        ):
            kept.append(declaration.strip())
        else:
            dropped = True
    return ";".join(kept) if dropped else style.strip()


def _declarations(style: str) -> dict[str, str]:
    """*style*'s declarations by lower-case property, the last one of a property winning."""
    out: dict[str, str] = {}
    for declaration in style.split(";"):
        prop, colon, value = declaration.partition(":")
        if colon and prop.strip() and value.strip():
            out[prop.strip().lower()] = value.strip()
    return out


def compose_declarations(compose_style: str | None) -> dict[str, str]:
    """The declarations a ``compose_style`` setting adds: the default when unset, none for ``none``.

    The value is filtered as an author's style is (:func:`_filter_style`), so a
    setting can never carry what a body could not.
    """
    if compose_style is None or not compose_style.strip():
        compose_style = DEFAULT_COMPOSE_STYLE
    if compose_style.strip().lower() == NO_COMPOSE_STYLE:
        return {}
    return _declarations(_filter_style(compose_style))


def _styled_props(element: Tag) -> set[str]:
    """The properties *element* and its ancestors set in their own ``style``."""
    props: set[str] = set()
    for node in (element, *element.parents):
        if isinstance(node, Tag) and node.get("style"):
            props.update(_declarations(str(node["style"])))
    return props


def _inline_runs(block: Tag) -> list[list[PageElement]]:
    """The runs of *block*'s direct children that are text or inline markup, each holding some text."""
    runs: list[list[PageElement]] = []
    current: list[PageElement] = []
    for child in list(block.children):
        if isinstance(child, NavigableString) and type(child) is not NavigableString:
            continue  # a comment, a CDATA section: left where it is
        if isinstance(child, NavigableString) or (isinstance(child, Tag) and child.name in _INLINE_TAGS):
            current.append(child)
            continue
        if current:
            runs.append(current)
        current = []
    if current:
        runs.append(current)
    return [run for run in runs if any(str(node.get_text() if isinstance(node, Tag) else node).strip() for node in run)]


def _already_composed(run: list[PageElement], wanted: dict[str, str]) -> bool:
    """Whether *run* is one span (blank text around it aside) that already sets every property of *wanted*."""
    tags = [node for node in run if isinstance(node, Tag)]
    if len(tags) != 1 or tags[0].name != "span" or any(str(node).strip() for node in run if not isinstance(node, Tag)):
        return False
    return set(wanted) <= set(_declarations(str(tags[0].get("style") or "")))


def _apply_compose_style(soup: BeautifulSoup, declarations: dict[str, str]) -> None:
    """Wrap the text of every text block of *soup* in a span carrying *declarations*, as Outlook composes.

    Only the properties the block and its ancestors leave unset are added: an
    author's colour, font or size is kept. A heading keeps its size, code its
    font. A paragraph whose text is wrapped gets Outlook's ``elementToProof``
    class. Running it twice changes nothing.
    """
    if not declarations:
        return
    blocks: list[Tag] = [soup, *(tag for tag in soup.find_all(_COMPOSED_BLOCK_TAGS) if isinstance(tag, Tag))]
    for block in blocks:
        runs = _inline_runs(block)
        if not runs:
            continue
        unset = _styled_props(block) if isinstance(block, Tag) and block is not soup else set()
        if block.name in _KEEP_SIZE_TAGS or block.find_parent(_KEEP_SIZE_TAGS) is not None:
            unset.add("font-size")
        if block.name in _KEEP_FONT_TAGS or block.find_parent(_KEEP_FONT_TAGS) is not None:
            unset.add("font-family")
        wanted = {prop: value for prop, value in declarations.items() if prop not in unset}
        if not wanted:
            continue
        style = ";".join(f"{prop}:{value}" for prop, value in wanted.items())
        wrapped = False
        for run in runs:
            if _already_composed(run, wanted):
                wrapped = True
                continue
            span = soup.new_tag("span", attrs={"style": style})
            run[0].insert_before(span)
            for node in run:
                span.append(node.extract())
            wrapped = True
        if wrapped and block is not soup and block.name == "p":
            rest = {key: value for key, value in block.attrs.items() if key != "class"}
            block.attrs = {"class": COMPOSE_CLASS, **rest}


def _apply_inline_styles(html: str, compose: dict[str, str] | None = None) -> str:
    """Inject baseline inline styles onto generated table / th / td / a, then the compose style.

    Only elements that do not already carry a ``style`` attribute are touched,
    so hand-authored HTML keeps its own styling while Markdown-generated
    tables (which have none) get the Outlook-friendly baseline. *compose*
    (:func:`compose_declarations`) is applied last, by
    :func:`_apply_compose_style`; ``None`` or empty adds none.
    """
    soup = BeautifulSoup(html, "html.parser")
    for paragraph in soup.find_all("p", class_=True):
        # Only Outlook's own class survives: a class means nothing to a mail reader without its sheet.
        if COMPOSE_CLASS in (paragraph.get("class") or []):
            paragraph["class"] = COMPOSE_CLASS
        else:
            del paragraph["class"]
    for styled in soup.find_all(style=True):
        style = _filter_style(str(styled["style"]))
        if style:
            styled["style"] = style
        else:
            del styled["style"]
    for table in soup.find_all("table"):
        if not table.get("style"):
            table["style"] = EMAIL_TABLE_STYLE
    for th in soup.find_all("th"):
        if not th.get("style"):
            th["style"] = EMAIL_HEADER_STYLE
    for td in soup.find_all("td"):
        if not td.get("style"):
            td["style"] = EMAIL_CELL_STYLE
    for anchor in soup.find_all("a"):
        if not anchor.get("style"):
            anchor["style"] = EMAIL_LINK_STYLE
    for block in soup.find_all(_SPACED_BLOCK_TAGS):
        if block.find_parent(_UNSPACED_CONTAINERS) is not None:
            continue
        style = str(block.get("style") or "").strip()
        if _MARGIN_RE.search(style):
            continue
        block["style"] = f"{style.rstrip(';')};{EMAIL_BLOCK_SPACING_STYLE}" if style else EMAIL_BLOCK_SPACING_STYLE
    _apply_compose_style(soup, compose or {})
    return str(soup)


_MARGIN_BOTTOM_DECL_RE = re.compile(r"\s*margin-bottom\s*:[^;]*;?", re.IGNORECASE)
_STYLE_ATTR_RE = re.compile(r"\bstyle\s*=\s*(\"[^\"]*\"|'[^']*')", re.IGNORECASE)


def _without_bottom_margin(open_tag: str) -> str:
    """An opening tag with ``margin-bottom:0`` in its style, every other declaration kept."""
    match = _STYLE_ATTR_RE.search(open_tag)
    if match is None:
        return f'{open_tag[:-1].rstrip("/").rstrip()} style="margin-bottom:0">'
    quote = match.group(1)[0]
    declarations = _MARGIN_BOTTOM_DECL_RE.sub("", match.group(1)[1:-1]).strip().rstrip(";")
    style = f"{declarations};margin-bottom:0" if declarations else "margin-bottom:0"
    return f"{open_tag[: match.start()]}style={quote}{style}{quote}{open_tag[match.end() :]}"


def close_before_signature(html: str) -> str:
    """*html*, the part of an email a signature follows, with no bottom margin under its last block.

    The sign-off paragraph (and every list, quote or table it ends) gets
    ``margin-bottom:0``, so the signature follows it directly, as Outlook
    places one; a signature service's block brings its own top spacing, and
    the 12pt of :data:`EMAIL_BLOCK_SPACING_STYLE` stacked on it showed as a
    blank line. Every other block keeps its spacing. Only the opening tags of
    those last blocks change: the rest of *html* is kept byte for byte (a
    draft's head, a quoted thread). A block followed by text keeps its margin.
    """
    if not html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    last: list[Tag] = []
    for block in soup.find_all(_SPACED_BLOCK_TAGS):
        if block.find_parent(_UNSPACED_CONTAINERS) is not None or block.sourceline is None:
            continue
        # The text after the block's end: find_all_next also walks its own content.
        after = (
            s
            for s in block.find_all_next(string=True)
            if type(s) is NavigableString and not any(parent is block for parent in s.parents)
        )
        if not any(str(s).strip() for s in after):
            last.append(block)
    if not last:
        return html
    line_starts = [0]
    for index, char in enumerate(html):
        if char == "\n":
            line_starts.append(index + 1)
    out = html
    for block in sorted(last, key=lambda b: (b.sourceline, b.sourcepos), reverse=True):
        start = line_starts[block.sourceline - 1] + (block.sourcepos or 0)
        tag = re.compile(rf"<{block.name}\b[^>]*>", re.IGNORECASE).match(out, start)
        if tag is None:
            continue
        out = out[:start] + _without_bottom_margin(tag.group(0)) + out[tag.end() :]
    return out


def _plaintext_fallback(body: str) -> str:
    """Escape *body* into a valid HTML paragraph (used on conversion failure)."""
    escaped = _html.escape(body).replace("\r\n", "\n").replace("\r", "\n")
    return f'<p style="{EMAIL_BLOCK_SPACING_STYLE}">' + escaped.replace("\n", "<br>") + "</p>"


def markdown_to_email_html(body: str, *, assume_html: bool | None = None, compose_style: str | None = None) -> str:
    """Normalize an agent-authored email *body* into safe, styled HTML.

    Always returns a sanitized HTML fragment suitable for an email body whose
    ``contentType`` is ``HTML``:

    * Markdown (the common case) is converted to HTML with GFM tables, lists,
      emphasis, headings, inline + fenced code and ``[label](url)`` links.
    * A body that already looks like HTML is passed through unchanged aside
      from sanitization (never double-converted).
    * Plain text with no Markdown still produces valid HTML (paragraphs with
      ``<br>`` line breaks).

    Pass ``assume_html=True`` to force HTML pass-through (e.g. when the caller
    explicitly declared ``content_type="html"``); ``None`` auto-detects.
    ``compose_style`` is the integration's setting (:func:`compose_declarations`:
    ``None`` is :data:`DEFAULT_COMPOSE_STYLE`, ``"none"`` adds no style).

    The conversion is fail-open: if rendering or sanitization raises, the body
    is escaped into a plain ``<p>`` block so a malformed report can never block
    an outgoing email nor leak unsanitized markup.
    """
    if not body or not body.strip():
        return ""

    try:
        treat_as_html = assume_html if assume_html is not None else looks_like_html_email(body)
        html = body if treat_as_html else _markdown_to_html(body)
        html = _sanitize(html)
        return _apply_inline_styles(html, compose_declarations(compose_style))
    except Exception:
        # Fail-open: a malformed report must never block an outgoing email
        # nor leak unsanitized markup — fall back to escaped plain text.
        logger.warning("Email HTML normalization failed; falling back to escaped text", exc_info=True)
        return _plaintext_fallback(body)


def build_graph_email_body(
    body: str, content_type: str | None = None, *, compose_style: str | None = None
) -> dict[str, str]:
    """Build a Microsoft Graph message ``body`` dict — **always HTML**.

    ``contentType`` is forced to ``HTML`` and ``content`` is the normalized
    HTML from :func:`markdown_to_email_html`, so Markdown is never delivered
    raw with ``contentType: Text``. ``content_type`` is treated as a hint:
    ``"html"`` forces pass-through, anything else (including the legacy
    ``"text"`` default) auto-detects Markdown vs. HTML. ``compose_style`` as
    in :func:`markdown_to_email_html`.
    """
    assume_html = True if (content_type or "").strip().lower() == "html" else None
    return {
        "contentType": "HTML",
        "content": markdown_to_email_html(body, assume_html=assume_html, compose_style=compose_style),
    }
