# SPDX-License-Identifier: Apache-2.0

"""Microsoft Teams message normalization - Markdown -> Teams-safe HTML / text.

Agents author report bodies in Markdown (the correct format for web chat). The
same Markdown was previously handed verbatim to every Teams send boundary:

* Microsoft Graph ``chatMessage`` sends defaulted to ``contentType: "text"``,
  which renders nothing at all - a "Daily Compliance Posture Digest" arrived as
  a wall of literal ``**``, ``##``, ``|`` and ``---`` characters.
* Bot Framework ``text`` sends rely on Bot Framework's default Markdown
  rendering, which covers bold / italic / lists / links but NOT headings or
  tables - so headings showed raw ``##`` and tables showed raw pipes.

This module is the single, platform-side normalization point for Teams - the
Teams counterpart of ``services/email_html.py`` and the ``markdown_to_slack``
helper. It mirrors the email precedent: no per-agent prompt
changes are required, it works for all current and future agents, and the
conversion is deterministic and fail-open.

Two distinct Teams surfaces, two normalizers:

* **Graph ``chatMessage`` HTML** (``markdown_to_teams_html`` /
  ``build_graph_chat_body``). Teams renders a rich HTML subset here - bold,
  italic, headings, lists, links, blockquote, code/preformatted **and tables**
  (an empirically verified channel/chat capability). So Markdown is rendered to
  real HTML, GFM tables included, and an agent that authors HTML itself is
  passed through untouched aside from security sanitization - we never strip
  the tables / structure it relies on. Crucially the Markdown-vs-HTML decision
  is made from the **body content**, not the caller's ``content_type``: an
  agent that authors Markdown but labels it ``content_type="html"`` is still
  converted, never shipped raw (which previously dumped literal ``##`` / ``**``
  / ``|`` characters into Teams). The same path also unescapes
  **entity-encoded HTML** (``&lt;p&gt;...&lt;/p&gt;``) before detection:
  agents stuffing HTML into a JSON tool argument sometimes escape the tags,
  and shipping those entities as ``contentType: html`` makes Teams render
  the tags as visible characters (the intermittent ~5% Graph-send failure).
* **Bot Framework ``text``** (``markdown_to_teams_text``). Bot Framework renders
  bold / italic / lists / links from Markdown but NOT headings or tables. So on
  this surface only, headings downgrade to bold and GFM tables become readable
  record lines (``- **Header:** value``), leaving the rest of the Markdown for
  Bot Framework to render natively.

Bare ``https://...`` URLs (and the same URL wrapped in backticks, which
Python-Markdown would otherwise emit as ``<code>`` and Teams would render as
plain monospace text) are turned into real links on both surfaces. Fenced
code blocks, existing ``[label](url)`` links, and live ``<a>`` / ``<code>`` /
``<pre>`` HTML are left alone.

Sanitization (HTML path) uses an ``nh3`` allow-list that mirrors the Teams
renderable subset: scripts, event handlers and unsafe URL schemes are stripped,
but every tag Teams actually renders (tables included) survives - so an
agent-authored HTML message is preserved exactly, just made safe.

Spacing / breadth
-----------------
Teams renders agent reports far more compactly than web chat - headings,
paragraphs, lists and tables sit flush against each other with almost no
vertical gap. Teams *strips* CSS ``margin`` / ``padding`` from chatMessage HTML
by design (Microsoft engineering confirmed this is intentional - see the Graph
``chatMessage`` HTML restrictions), so inline spacing styles are silently
dropped. The only spacing primitive Teams reliably honors is structural markup,
so this module gives reports room to breathe deterministically, with no
per-agent prompt changes:

* **HTML surface**: a ``<br/>`` is inserted between consecutive top-level block
  elements of the Markdown-generated HTML (``_apply_block_spacing``). List items
  and table rows are left tight - only sibling *blocks* (heading, paragraph,
  list, table, blockquote, code, rule) are separated.
* **Bot Framework text surface**: section headings (downgraded to bold) are
  surrounded by a blank line so each section is visually separated.

Both are applied only to content we generate from Markdown; an agent that
authors its own HTML keeps full control of its layout (pass-through is only
sanitized, never re-spaced).

Mentions
--------
A Graph ``chatMessage`` mention is an ``<at id="N">name</at>`` tag in the body
plus the matching entry of the message's ``mentions`` array. The
sanitizer drops ``<at>``, so :func:`build_graph_chat_body` takes the mention
names (``mention_texts``) and writes the tags AFTER the conversion: each place
an agent marked with ``<at id="N">...</at>`` is swapped for a random
alphanumeric token that Markdown, the autolinker and the sanitizer all leave
alone, and the token becomes the tag once the HTML is final. A mention the body
does not place opens the message on a line of its own. Without
``mention_texts`` nothing changes: an ``<at>`` tag is stripped to its text.
"""

from __future__ import annotations

import html as _html
import logging
import re
import secrets
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

import markdown as _markdown
import nh3
from bs4 import BeautifulSoup, Comment, NavigableString

from app.services.markdown_guards import add_missing_table_delimiters, escape_glued_hash

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Baseline inline styles for Markdown-generated tables
# ---------------------------------------------------------------------------
# Teams (like Outlook) ignores ``<style>`` blocks, so table styling has to live
# in ``style=`` attributes. These are applied ONLY to Markdown-generated tables
# that carry no style of their own - an agent-authored HTML table keeps its own
# styling untouched.

#: ``<table>`` - collapse borders, full width, readable sans-serif font.
TEAMS_TABLE_STYLE = (
    "border-collapse:collapse;width:100%;"
    "font-family:'Segoe UI',Arial,sans-serif;font-size:14px"
)
#: ``<td>`` (and the base for ``<th>``) - visible cell borders + padding.
TEAMS_CELL_STYLE = "border:1px solid #d0d0d0;padding:6px 10px;text-align:left"
#: ``<th>`` - cell style plus a shaded, bold header.
TEAMS_HEADER_STYLE = f"{TEAMS_CELL_STYLE};background:#f3f3f3;font-weight:600"

# ---------------------------------------------------------------------------
# Sanitizer allow-list (Teams-renderable HTML subset)
# ---------------------------------------------------------------------------

#: Tags kept after sanitization. Mirrors the subset of HTML that Microsoft
#: Teams renders for a ``contentType: "html"`` chatMessage - tables INCLUDED
#: (verified: Teams renders HTML tables in channel and chat messages). An
#: agent that authors its own HTML therefore keeps the structure it relies on;
#: only genuinely dangerous markup is stripped.
_ALLOWED_TAGS: set[str] = {
    "table", "thead", "tbody", "tr", "th", "td",
    "a", "b", "strong", "i", "em", "u", "s", "strike", "del",
    "p", "div", "span", "hr",
    "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "code", "br",
}

#: ``<script>`` / ``<style>`` are removed *with their content* (otherwise the
#: raw JS / CSS text would leak into the visible message body).
_CLEAN_CONTENT_TAGS: set[str] = {"script", "style", "iframe", "object", "embed"}

#: Per-tag attribute allow-list. ``style`` is allowed on layout tags so an
#: agent's hand-authored HTML keeps its styling (filtered by
#: ``_ALLOWED_STYLE_PROPS``); links keep ``href`` / ``title`` exactly so deep
#: links (including query strings) are never mangled.
_ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    # ``rel`` is managed by nh3's ``link_rel`` (do not list it here).
    "a": {"href", "title"},
    "table": {"style", "border", "cellpadding", "cellspacing", "width", "align"},
    "thead": {"style"},
    "tbody": {"style"},
    "tr": {"style"},
    "th": {"style", "align", "valign", "colspan", "rowspan", "width", "scope"},
    "td": {"style", "align", "valign", "colspan", "rowspan", "width"},
    "div": {"style"},
    "p": {"style"},
    "span": {"style"},
    "blockquote": {"style"},
    "ul": {"style"},
    "ol": {"style", "start", "type"},
    "li": {"style"},
    "code": {"style"},
    "pre": {"style"},
}

