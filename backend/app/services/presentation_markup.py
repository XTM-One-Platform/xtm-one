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

"""Inline markup for presentation text — parser + validator.

Every human-readable string in a v2 slide (block text, headings, list items,
table cells, callout copy...) may carry a *tiny* inline markup instead of HTML:

* ``**bold**``
* ``_emphasis_`` (only at word boundaries, so ``snake_case`` stays literal)
* ```` `code` ````
* ``[[accent:text]]`` — a tone-coloured span; the tone is one of
  :data:`MARKUP_TONES` (``accent``, ``info``, ``success``, ``warning``,
  ``error``, ``muted``) or ``gradient`` for the brand gradient
* ``[text](#/<slide id>)`` — a link to another slide of the SAME deck, by the
  target's stable id (:data:`SLIDE_LINK_REF_PATTERN`, the grammar of
  ``SLIDE_ID_PATTERN``). Never a position: an id follows the slide when the
  deck is reordered, a number would silently point at another one.
* ``[text](https://...)`` / ``[text](mailto:...)`` — a link to a web page or an
  e-mail address (:data:`LINK_URL_PATTERN`: the schemes the banner action
  admits, never ``javascript:``, ``data:`` or ``http:``). A Markdown link to
  anything else stays literal text, as it always did - the save names it
  (:func:`literal_markdown_links`).
* a newline (``\\n``) — a line break

That is the whole grammar. It is escape-first and flat on purpose: the frontend
renders it to React nodes (``inline-markup.tsx``), never through an HTML sink,
so a deck string can never smuggle markup past the renderer, and the PPTX
writer turns the same segments into text runs. No nesting — a marker inside
another span is an error at generation time rather than a surprise at render
time — and no HTML: a tag-like sequence is refused with a pointer to the markup
above, because an agent that writes ``<b>`` here would otherwise see the tag
printed literally on the slide.

:func:`parse_inline_markup` is the single implementation; :func:`check_markup`
is the Pydantic-facing validator built on it. The frontend tokenizer mirrors
the same token set, and ``tests/test_presentation_spec_v2.py`` pins the two.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Tones a ``[[tone:text]]`` span may name. ``gradient`` is not a tone in the
#: block sense (it is the brand gradient text treatment) but shares the syntax.
MARKUP_TONES: tuple[str, ...] = (
    "accent",
    "info",
    "success",
    "warning",
    "error",
    "muted",
)
MARKUP_SPAN_KINDS: tuple[str, ...] = (*MARKUP_TONES, "gradient")

#: Short grammar summary quoted in tool descriptions and validation messages,
#: so the prompt can never advertise a construct the parser rejects.
MARKUP_SUMMARY = (
    "**bold**, _emphasis_, `code`, [[accent:text]] (tones: "
    + ", ".join(MARKUP_SPAN_KINDS)
    + "), [text](#/SLIDE_ID) to link a slide of the deck by its id, "
    "[text](https://...) or [text](mailto:...) to link out, "
    "and \\n for a line break"
)

#: The ref of a ``[text](#/<ref>)`` slide link: a slide id
#: (``presentation_spec.SLIDE_ID_PATTERN`` without its anchors - pinned by
#: ``tests/test_presentation_slide_links_in_deck.py``).
SLIDE_LINK_REF_PATTERN = r"[A-Za-z0-9_-]{1,64}"

#: The address of a web or e-mail link: ``presentation_spec.check_action_url``'s
#: two schemes, without parentheses (the one that ends the link) and without
#: quotes, angle brackets, backticks or whitespace. A slide id holds no ``:``,
#: so a link's target is an address exactly when it matches this.
_URL_CHAR = r"""[^\s<>"'`()]"""
_MAIL_CHAR = r"""[^\s<>"'`()@]"""
LINK_URL_PATTERN = rf"https://{_URL_CHAR}+|mailto:{_MAIL_CHAR}+@{_MAIL_CHAR}+\.{_MAIL_CHAR}+"

_TOKEN_RE = re.compile(
    r"(?P<bold>\*\*(?P<bold_text>[^\n]+?)\*\*)"
    r"|(?P<code>`(?P<code_text>[^`\n]+)`)"
    r"|(?P<span>\[\[(?P<span_kind>[a-zA-Z_]+):(?P<span_text>[^\]\n]+?)\]\])"
    rf"|(?P<link>\[(?P<link_text>[^\[\]\n]+)\]\((?:#/(?P<link_ref>{SLIDE_LINK_REF_PATTERN})|(?P<link_url>{LINK_URL_PATTERN}))\))"
    r"|(?P<em>(?<![A-Za-z0-9_])_(?P<em_text>[^_\n]+?)_(?![A-Za-z0-9_]))"
    r"|(?P<br>\n)"
)
#: Anything that reads as an HTML/XML tag: ``<b>``, ``</p>``, ``<br/>``,
#: ``<!--``. A bare ``<`` (``a < b``, ``<3``) is fine.
_TAG_LIKE_RE = re.compile(r"<\s*/?\s*[A-Za-z!]")
#: ``](#/`` is the slide link's own marker: left over in plain text it is a
#: link the parser could not read (an empty label, a ref that is no slide id).
_MARKER_RE = re.compile(r"\*\*|`|\[\[|\]\]|\]\(#\/")
#: A Markdown link the grammar does not read (``[x](http://...)``,
#: ``[x](/path)``): literal text on the slide, named by the save.
_MARKDOWN_LINK_RE = re.compile(r"\[[^\[\]\n]+\]\([^\s]*?\)")
#: A whole tag, for flattening text that was never markup (``<b>x</b>`` -> ``x``).
_TAG_RE = re.compile(r"<[^>]*>?")
_HORIZONTAL_SPACE_RE = re.compile(r"[ \t\r\f\v]+")


@dataclass(frozen=True, slots=True)
class Segment:
    """One rendered run of inline markup.

    ``kind`` is ``text`` / ``bold`` / ``em`` / ``code`` / ``span`` / ``link``
    / ``br``; ``tone`` is set for ``span`` only (one of
    :data:`MARKUP_SPAN_KINDS`), ``target`` for ``link`` only: the slide id, or
    the address (:data:`LINK_URL_PATTERN`) of a web or e-mail link.
    """

    kind: str
    text: str = ""
    tone: str | None = None
    target: str | None = None


class InlineMarkupError(ValueError):
    """The string is not valid inline markup (message is agent-readable)."""


def _reject_nested(text: str, construct: str) -> None:
    if _MARKER_RE.search(text) or "\n" in text:
        raise InlineMarkupError(
            f"inline markup cannot be nested: the {construct} contains another "
            "marker (**, `, [[ ]], a slide link or a line break). Close one "
            "before opening the next."
        )


def parse_inline_markup(value: str) -> list[Segment]:
    """Tokenize *value* into segments, raising :class:`InlineMarkupError`.

    Plain text between markers becomes ``text`` segments. Unbalanced ``**`` /
    ```` ` ```` / ``[[`` ``]]`` markers left over in plain text are an error
    (rather than rendered literally) so a typo is caught where the agent can
    fix it; an isolated ``_`` is legitimate text (``snake_case``, ``file_id``)
    and stays literal.
    """
    if _TAG_LIKE_RE.search(value):
        raise InlineMarkupError(
            "HTML is not allowed in this field. Use inline markup instead: "
            f"{MARKUP_SUMMARY}."
        )
    segments: list[Segment] = []
    cursor = 0

    def _plain(chunk: str) -> None:
        if not chunk:
            return
        leftover = _MARKER_RE.search(chunk)
        if leftover:
            raise InlineMarkupError(
                f"unbalanced inline marker {leftover.group(0)!r}: every ** and ` "
                "must be closed, [[tone:text]] needs both brackets, and a slide "
                "link is [text](#/SLIDE_ID) with a label and the target's id. "
                f"Allowed markup: {MARKUP_SUMMARY}."
            )
        segments.append(Segment("text", chunk))

    for match in _TOKEN_RE.finditer(value):
        _plain(value[cursor : match.start()])
        cursor = match.end()
        if match.group("bold") is not None:
            text = match.group("bold_text")
            _reject_nested(text, "**bold** span")
            segments.append(Segment("bold", text))
        elif match.group("code") is not None:
            text = match.group("code_text")
            _reject_nested(text, "`code` span")
            segments.append(Segment("code", text))
        elif match.group("span") is not None:
            kind = match.group("span_kind").lower()
            text = match.group("span_text")
            if kind not in MARKUP_SPAN_KINDS:
                raise InlineMarkupError(
                    f"unknown span tone {kind!r} in [[{kind}:...]]. Use one of: "
                    + ", ".join(MARKUP_SPAN_KINDS)
                    + "."
                )
            _reject_nested(text, f"[[{kind}:...]] span")
            segments.append(Segment("span", text, tone=kind))
        elif match.group("link") is not None:
            text = match.group("link_text")
            _reject_nested(text, "[text](...) link")
            segments.append(Segment("link", text, target=match.group("link_ref") or match.group("link_url")))
        elif match.group("em") is not None:
            text = match.group("em_text")
            _reject_nested(text, "_emphasis_ span")
            segments.append(Segment("em", text))
        else:
            segments.append(Segment("br"))
    _plain(value[cursor:])
    return segments


def check_markup(value: str | None) -> str | None:
    """Pydantic ``AfterValidator``: accept *value* iff it parses as markup."""
    if value is None:
        return None
    try:
        parse_inline_markup(value)
    except InlineMarkupError as exc:
        raise ValueError(str(exc)) from None
    return value


def markup_to_plain_text(value: str | None) -> str:
    """Strip markers, keeping the readable text (summaries, alt text).

    Built on the strict parser: a value it refuses (an unknown tone, a stray
    marker, a tag) is returned as written. Right for VALIDATED slide text,
    where the refusal already happened at generation time and this fallback
    never runs; for text that is not validated, see
    :func:`markup_to_plain_text_lenient`.
    """
    if not value:
        return ""
    try:
        segments = parse_inline_markup(value)
    except InlineMarkupError:
        return value
    return "".join("\n" if seg.kind == "br" else seg.text for seg in segments)


def markup_to_plain_text_lenient(value: str | None) -> str:
    """Strip markers the way the frontend renderer does - never refusing.

    Speaker notes are not validated as markup (a stray ``*`` in something the
    presenter only reads must never refuse a deck), and the presenter reads
    them through the frontend tokenizer (``markup.ts``), which never refuses
    either: every marker it recognises is stripped, an unknown
    ``[[tone:text]]`` keeps its text and drops the wrapper, and anything else
    - an unbalanced marker, a tag-like ``<b>`` - stays literal. The PowerPoint
    notes pane and the speaking estimate must read the SAME text the speaker
    view shows, so this walks :data:`_TOKEN_RE` exactly as that tokenizer does
    instead of falling back to the raw line, which would count ``[[warn:risk]]``
    as two words where the presenter reads one.
    """
    if not value:
        return ""
    parts: list[str] = []
    cursor = 0
    for match in _TOKEN_RE.finditer(value):
        parts.append(value[cursor : match.start()])
        cursor = match.end()
        if match.group("bold") is not None:
            parts.append(match.group("bold_text"))
        elif match.group("code") is not None:
            parts.append(match.group("code_text"))
        elif match.group("span") is not None:
            parts.append(match.group("span_text"))
        elif match.group("link") is not None:
            parts.append(match.group("link_text"))
        elif match.group("em") is not None:
            parts.append(match.group("em_text"))
        else:
            parts.append("\n")
    parts.append(value[cursor:])
    return "".join(parts)


def slide_link_targets(value: str | None) -> list[str]:
    """The slide ids the ``[text](#/<slide id>)`` links of *value* point at, in order.

    Read the way the renderer reads them (:data:`_TOKEN_RE`, never refusing),
    so a deck string the strict parser would refuse still reports the links it
    shows - speaker notes are not markup-validated and are drawn by the same
    tokenizer.
    """
    if not value or "](#/" not in value:
        return []
    return [match.group("link_ref") for match in _TOKEN_RE.finditer(value) if match.group("link_ref") is not None]


def literal_markdown_links(value: str | None) -> list[str]:
    """The Markdown links of *value* the grammar does not read, as written.

    ``[x](http://...)``, ``[x](/path)``, ``[x](ftp://...)``: the slide shows
    them as the literal text, brackets and address included. Read in the
    plain text between the tokens, the way the renderer walks it.
    """
    if not value or "](" not in value:
        return []
    found: list[str] = []
    cursor = 0
    for match in _TOKEN_RE.finditer(value):
        found.extend(_MARKDOWN_LINK_RE.findall(value[cursor : match.start()]))
        cursor = match.end()
    found.extend(_MARKDOWN_LINK_RE.findall(value[cursor:]))
    return found


# ── Fitting text INTO a markup field ─────────────────────────────────────────
#
# The converters that build a slide out of another one - the layout switch
# and the upgrade of a retired v1 slide - write strings they
# did not author into fields with a length cap and the markup grammar above. A
# value that already fits travels as written; anything else is flattened to
# what the renderer would have shown and cut, so the built slide validates
# instead of being refused on a marker the person never typed.


def cut_text(text: str, limit: int) -> str:
    """*text* cut to *limit* characters, with an ellipsis when it was cut."""
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 3)].rstrip() + "..."


def fit_text(value: object, limit: int) -> str | None:
    """A plain string fitted to *limit* characters (``None`` when blank or not a string)."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return cut_text(text, limit) if text else None


def fit_markup(value: object, limit: int) -> str | None:
    """Fit *value* into an inline-markup field of at most *limit* characters.

    Valid markup within the limit travels as written. Anything else - a plain
    string carrying a stray ``**``, a tag-like ``<b>``, markup too long for the
    target field - is flattened to plain text the way the renderer reads it
    (:func:`markup_to_plain_text_lenient`), stripped of tags and stray markers,
    and cut. ``None`` when nothing readable is left.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) <= limit:
        try:
            check_markup(text)
        except ValueError:
            pass
        else:
            return text
    plain = markup_to_plain_text_lenient(text)
    plain = _TAG_RE.sub("", plain)
    plain = _MARKER_RE.sub("", plain)
    plain = _HORIZONTAL_SPACE_RE.sub(" ", plain).strip()
    return cut_text(plain, limit) if plain else None