#: CSS properties allowed to survive on author-supplied ``style`` attributes.
#: nh3 drops every other property (e.g. ``position``, ``url(javascript:...)``).
_ALLOWED_STYLE_PROPS: set[str] = {
    "border", "border-collapse", "border-color", "border-spacing", "border-style",
    "border-width", "padding", "margin", "text-align", "vertical-align",
    "background", "background-color", "color", "font-weight", "font-style",
    "font-family", "font-size", "width", "white-space",
}

#: URL schemes permitted on ``href``. Everything else (``javascript:``,
#: ``data:``) is dropped by nh3.
_ALLOWED_URL_SCHEMES: set[str] = {"http", "https", "mailto", "tel"}

#: Block-level tags whose presence (OUTSIDE a fenced code block - see
#: ``looks_like_html_teams``) means the body is already HTML (so Markdown
#: conversion is skipped - idempotency). Inline-only markup (``<b>``, ``<a>``,
#: ``<br>``) does NOT trigger HTML treatment.
_BLOCK_HTML_RE = re.compile(
    r"<\s*(?:table|thead|tbody|tr|th|td|p|div|h[1-6]|ul|ol|li|blockquote|pre)\b",
    re.IGNORECASE,
)

#: Entity-encoded block tag used as a real wrapper
#: (``&lt;p&gt;Hello&lt;/p&gt;``), not a tag *name* mentioned in prose
#: (``&lt;p&gt;`` alone). The 5% Graph-send failure: the agent authors HTML
#: then HTML-escapes the tags (common when stuffing markup into a JSON tool
#: argument). Live-tag detection misses it, Markdown wraps the entities in a
#: real ``<p>``, and Teams renders the escaped tags as visible ``<p>`` /
#: ``<b>`` / ``<ul>`` characters.
_ENCODED_WRAPPED_HTML_RE = re.compile(
    r"&lt;\s*(p|div|ul|ol|table|h[1-6]|blockquote|pre|li|thead|tbody|tr|th|td)\b"
    r"(?:[^&]|&(?!lt;))*&gt;"
    r".*?"
    r"&lt;\s*/\s*\1\s*&gt;",
    re.IGNORECASE | re.DOTALL,
)

#: Markdown extensions. ``tables`` is enabled so GFM tables render as real HTML
#: tables (Teams renders them).
_MARKDOWN_EXTENSIONS = ["tables", "fenced_code", "sane_lists", "nl2br"]

#: Top-level block elements separated by a ``<br/>`` for vertical breathing room
#: (Teams strips CSS margins, so spacing has to be structural). Nested elements
#: (``<li>`` / ``<tr>``) are intentionally absent - only sibling blocks are
#: spaced, never the rows inside a list or table.
_SPACED_BLOCKS: set[str] = {
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "ul", "ol", "table", "blockquote", "pre", "hr",
}
#: Blocks Teams already opens with a gap of their own (a list's top margin
#: survives the chatMessage sanitizer): a ``<br/>`` before them doubles it.
_SELF_SPACED_BLOCKS: frozenset[str] = frozenset({"ul", "ol"})

# ---------------------------------------------------------------------------
# GFM table -> record lines (Bot Framework text surface only)
# ---------------------------------------------------------------------------

#: A delimiter cell of a GFM table header separator (``---``, ``:--``, ``:-:``).
_DELIM_CELL_RE = re.compile(r"^:?-{1,}:?$")
#: Placeholder protecting escaped pipes (``\|``) while a row is split on ``|``.
_PIPE_PLACEHOLDER = "\x00PIPE\x00"
#: ATX heading line (``# ...`` .. ``###### ...``).
_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+(.+?)[ \t]*$")
#: A fenced code-block delimiter (opening or closing): three or more backticks
#: or tildes, optionally indented up to three spaces. The text-surface table /
#: heading normalizers skip fenced lines so a snippet that legitimately
#: contains ``|``, ``---`` or ``#`` is preserved verbatim, not rewritten.
_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
#: An inline code span (`` `<tag>` ``, ``` ``<tag>`` ```): a backtick run, the
#: shortest content, then a closing run of the SAME length. Stripped from each
#: non-fenced line before block-tag detection so a tag named inside inline code
#: (e.g. ``Use `<table>` like this``) is treated as code, not as live HTML.
_INLINE_CODE_RE = re.compile(r"(`+).*?\1")
#: Markdown / image link. Protected during autolink so an existing
#: ``[label](url)`` is not rewritten around its destination URL.
_MD_LINK_RE = re.compile(r"!?\[(?:[^\]]*)\]\([^)]+\)")
#: Bare http(s) URL. Trailing punctuation is stripped after the match
#: (see :func:`_split_trailing_url_punct`).
_HTTP_URL_RE = re.compile(r"https?://[^\s<>`'\"\]]+", re.IGNORECASE)
#: Trailing characters that are almost never part of a URL (sentence
#: punctuation, unmatched closers). A balanced ``)`` inside the URL is kept
#: so Wikipedia-style ``https://en.wikipedia.org/wiki/Foo_(bar)`` stays intact.
_TRAILING_URL_PUNCT = ".,;:!?)]}>\"'"


def _code_fence_mask(lines: list[str]) -> list[bool]:
    """Flag every line that is inside (or delimits) a fenced code block.

    Returns a per-line boolean mask so :func:`_tables_to_records` and
    :func:`_downgrade_headings` leave fenced snippets untouched - a code block
    may legitimately contain pipe tables, ``---`` rows or ``#`` lines that must
    NOT be reinterpreted as Markdown. A fence opened with ``` ``` ``` / ``~~~``
    is closed only by a delimiter of the same character whose run length is at
    least as long (CommonMark semantics).
    """
    mask = [False] * len(lines)
    fence: str | None = None
    for idx, line in enumerate(lines):
        match = _FENCE_RE.match(line)
        if fence is None:
            if match:
                fence = match.group(1)
                mask[idx] = True
        else:
            mask[idx] = True
            if (
                match
                and match.group(1)[0] == fence[0]
                and len(match.group(1)) >= len(fence)
            ):
                fence = None
    return mask


def _looks_like_http_url(value: str) -> bool:
    """Return ``True`` when *value* is a well-formed http(s) URL with a host."""
    if not value or any(ch.isspace() for ch in value):
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _split_trailing_url_punct(raw: str) -> tuple[str, str]:
    """Split a URL match into ``(url, trailing_punctuation)``.

    A closing ``)`` is kept when it is balanced by an opening ``(`` in the
    URL (Wikipedia-style ``Foo_(bar)``); unmatched closers and sentence
    punctuation are peeled off so ``https://example.com.`` still autolinks.
    """
    trailing: list[str] = []
    url = raw
    while url and url[-1] in _TRAILING_URL_PUNCT:
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break
        trailing.append(url[-1])
        url = url[:-1]
    return url, "".join(reversed(trailing))


def _unwrap_code_urls_line(line: str) -> str:
    """Turn a backtick-wrapped URL into a Markdown link.

    Agents (and some models) wrap URLs in inline code so they "look like a
    link". Python-Markdown then emits ``<code>url</code>``, and Teams renders
    that as unclickable monospace text. When the *entire* code span is an
    http(s) URL, unwrap it to ``[url](url)`` so it becomes a real ``<a>``.
    """

    def _repl(match: re.Match[str]) -> str:
        ticks = match.group(1)
        inner = match.group(0)[len(ticks) : -len(ticks)]
        stripped = inner.strip()
        url, trailing = _split_trailing_url_punct(stripped)
        if inner != stripped or not _looks_like_http_url(url):
            return match.group(0)
        return f"[{url}]({url}){trailing}"

    return _INLINE_CODE_RE.sub(_repl, line)


def _autolink_bare_urls_line(line: str) -> str:
    """Wrap bare http(s) URLs on *line* as ``[url](url)``.

    Existing Markdown links / images and remaining inline-code spans are
    masked first so a destination URL inside ``[label](url)`` is not
    double-wrapped, and a URL mentioned inside non-URL code stays code.
    """
    protected: list[str] = []

    def _mask(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return f"\x00LNK{len(protected) - 1}\x00"

    masked = _MD_LINK_RE.sub(_mask, line)
    masked = _INLINE_CODE_RE.sub(_mask, masked)

    def _linkify(match: re.Match[str]) -> str:
        url, trailing = _split_trailing_url_punct(match.group(0))
        if not _looks_like_http_url(url):
            return match.group(0)
        return f"[{url}]({url}){trailing}"

    result = _HTTP_URL_RE.sub(_linkify, masked)
    for idx, original in enumerate(protected):
        result = result.replace(f"\x00LNK{idx}\x00", original)
    return result


def _linkify_markdown(text: str) -> str:
    """Autolink bare / backtick-wrapped URLs outside fenced code blocks."""
    lines = text.split("\n")
    in_code = _code_fence_mask(lines)
    return "\n".join(
        line if in_code[idx] else _autolink_bare_urls_line(_unwrap_code_urls_line(line))
        for idx, line in enumerate(lines)
    )


def _replace_node_with_parts(node: NavigableString, parts: list[object]) -> None:
    """Replace *node* with a sequence of strings and tags."""
    anchor: object = node
    for i, piece in enumerate(parts):
        el: object = piece if not isinstance(piece, str) else NavigableString(piece)
        if i == 0:
            node.replace_with(el)
            anchor = el
        else:
            anchor.insert_after(el)  # type: ignore[union-attr]
            anchor = el


def _linkify_text_to_nodes(text: str, soup: BeautifulSoup) -> list[object] | None:
    """Split *text* into NavigableStrings and ``<a>`` tags, or ``None`` if no URL."""
    parts: list[object] = []
    last = 0
    found = False
    for match in _HTTP_URL_RE.finditer(text):
        url, trailing = _split_trailing_url_punct(match.group(0))
        if not _looks_like_http_url(url):
            continue
        found = True
        if match.start() > last:
            parts.append(text[last : match.start()])
        a = soup.new_tag("a", href=url)
        a.string = url
        parts.append(a)
        if trailing:
            parts.append(trailing)
        last = match.end()
    if not found:
        return None
    if last < len(text):
        parts.append(text[last:])
    return parts


_SKIP_AUTOLINK_PARENTS = ("a", "code", "pre", "script", "style")


def _autolink_html(html: str) -> str:
    """Unwrap ``<code>``-only URLs and linkify remaining bare URLs in HTML.

    Used on the agent-authored HTML pass-through path (Markdown already went
    through :func:`_linkify_markdown`). Text inside ``<a>`` / ``<code>`` /
    ``<pre>`` is left alone after the code-unwrap pass.
    """
    soup = BeautifulSoup(html, "html.parser")
    for code in list(soup.find_all("code")):
        if code.find_parent(["a", "pre"]):
            continue
        raw = code.get_text()
        stripped = raw.strip()
        url, trailing = _split_trailing_url_punct(stripped)
        if not _looks_like_http_url(url) or raw != stripped:
            continue
        a = soup.new_tag("a", href=url)
        a.string = url
        if trailing:
            code.replace_with(a, NavigableString(trailing))
        else:
            code.replace_with(a)
    for node in list(soup.find_all(string=True)):
        if not isinstance(node, NavigableString):
            continue
        parent = node.parent
        if parent is None:
            continue
        name = (parent.name or "").lower()
        if name in _SKIP_AUTOLINK_PARENTS or parent.find_parent(_SKIP_AUTOLINK_PARENTS):
            continue
        parts = _linkify_text_to_nodes(str(node), soup)
        if parts is None:
            continue
        _replace_node_with_parts(node, parts)
    return str(soup)


def _split_table_row(line: str) -> list[str]:
    """Split a GFM table row into trimmed cell strings (honouring ``\\|``)."""
    s = line.strip().replace(r"\|", _PIPE_PLACEHOLDER)
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip().replace(_PIPE_PLACEHOLDER, "|") for c in s.split("|")]


def _is_delimiter_row(line: str) -> bool:
    """Return ``True`` when *line* is a GFM header/body delimiter row."""
    if "|" not in line and "-" not in line:
        return False
    cells = _split_table_row(line)
    return bool(cells) and all(bool(c) and _DELIM_CELL_RE.match(c) for c in cells)


def _render_table_records(header_cells: list[str], rows: list[list[str]]) -> list[str]:
    """Render a parsed GFM table as Markdown record lines.

    Each data row becomes a single bullet that pairs every header label with
    its cell value (``- **Header:** value, **Header:** value``). Cell content
    is preserved as Markdown so inline emphasis and ``[label](url)`` links keep
    rendering natively on the Bot-Framework-text surface.
    """
    headers = [h.strip() for h in header_cells]
    if not rows:
        joined = ", ".join(h for h in headers if h)
        return [f"**{joined}**", ""] if joined else [""]

    out: list[str] = []
    for row in rows:
        pairs: list[str] = []
        for idx, raw in enumerate(row):
            val = raw.strip()
            if not val:
                continue
            label = headers[idx] if idx < len(headers) else ""
            pairs.append(f"**{label}:** {val}" if label else val)
        if pairs:
            out.append("- " + ", ".join(pairs))
    out.append("")
    return out


def _tables_to_records(text: str) -> str:
    """Replace every GFM table in *text* with Markdown record lines.

    The Bot Framework ``text`` surface renders neither Markdown nor HTML
    tables, so a table is rewritten as a bullet list of ``**Header:** value``
    records that reads cleanly there. Tables inside a fenced code block are
    left verbatim (a code snippet may legitimately contain pipe rows).
    """
    lines = text.split("\n")
    in_code = _code_fence_mask(lines)
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        header = lines[i]
        if (
            not in_code[i]
            and "|" in header
            and i + 1 < n
            and not in_code[i + 1]
            and _is_delimiter_row(lines[i + 1])
        ):
            header_cells = _split_table_row(header)
            j = i + 2
            rows: list[list[str]] = []
            while (
                j < n
                and not in_code[j]
                and lines[j].strip()
                and "|" in lines[j]
                and not _is_delimiter_row(lines[j])
            ):
                rows.append(_split_table_row(lines[j]))
                j += 1
            # Ensure a blank line separates the records from preceding content
            # so Markdown parses the bullet list correctly.
            if out and out[-1].strip():
                out.append("")
            out.extend(_render_table_records(header_cells, rows))
            i = j
            continue
        out.append(header)
        i += 1
    return "\n".join(out)


def _collapse_blank_runs(text: str) -> str:
    """Keep one blank line where several follow each other, outside fenced code.

    The author's blank line and the one a block rule adds on top of it
    (a table turned into records, a heading downgraded) would show as a double
    gap in Teams.
    """
    lines = text.split("\n")
    in_code = _code_fence_mask(lines)
    out: list[str] = []
    for idx, line in enumerate(lines):
        if not in_code[idx] and not line.strip() and out and not out[-1].strip() and not in_code[idx - 1]:
            continue
        out.append(line if in_code[idx] or line.strip() else "")
    return "\n".join(out)


def _downgrade_headings(text: str) -> str:
    """Downgrade ATX headings (``## Title``) to bold (``**Title**``) line by line.

    Each downgraded heading is also surrounded by a blank line so sections are
    visually separated on the Bot Framework text surface (Teams renders this
    field as Markdown, where a blank line is the spacing primitive). Existing
    blank lines are reused, so a heading is never padded with a double gap.

    Lines inside a fenced code block are left untouched so a ``#`` comment line
    in a code snippet is not turned into a bold run (and gets no spacing).
    """
    lines = text.split("\n")
    in_code = _code_fence_mask(lines)
    n = len(lines)
    out: list[str] = []
    for idx, line in enumerate(lines):
        if in_code[idx]:
            out.append(line)
            continue
        match = _HEADING_RE.match(line)
        if not match:
            out.append(line)
            continue
        # Blank line before the heading - unless it opens the message or the
        # previous emitted line is already blank.
        if out and out[-1].strip():
            out.append("")
        out.append(f"**{match.group(1)}**")
        # Blank line after the heading - unless it closes the message or the
        # next source line is already blank.
        if idx + 1 < n and lines[idx + 1].strip():
            out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Block normalization (both surfaces)
# ---------------------------------------------------------------------------

#: A list item line: a bullet (``-``, ``*``, ``+``) or an ordered marker
#: (``1.`` / ``1)``) followed by a space and content. Indentation is checked by
#: the caller: up to three spaces opens a list, any indentation continues one.
_LIST_ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)(?:[-*+]|\d{1,9}[.)])[ \t]+\S")
#: A run of bullets written on one line after a colon (``Read live: - a - b``):
#: the lead ending with the colon, then the first item.
_INLINE_LIST_RE = re.compile(r"^(?P<lead>[^|]*\S:)[ \t]+-[ \t]+(?P<rest>\S.*)$")
#: The separator between two items of such a run.
_INLINE_LIST_SEP_RE = re.compile(r"[ \t]+-[ \t]+")
#: Non-breaking spaces written as entities. Markdown keeps them as entities and
#: the text surface shows them verbatim, so they become plain spaces.
_NBSP_ENTITY_RE = re.compile(r"&(?:nbsp|#160|#x0*a0);", re.IGNORECASE)
#: ``&amp;`` that does not start another entity (``&amp;lt;`` is escaped markup,
#: decoded by :func:`_decode_escaped_html` or kept as a code sample).
_AMP_ENTITY_RE = re.compile(r"&amp;(?!#?[0-9a-z]+;)", re.IGNORECASE)


def _is_list_item(line: str, *, in_list: bool) -> bool:
    match = _LIST_ITEM_RE.match(line)
    if not match:
        return False
    return in_list or len(match.group("indent").expandtabs(4)) <= 3


def _is_table_start(lines: list[str], idx: int, in_code: list[bool]) -> bool:
    return (
        "|" in lines[idx]
        and idx + 1 < len(lines)
        and not in_code[idx + 1]
        and _is_delimiter_row(lines[idx + 1])
    )


def _split_inline_list(line: str) -> list[str] | None:
    """``Lead: - a - b`` as ``["Lead:", "", "- a", "- b"]``; ``None`` when it is not such a run."""
    if _LIST_ITEM_RE.match(line):
        return None
    match = _INLINE_LIST_RE.match(line)
    if not match:
        return None
    items = [item.strip() for item in _INLINE_LIST_SEP_RE.split(match.group("rest")) if item.strip()]
    if len(items) < 2:
        return None
    return [match.group("lead"), "", *(f"- {item}" for item in items)]


#: A list item split into its indentation, marker and content.
_LIST_PARTS_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>[-*+]|\d{1,9}[.)])[ \t]+(?P<content>\S.*)$")


def _list_block_end(lines: list[str], code: list[bool], start: int) -> int:
    """Index past the list that opens at *start*: its items, their indented lines, inner blanks."""
    idx = start
    end = start
    while idx < len(lines) and not code[idx]:
        line = lines[idx]
        if not line.strip():
            idx += 1
            continue
        if _LIST_ITEM_RE.match(line) or line[:1] in (" ", "\t"):
            idx += 1
            end = idx
            continue
        break
    return end


#: The longest section label (markup and the trailing colon left out) that
#: becomes a bold line of its own. A longer parent is a sentence: written as a
#: bold line it is heavy to read, so it stays a bullet.
_SECTION_LABEL_MAX_CHARS = 40
#: Markup left out when a label is measured and when it is written in bold.
_LABEL_MARKUP_RE = re.compile(r"[*_`]")
#: The prefix of a child line folded under a parent that stays a bullet.
_CHILD_PREFIX = "\u21b3 "
#: Ends a list on the HTML surfaces: a raw HTML comment is a block of its own
#: for Markdown (the list before it closes, the items after it open a new one)
#: and the sanitizer drops it, so nothing reaches the reader.
_HTML_LIST_BREAK = "<!-- list break -->"


def _is_section_label(item: dict) -> bool:
    """Whether *item* reads as a short section label: it ends with ``:`` and is at most 40 characters."""
    text = _LABEL_MARKUP_RE.sub("", item["content"]).strip()
    return text.endswith(":") and len(text[:-1].strip()) <= _SECTION_LABEL_MAX_CHARS


def _list_tree(items: list[dict]) -> list[dict]:
    """The items of one list as a tree: each item carries its child items."""
    roots: list[dict] = []
    stack: list[dict] = []
    for item in items:
        while stack and stack[-1]["indent"] >= item["indent"]:
            stack.pop()
        (stack[-1]["children"] if stack else roots).append(item)
        stack.append(item)
    return roots


def _child_lines(children: list[dict]) -> list[str]:
    """Every descendant of a parent that stays a bullet, as continuation lines of that bullet."""
    out: list[str] = []
    for child in children:
        marker = "" if child["marker"] in ("-", "*", "+") else f"{child['marker']} "
        out.append(f"  {_CHILD_PREFIX}{marker}{child['content']}")
        out.extend(f"  {more}" for more in child["more"])
        out.extend(_child_lines(child["children"]))
    return out


def _switched_marker(marker: str) -> str:
    """*marker* with another bullet character or ordered delimiter: CommonMark opens a new list on it."""
    if marker in ("-", "*", "+"):
        return "-" if marker == "*" else "*"
    return marker[:-1] + (")" if marker.endswith(".") else ".")


def _trim_blank_tail(out: list[str]) -> None:
    while out and not out[-1]:
        out.pop()


def _render_flat_list(nodes: list[dict], out: list[str], list_break: str) -> None:
    """Write sibling *nodes* with no nesting: short labels as bold lines, every other item a bullet."""
    section_open = False
    # On the text surface the items after a section switch bullet character
    # (or ordered delimiter): CommonMark starts a new list on it, so the blank
    # line is not merged into a loose list.
    switch_bullet = False
    for idx, node in enumerate(nodes):
        has_next = idx + 1 < len(nodes)
        is_label = _is_section_label(node) and (bool(node["children"]) or (has_next and not node["more"]))
        if is_label:
            if out and out[-1]:
                out.append("")
            out.append(f"**{_LABEL_MARKUP_RE.sub('', node['content']).strip()}**")
            out.append("")
            if node["more"]:
                out.extend(node["more"])
                out.append("")
            _render_flat_list(node["children"], out, list_break)
            _trim_blank_tail(out)
            # Its children are its section: a sibling after them is not one of them.
            section_open = bool(node["children"])
            switch_bullet = False
            continue
        if section_open:
            _trim_blank_tail(out)
            out.extend(["", list_break, ""] if list_break else [""])
            section_open = False
            switch_bullet = not list_break
        marker = node["marker"]
        if switch_bullet:
            marker = _switched_marker(marker)
        out.append(f"{marker} {node['content']}")
        out.extend(f"  {more}" for more in node["more"])
        out.extend(_child_lines(node["children"]))


def _flatten_list_block(block: list[str], list_break: str = "") -> list[str] | None:
    """One list without nesting, its short section labels as bold lines; ``None`` when nothing to change.

    A short label (:func:`_is_section_label`) with child items, or followed by
    another item at its level, becomes a bold line of its own with blank lines
    around it, its children a flat list under it. Any other parent stays a
    bullet and its children follow it as continuation lines prefixed with
    ``↳`` (an ordered child keeps its number). An item at the level of a
    labelled section, after that section's children, starts a new list
    (*list_break* between them on the HTML surfaces, a blank line on the
    text surface) so it never reads as one more item of the section.
    """
    items: list[dict] = []
    for line in block:
        if not line.strip():
            continue
        match = _LIST_PARTS_RE.match(line)
        if match:
            items.append(
                {
                    "indent": len(match.group("indent").expandtabs(4)),
                    "marker": match.group("marker"),
                    "content": match.group("content").rstrip(),
                    "more": [],
                    "children": [],
                }
            )
        elif items:
            items[-1]["more"].append(line.strip())
        else:
            return None
    nested = any(item["indent"] != items[0]["indent"] for item in items)
    if not nested and not any(
        _is_section_label(item) and idx + 1 < len(items) and not item["more"] for idx, item in enumerate(items)
    ):
        return None
    out: list[str] = []
    _render_flat_list(_list_tree(items), out, list_break)
    _trim_blank_tail(out)
    return out


def _flatten_nested_lists(lines: list[str], code: list[bool], list_break: str = "") -> tuple[list[str], list[bool]]:
    """Rewrite every nested list outside code as flat lists, short section labels as bold lines.

    Teams flattens a nested list into one bullet list, so a parent bullet used
    as a section label (``- Done:`` over indented items) read as one more
    item. Each such short label becomes a bold line of its own, its children a
    flat list under it; a longer parent stays a bullet with its children
    folded under it (:func:`_flatten_list_block`). A list with no nesting and
    no label is left as written.
    """
    out_lines: list[str] = []
    out_code: list[bool] = []
    idx = 0
    while idx < len(lines):
        if code[idx] or not _is_list_item(lines[idx], in_list=False):
            out_lines.append(lines[idx])
            out_code.append(code[idx])
            idx += 1
            continue
        end = _list_block_end(lines, code, idx)
        flat = _flatten_list_block(lines[idx:end], list_break)
        block = lines[idx:end] if flat is None else flat
        if flat is not None and out_lines and out_lines[-1].strip() and not out_code[-1]:
            # A bold label right under a sentence would join its paragraph.
            out_lines.append("")
            out_code.append(False)
        out_lines.extend(block)
        out_code.extend([False] * len(block))
        idx = end
    return out_lines, out_code


def _decode_text_entities(line: str) -> str:
    """Decode the entities a reader must never see (``&nbsp;``, ``&amp;``) outside inline code."""
    pieces: list[str] = []
    pos = 0
    for match in _INLINE_CODE_RE.finditer(line):
        pieces.append(line[pos : match.start()])
        pieces.append(match.group(0))
        pos = match.end()
    pieces.append(line[pos:])
    for idx in range(0, len(pieces), 2):
        pieces[idx] = _AMP_ENTITY_RE.sub("&", _NBSP_ENTITY_RE.sub(" ", pieces[idx]))
    return "".join(pieces)


def _normalize_markdown_blocks(text: str, *, list_break: str = "", escape_glued_hashes: bool = False) -> str:
    """Give every block of agent Markdown the blank lines Markdown needs to see it.

    Models often write a list straight under a sentence (``Read live:`` then
    ``- item``), a table under a paragraph, or the whole list on the colon's
    line (``Read live: - a - b``). Markdown reads the first two as one
    paragraph, so Teams showed the items run together on one line. Outside
    fenced code, this puts a blank line before and after every list, table
    and heading, turns a one-line run of two or more ``- `` items after a
    colon into a list, decodes ``&nbsp;`` / ``&amp;`` (outside inline code
    too) so no entity reaches the reader, and rewrites a nested list as bold
    section labels over flat lists (:func:`_flatten_nested_lists`), since
    Teams flattens nesting. *list_break* is the line that separates a
    labelled section from the items after it on the HTML surfaces (empty on
    the text surface). A pipe table written without its separator row gets
    one (:func:`add_missing_table_delimiters`), so it renders as a table and
    not as raw pipes. *escape_glued_hashes* (the HTML surface, rendered by
    Python-Markdown) escapes a line-start ``#`` glued to its text
    (``#9195 is ...``), which Python-Markdown would render as a heading
    (:func:`escape_glued_hash`). Deterministic and idempotent.
    """
    source = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    in_code = _code_fence_mask(source)
    lines: list[str] = []
    code: list[bool] = []
    for idx, line in enumerate(source):
        if in_code[idx]:
            lines.append(line)
            code.append(True)
            continue
        line = _decode_text_entities(line)
        if escape_glued_hashes:
            line = escape_glued_hash(line)
        split = _split_inline_list(line)
        for piece in split or [line]:
            lines.append(piece)
            code.append(False)
    lines, code = add_missing_table_delimiters(lines, code)
    lines, code = _flatten_nested_lists(lines, code, list_break)

    out: list[str] = []

    def _blank() -> None:
        if out and out[-1].strip():
            out.append("")

    in_list = False
    in_table = False
    for idx, line in enumerate(lines):
        if code[idx]:
            if idx == 0 or not code[idx - 1]:
                # A fence opens: it is a block of its own.
                _blank()
            in_list = in_table = False
            out.append(line)
            continue
        if idx and code[idx - 1] and line.strip():
            # The line after a closing fence starts a new block.
            _blank()
        if not line.strip():
            in_list = in_table = False
            # Consecutive blank lines are one gap, never two: a closing fence
            # is never blank, so the line above is always outside code.
            if not out or out[-1].strip():
                out.append("")
            continue
        if in_table:
            if "|" in line:
                out.append(line)
                continue
            in_table = False
            _blank()
        if _is_table_start(lines, idx, code):
            _blank()
            in_list = False
            in_table = True
            out.append(line)
            continue
        if _HEADING_RE.match(line):
            _blank()
            in_list = False
            out.append(line)
            if idx + 1 < len(lines) and lines[idx + 1].strip():
                out.append("")
            continue
        if _is_list_item(line, in_list=in_list):
            if not in_list:
                _blank()
            in_list = True
            out.append(line)
            continue
        if in_list and line[:1] in (" ", "\t"):
            # An indented line continues the item above it.
            out.append(line)
            continue
        if in_list:
            in_list = False
            _blank()
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# HTML detection / conversion / sanitization / styling / fallback
# ---------------------------------------------------------------------------


def _mask_code_constructs(body: str) -> str:
    """Return *body* with fenced blocks and inline code spans blanked out.

    Shared by live-HTML and entity-encoded-HTML detection so a tag named
    inside a fence or backticks is never mistaken for document markup.
    """
    lines = body.split("\n")
    in_code = _code_fence_mask(lines)
    return "\n".join(
        "" if in_code[idx] else _INLINE_CODE_RE.sub("", line)
        for idx, line in enumerate(lines)
    )


#: An opening or closing block-level tag (the names of :data:`_BLOCK_HTML_RE`).
_BLOCK_TAG_TOKEN_RE = re.compile(
    r"<\s*(/?)\s*(table|thead|tbody|tr|th|td|p|div|h[1-6]|ul|ol|li|blockquote|pre)\b[^<>]*>",
    re.IGNORECASE,
)


def _escape_unbalanced_block_tags(body: str) -> str:
    """Escape the block tags of a name that is not opened and closed as often.

    An agent naming a tag in running text (``lists as plain <li> with no
    spacing``) writes one ``<li>`` and no ``</li>``. Left live, that tag opens
    a list item the sanitizer closes at the end of the message, so Teams shows
    one bullet holding every later paragraph, bold lead-in and list. A name whose opening and closing tags balance outside code is real
    markup and stays live; the others become ``&lt;li&gt;`` text. Fenced
    blocks and inline code spans are left as they are.
    """
    lines = body.split("\n")
    in_code = _code_fence_mask(lines)
    masked = _mask_code_constructs(body)
    counts: dict[str, int] = {}
    for match in _BLOCK_TAG_TOKEN_RE.finditer(masked):
        name = match.group(2).lower()
        counts[name] = counts.get(name, 0) + (-1 if match.group(1) else 1)
    unbalanced = {name for name, count in counts.items() if count}
    if not unbalanced:
        return body

    def _escape(text: str) -> str:
        return _BLOCK_TAG_TOKEN_RE.sub(
            lambda m: _html.escape(m.group(0)) if m.group(2).lower() in unbalanced else m.group(0),
            text,
        )

    out: list[str] = []
    for idx, line in enumerate(lines):
        if in_code[idx]:
            out.append(line)
            continue
        pieces: list[str] = []
        pos = 0
        for match in _INLINE_CODE_RE.finditer(line):
            pieces.append(_escape(line[pos : match.start()]))
            pieces.append(match.group(0))
            pos = match.end()
        pieces.append(_escape(line[pos:]))
        out.append("".join(pieces))
    return "\n".join(out)


def looks_like_html_teams(body: str) -> bool:
    """Return ``True`` when *body* already contains block-level HTML.

    Block-level tags that appear inside a code construct - a **fenced code
    block** or an **inline code span** (`` `<table>` ``) - do NOT count. A
    Markdown report that embeds an HTML snippet in a fence (e.g. ```` ```html
    <table>... ````) or names a tag in inline code is still Markdown: the
    snippet must be escaped and rendered as code, never passed through as live
    HTML. Detection therefore masks fenced lines (via :func:`_code_fence_mask`)
    and strips inline code spans (via :data:`_INLINE_CODE_RE`) before scanning
    for block tags, so such a body is converted instead of dumped raw /
    mis-rendered. A genuine HTML body's leading block tag sits outside any code
    construct, so it still short-circuits detection to ``True``.
    """
    return bool(_BLOCK_HTML_RE.search(_mask_code_constructs(body)))


#: A line that only Markdown structures: an ATX heading, a list item, or a
#: bold lead-in line (``**Heading**`` / ``**Heading:**``). A line carrying a
#: ``<`` is left out so an HTML document's own lines never count.
_MARKDOWN_STRUCTURE_RE = re.compile(
    r"^[ \t]{0,3}(?:#{1,6}[ \t]+\S|(?:[-*+]|\d{1,9}[.)])[ \t]+\S|\*\*[^*<]+\*\*:?[ \t]*$)"
)


def _looks_like_markdown_document(body: str) -> bool:
    """Return ``True`` when *body* is laid out with Markdown block syntax.

    Outside code constructs, a heading, a list item, a bold lead-in line or a
    GFM table means the agent wrote Markdown. Such a body naming a block tag
    in prose (``the <p> wrapper``) is still Markdown: passed through as HTML,
    its newlines collapse and Teams shows one paragraph with literal ``**``
    and ``- `` runs.
    """
    lines = _mask_code_constructs(body).split("\n")
    in_code = [False] * len(lines)
    for idx, line in enumerate(lines):
        if "<" in line:
            continue
        if _MARKDOWN_STRUCTURE_RE.match(line) or _is_table_start(lines, idx, in_code):
            return True
    return False


def looks_like_escaped_html_teams(body: str) -> bool:
    """Return ``True`` when *body* is HTML whose tags were entity-encoded.

    A document of ``&lt;p&gt;...&lt;/p&gt;`` / ``&lt;ul&gt;...&lt;/ul&gt;``
    wrappers (outside code constructs) is escaped HTML, not Markdown that
    happens to mention a tag name. Double-encoded wrappers
    (``&amp;lt;p&amp;gt;...``) also count: one unescape reveals the
    ``&lt;p&gt;`` form this regex matches.
    """
    if not body or looks_like_html_teams(body):
        return False
    masked = _mask_code_constructs(body)
    if _ENCODED_WRAPPED_HTML_RE.search(masked):
        return True
    if re.search(r"&amp;lt;", masked, re.IGNORECASE):
        return bool(_ENCODED_WRAPPED_HTML_RE.search(_html.unescape(masked)))
    return False


def _unescape_outside_code(body: str) -> str:
    """Unescape entities everywhere EXCEPT inside code constructs.

    Fenced lines are preserved verbatim and inline code spans keep their
    escaped entities, so a code sample that *shows* ``&lt;p&gt;`` is never
    promoted to a live tag by the decode pass. Only the surrounding document
    text - the part :func:`looks_like_escaped_html_teams` actually matched
    against - is unescaped.
    """
    lines = body.split("\n")
    in_code = _code_fence_mask(lines)
    out: list[str] = []
    for idx, line in enumerate(lines):
        if in_code[idx]:
            out.append(line)
            continue
        pieces: list[str] = []
        pos = 0
        for match in _INLINE_CODE_RE.finditer(line):
            pieces.append(_html.unescape(line[pos : match.start()]))
            pieces.append(match.group(0))
            pos = match.end()
        pieces.append(_html.unescape(line[pos:]))
        out.append("".join(pieces))
    return "\n".join(out)


def _decode_escaped_html(body: str) -> str:
    """Unescape entity-encoded HTML so live-tag detection can see it.

    At most two passes (``&amp;lt;p&amp;gt;`` -> ``&lt;p&gt;`` -> ``<p>``).
    Stops as soon as the body no longer looks like escaped HTML, so a
    legitimate ``&amp;`` in Markdown text is never touched. Each pass
    unescapes only OUTSIDE fenced blocks and inline code spans (via
    :func:`_unescape_outside_code`): detection already ignores code
    constructs, so a full-body ``html.unescape`` here would decode a code
    sample's escaped tags too and hand them to the HTML pass-through as live
    markup - the exact leak this module exists to prevent.
    """
    for _ in range(2):
        if not looks_like_escaped_html_teams(body):
            return body
        body = _unescape_outside_code(body)
    return body


def _wellform_html(html: str) -> str:
    """Parse and re-serialize *html* so Graph receives well-formed markup.

    Closes unclosed tags and HTML-escapes bare ``&`` in text (``Q&A`` ->
    ``Q&amp;A``). Graph's chatMessage sanitizer is stricter than nh3: invalid
    HTML is stored as ``contentType: text``, which is how raw tags leak into
    the Teams client.
    """
    soup = BeautifulSoup(html, "html.parser")
    return soup.decode(formatter="html")


def _markdown_to_html(text: str) -> str:
    """Render Markdown to an HTML fragment with GFM tables enabled."""
    return _markdown.markdown(
        text,
        extensions=_MARKDOWN_EXTENSIONS,
        output_format="html",
    )


def _sanitize(html: str) -> str:
    """Strip scripts / event handlers / unsafe URLs against the allow-list."""
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        clean_content_tags=_CLEAN_CONTENT_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_ALLOWED_URL_SCHEMES,
        filter_style_properties=_ALLOWED_STYLE_PROPS,
        link_rel="noopener noreferrer",
    )


def _apply_table_styles(html: str) -> str:
    """Inject baseline inline styles onto Markdown-generated table elements.

    Only elements that do not already carry a ``style`` attribute are touched,
    so an agent-authored HTML table keeps its own styling while a
    Markdown-generated table (which has none) gets visible borders.
    """
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        if not table.get("style"):
            table["style"] = TEAMS_TABLE_STYLE
    for th in soup.find_all("th"):
        if not th.get("style"):
            th["style"] = TEAMS_HEADER_STYLE
    for td in soup.find_all("td"):
        if not td.get("style"):
            td["style"] = TEAMS_CELL_STYLE
    return str(soup)


def _apply_block_spacing(html: str) -> str:
    """Insert a ``<br/>`` between consecutive top-level block elements.

    Markdown-generated HTML packs blocks tightly and Teams renders them with
    almost no vertical gap - it strips CSS ``margin`` / ``padding`` from
    chatMessage HTML by design, so inline spacing styles never take effect. A
    structural ``<br/>`` between sibling blocks is the one spacing primitive
    Teams honors, giving headings, paragraphs, lists and tables room to breathe
    with no per-agent prompt changes.

    Only top-level *block* siblings are separated; list items (``<li>``) and
    table rows (``<tr>``) are nested, so they stay tight. A list gets no
    ``<br/>`` before it: Teams opens a list with a gap of its own, so a
    paragraph or a bold label over a list reads with one standard gap instead
    of two. Fail-open: the
    original HTML is returned unchanged if anything raises, so a spacing hiccup
    can never degrade an otherwise well-rendered report.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        blocks = [
            child
            for child in soup.contents
            if getattr(child, "name", None) in _SPACED_BLOCKS
        ]
        for block in blocks[1:]:
            if getattr(block, "name", None) in _SELF_SPACED_BLOCKS:
                continue
            block.insert_before(soup.new_tag("br"))
        return str(soup)
    except Exception:
        logger.warning(
            "Teams block-spacing failed; returning unspaced HTML", exc_info=True
        )
        return html


#: Elements whose text keeps every whitespace character (code is literal).
_WHITESPACE_KEEP_PARENTS = frozenset({"pre", "code", "textarea"})
#: Elements a line starts in: their first text starts at column 0 in Teams.
_LINE_START_PARENTS = frozenset({
    "p", "li", "td", "th", "blockquote", "div",
    "h1", "h2", "h3", "h4", "h5", "h6",
})
#: Containers whose whitespace-only text children are layout, never content.
_LAYOUT_PARENTS = frozenset({"ul", "ol", "table", "thead", "tbody", "tfoot", "tr"})
#: Elements that end a line: whitespace beside them is never a word gap.
_LINE_BREAKING_TAGS = frozenset(_SPACED_BLOCKS | {"br", "li", "div", "tr", "td", "th"})
#: Whitespace an HTML source carries between tags (``\xa0`` is handled apart).
_SOURCE_WHITESPACE = " \t\r\n"


def _strip_line_start(text: str) -> str:
    """Drop the whitespace a line starts with; keep an intentionally empty line."""
    stripped = text.lstrip(_SOURCE_WHITESPACE)
    if stripped.strip():
        # A line with content: a leading non-breaking space is an indent too.
        return stripped.lstrip(_SOURCE_WHITESPACE + "\xa0")
    # A line holding only ``&nbsp;`` is an intentionally empty line: keep it.
    return stripped


def _strip_line_break_whitespace(html: str, *, formatter: str = "minimal") -> str:
    """Remove the whitespace Teams would show at the start of a line.

    Teams renders a newline or a space kept in chatMessage HTML right after a
    ``<br>`` or at the start of a block as a visible leading space, so the line
    after a break looks indented (``"Kind regards,<br />\\nCEO Office"`` shows
    `` CEO Office``). python-markdown's ``nl2br`` emits exactly that, and so
    does agent HTML written over several source lines. This drops, outside
    ``<pre>`` / ``<code>``:

    * the whitespace (and a leading ``&nbsp;`` before text) of a text node that
      follows a ``<br>`` or opens a block (paragraph, list item, cell, heading);
    * the whitespace a text node ends with before a ``<br>``;
    * whitespace-only text between top-level blocks and inside list / table
      containers, which is source layout only.

    A line holding only ``&nbsp;`` (an intentionally empty line) is kept.
    Fail-open: the HTML is returned unchanged if anything raises.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        for node in list(soup.find_all(string=True)):
            if not isinstance(node, NavigableString) or isinstance(node, Comment):
                continue
            if any(
                getattr(parent, "name", None) in _WHITESPACE_KEEP_PARENTS
                for parent in node.parents
            ):
                continue
            text = str(node)
            parent = node.parent
            parent_name = getattr(parent, "name", None)
            previous = node.previous_sibling
            following = node.next_sibling
            if text and not text.strip(_SOURCE_WHITESPACE) and (
                parent_name in _LAYOUT_PARENTS
                or previous is None
                or following is None
                or getattr(previous, "name", None) in _LINE_BREAKING_TAGS
                or getattr(following, "name", None) in _LINE_BREAKING_TAGS
            ):
                # Source layout between or around blocks and breaks, never the
                # space between two inline words (``<b>a</b> <i>b</i>``).
                node.extract()
                continue
            starts_line = (
                getattr(previous, "name", None) == "br"
                or (previous is None and parent_name in _LINE_START_PARENTS)
            )
            if starts_line:
                text = _strip_line_start(text)
            if getattr(following, "name", None) == "br":
                text = text.rstrip(_SOURCE_WHITESPACE)
            if text != str(node):
                if text:
                    node.replace_with(NavigableString(text))
                else:
                    node.extract()
        return soup.decode(formatter=formatter)
    except Exception:
        logger.warning(
            "Teams line-break whitespace strip failed; returning HTML unchanged",
            exc_info=True,
        )
        return html


def _plaintext_fallback(body: str) -> str:
    """Escape *body* into a valid HTML paragraph (used on conversion failure)."""
    escaped = _html.escape(body).replace("\r\n", "\n").replace("\r", "\n")
    return "<p>" + escaped.replace("\n", "<br>") + "</p>"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def markdown_to_teams_html(body: str, *, assume_html: bool | None = None) -> str:
    """Normalize an agent-authored *body* into safe, Teams-renderable HTML.

    * Markdown (the common case) is converted to HTML with emphasis, headings,
      lists, blockquote, inline + fenced code, ``[label](url)`` links and GFM
      tables (rendered as real ``<table>`` markup, which Teams renders), with
      baseline inline borders applied to the generated tables and a ``<br/>``
      inserted between top-level blocks so the report is not rendered flush
      (Teams strips CSS margins, so the spacing has to be structural).
    * A body that already looks like HTML (it contains block-level HTML tags -
      a table, paragraph, heading, list, etc.) is passed through unchanged
      aside from well-forming (close tags, escape bare ``&``) and security
      sanitization. Its tables and structure are preserved (NEVER converted
      or restyled): the agent crafted real HTML, so we only make it safe.
    * A body whose tags were entity-encoded (``&lt;p&gt;...&lt;/p&gt;``,
      including a double-encoded ``&amp;lt;p&amp;gt;`` form) is unescaped
      first so the HTML path above can see the live tags. Without that,
      Markdown wraps the entities and Teams renders ``<p>`` / ``<b>`` as
      visible characters. Encoded tags inside fenced or inline code are
      left alone.
    * Bare ``https://...`` URLs, and the same URL wrapped in backticks, become
      clickable ``<a href>`` links. Fenced code, existing Markdown links, and
      live ``<a>`` / remaining ``<code>`` / ``<pre>`` are left alone.
    * Plain text still produces valid HTML (paragraphs with ``<br>`` breaks).
    * Every line starts at column 0: the whitespace after a ``<br>`` or at the
      start of a block (which Teams shows as an indent) is dropped on both
      paths, outside ``<pre>`` / ``<code>`` (see
      :func:`_strip_line_break_whitespace`).

    Detection is driven by the body itself, NOT by the caller's
    ``content_type``. ``assume_html`` is advisory: ``True`` / ``None`` both
    auto-detect (so a Markdown body mislabelled ``content_type="html"`` is
    still converted instead of dumped raw), while ``assume_html=False`` forces
    Markdown conversion.

    Fail-open: if rendering or sanitization raises, the body is escaped into a
    plain ``<p>`` block so a malformed report can never block an outgoing Teams
    message nor leak unsanitized markup.
    """
    if not body or not body.strip():
        return ""

    try:
        # Agents stuffing HTML into a JSON tool argument sometimes
        # entity-encode the tags (``&lt;p&gt;``). Decode that BEFORE
        # live-tag detection, otherwise Markdown conversion wraps the
        # entities and Teams shows the tags as visible characters.
        body = _decode_escaped_html(body)
        # The ``assume_html`` hint (driven by the caller's ``content_type``) is
        # ADVISORY, never authoritative. Agents frequently author Markdown while
        # passing ``content_type="html"``, and blindly trusting that hint
        # shipped raw Markdown (literal ``##``, ``**``, ``|`` tables) straight
        # into Teams. So the pass-through path - preserve the agent's own HTML
        # untouched - is taken ONLY when the body actually contains HTML; a
        # Markdown body is always converted, whatever the hint says.
        # ``assume_html=False`` still force-disables pass-through.
        # A Markdown document that names a block tag in prose is Markdown: the
        # pass-through would send it raw and Teams would collapse it into one
        # paragraph.
        # A tag named in running text (one ``<li>``, no ``</li>``) is not
        # markup: it is escaped on the Markdown path, and it alone never makes
        # a body HTML, unless the body opens with a tag (an HTML document
        # that leaves its closing tags implicit).
        prose = _escape_unbalanced_block_tags(body)
        treat_as_html = (
            False
            if assume_html is False
            else looks_like_html_teams(body)
            and (looks_like_html_teams(prose) or body.lstrip().startswith("<"))
            and not _looks_like_markdown_document(body)
        )
        if treat_as_html:
            # Agent-authored HTML: well-form (close tags, escape bare ``&``),
            # autolink bare / <code>-wrapped URLs, then sanitize. No
            # re-spacing - the agent controls its own layout.
            return _sanitize(
                _autolink_html(
                    _strip_line_break_whitespace(_wellform_html(body), formatter="html")
                )
            )
        html = _sanitize(
            _markdown_to_html(
                _linkify_markdown(
                    _normalize_markdown_blocks(prose, list_break=_HTML_LIST_BREAK, escape_glued_hashes=True)
                )
            )
        )
        html = _apply_table_styles(html)
        return _strip_line_break_whitespace(_apply_block_spacing(html))
    except Exception:
        logger.warning(
            "Teams HTML normalization failed; falling back to escaped text",
            exc_info=True,
        )
        return _plaintext_fallback(body)


def markdown_to_teams_text(body: str) -> str:
    """Normalize agent Markdown for the Bot Framework ``text`` field.

    Bot Framework renders bold / italic / lists / links from Markdown but NOT
    headings or tables. This converts GFM tables to record lines and downgrades
    ATX headings (``## Title``) to bold (``**Title**``) - surrounding each
    heading with a blank line so sections breathe - leaving the rest of the
    Markdown intact so Bot Framework renders it natively. Bare and
    backtick-wrapped ``https://...`` URLs become ``[url](url)`` links (Bot
    Framework renders those). Fenced code blocks are preserved verbatim
    (tables, ``#`` lines, and URLs inside them are not rewritten). A block tag
    named in running text (``a <p>`` with no ``</p>``) is escaped so the
    reader sees the tag as text instead of losing it.

    Fail-open: returns the original body unchanged if normalization raises.
    """
    if not body or not body.strip():
        return body or ""

    try:
        text = _linkify_markdown(_normalize_markdown_blocks(_escape_unbalanced_block_tags(body)))
        text = _tables_to_records(text)
        return _collapse_blank_runs(_downgrade_headings(text))
    except Exception:
        logger.warning(
            "Teams text normalization failed; returning original body",
            exc_info=True,
        )
        return body


#: Where an agent places mention N in its body: ``<at id="N">...</at>`` (the
#: text inside is replaced by the mention's name) or ``<at id="N"/>``.
_MENTION_MARKER_RE = re.compile(
    r"<at\b[^>]*?\bid\s*=\s*[\"']?(\d+)[\"']?[^>]*?(?:/>|>.*?</at\s*>)",
    re.IGNORECASE | re.DOTALL,
)


class MentionPlacementError(ValueError):
    """The mention markers of a body do not match the mentions sent with it."""


def _mention_token(nonce: str, index: int) -> str:
    return f"xtmmention{nonce}n{index}e"


def _mark_mentions(body: str, count: int, nonce: str) -> tuple[str, set[int]]:
    """Swap each ``<at id="N">`` marker of *body* for its token."""
    placed: set[int] = set()

    def _token(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if index >= count:
            raise MentionPlacementError(
                f'the message places <at id="{index}"> but only {count} mention(s) '
                f"were given (ids 0 to {count - 1})"
            )
        if index in placed:
            raise MentionPlacementError(
                f'the message places <at id="{index}"> more than once; give each '
                "place its own entry in mentions"
            )
        placed.add(index)
        return _mention_token(nonce, index)

    return _MENTION_MARKER_RE.sub(_token, body), placed


def _write_mention_tags(html: str, mention_texts: Sequence[str], nonce: str) -> str:
    """Turn each mention token of the final *html* into its ``<at>`` tag."""
    written: set[int] = set()

    def _tag(match: re.Match[str]) -> str:
        index = int(match.group(1))
        written.add(index)
        return f'<at id="{index}">{_html.escape(mention_texts[index], quote=False)}</at>'

    html = re.sub(rf"xtmmention{nonce}n(\d+)e", _tag, html)
    lost = [index for index in range(len(mention_texts)) if index not in written]
    if lost:
        raise MentionPlacementError(
            "mention(s) "
            + ", ".join(str(index) for index in lost)
            + " sit where Teams shows no text (a comment, a script or an attribute)"
        )
    return html


def build_graph_chat_body(
    body: str,
    content_type: str | None = None,
    *,
    mention_texts: Sequence[str] = (),
) -> dict[str, str]:
    """Build a Microsoft Graph ``chatMessage`` ``body`` dict - **always HTML**.

    ``contentType`` is forced to ``"html"`` and ``content`` is the normalized
    HTML from :func:`markdown_to_teams_html`, so Markdown is never delivered raw
    with ``contentType: "text"`` (which Teams renders as literal characters).

    ``content_type`` is only an advisory hint - the actual Markdown-vs-HTML
    decision is made from the body itself. An agent that authors real HTML
    keeps it intact; an agent that authors Markdown is converted even if it
    mislabels ``content_type="html"`` (the previous behaviour shipped that
    Markdown raw, so Teams showed literal ``##`` / ``**`` / ``|`` characters).

    ``mention_texts`` are the names of the message's mentions, in the order of
    its Graph ``mentions`` array: mention N is written ``<at id="N">name</at>``
    where the body holds ``<at id="N">...</at>``, else on a first line of its
    own. Raises :class:`MentionPlacementError` when a marker names no mention,
    a mention is placed twice, or a name is empty.
    """
    assume_html = True if (content_type or "").strip().lower() == "html" else None
    if not mention_texts:
        return {
            "contentType": "html",
            "content": markdown_to_teams_html(body, assume_html=assume_html),
        }
    if any(not (text or "").strip() for text in mention_texts):
        raise MentionPlacementError("every mention needs a name to show")
    nonce = secrets.token_hex(8)
    marked, placed = _mark_mentions(body or "", len(mention_texts), nonce)
    leading = [index for index in range(len(mention_texts)) if index not in placed]
    if leading:
        line = " ".join(_mention_token(nonce, index) for index in leading)
        if not marked.strip():
            marked = line
        elif looks_like_html_teams(marked):
            marked = f"<p>{line}</p>{marked}"
        else:
            marked = f"{line}\n\n{marked}"
    html = markdown_to_teams_html(marked, assume_html=assume_html)
    return {
        "contentType": "html",
        "content": _write_mention_tags(html, mention_texts, nonce),
    }


def ensure_full_width_card(card: Any) -> Any:
    """Opt an Adaptive Card into full-width rendering in Microsoft Teams.

    Teams renders Adaptive Cards at a constrained default width unless the card
    root carries ``msteams.width = "Full"``.  Rich content (daily digests,
    multi-column layouts, tables) looks cramped at the default width — and feels
    like a regression from plain-text messages, which always span the full
    conversation width.  Setting ``Full`` makes channel cards render edge-to-edge
    and 1:1/chat cards fill the message bubble.

    A ``dict`` card root is returned as a shallow copy so the caller's object is
    never mutated; an explicit ``msteams.width`` already set by the caller is
    preserved, so a card can still opt back out with ``"Auto"``.  Non-``dict``
    input (e.g. a malformed agent-supplied payload) is returned unchanged — the
    signature is intentionally permissive to reflect that defensive passthrough.

    See https://learn.microsoft.com/microsoftteams/platform/task-modules-and-cards/cards/cards-format#full-width-adaptive-card
    """
    if not isinstance(card, dict):
        return card
    existing = card.get("msteams")
    if isinstance(existing, dict) and "width" in existing:
        # Already opted in/out — still return a shallow copy so callers can
        # safely tweak the returned payload without aliasing their original.
        return {**card}
    merged_msteams = {**existing} if isinstance(existing, dict) else {}
    merged_msteams["width"] = "Full"
    return {**card, "msteams": merged_msteams}
