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

"""Header and footer DEFINED BY THE TEMPLATE, filled once from the deck.

A template's ``extra_metadata.chrome`` declares the PATTERN its decks' footer
(and, optionally, header) reads::

    {
      "footer": "{{event}} - {{title}} - {{classification}}",
      "header": null,
      "variables": [
        {"key": "classification", "label": "Classification", "default": "Internal"}
      ]
    }

A declaration may also carry ``"watermark": true``: the template's MARK (its
``logo_mark`` asset, the ``mark_file_id`` of its palette) drawn faint in the
top-right corner of every body slide, over a soft bloom of the brand gradient
- the "filigrane" the Filigran Brand Deck's body slides wear. It is furniture
like the logo, so a deck that hides its logo (``chrome.show_logo: false``)
hides it too, and a template with no mark draws nothing. Never on a title,
section or closing slide: those have their own backdrops.

``"emblem": true`` draws the same mark the other way round: small, at full
strength, in the top-right corner of every content and section slide - a
brand emblem in the top margin, above the slide title's row. Furniture like
the watermark, under the same
``show_logo`` switch; never on the title or closing slide, whose backdrop
carries the brand. A template may declare either, both or neither. Two
optional keys place it: ``"emblem_size"`` (the mark's height in
canvas pixels, 24 to 96; the default box is 60 px in the top margin) and
``"emblem_align"``: ``"margin"`` (default, in the top safe margin above the
title row) or ``"title"`` (on the right margin, its top on the top of the
slide title, the title row kept clear of it). ``"emblem_ink"`` says how the mark is
inked: ``"mark"`` (default, the asset's own colours, inverted on a light
slide) or ``"accent"`` (one colour: the slide's accent on a light slide, its
foreground on a dark one; for a one-colour mark such as Filigran's).

``"footer_right"`` is a second pattern, drawn in the footer's right corner
INSTEAD of the slide number: it may read the deck's placeholders and
two of the slide's own, ``{{page}}`` (the slide's page, two digits, counted
from the first slide after the cover; empty when ``chrome.show_slide_numbers``
is false) and ``{{year}}`` (the current year, computed when the slide is
drawn or exported). The Filigran Brand Deck reads
``{{page}} · © {{year}} Filigran. All rights reserved.``. Never on the title
or closing slide. A template without it keeps its ``n / total`` number.

Three placeholders are always available — ``{{title}}`` (the deck title),
``{{subtitle}}`` (the opening slide's subtitle) and ``{{event}}`` (the deck's
event label, ``chrome.event_label``). Every other placeholder must be DECLARED
in ``variables`` (key, label, optional hint and default) and is filled from the
deck's ``chrome.fields`` — one value per deck, set in the deck settings or by
the agent, never typed slide by slide. The v1 footer (``event_label`` +
``footer_text`` per deck) keeps working for a free deck or a template that
declares no pattern.

Rendering (:func:`render_chrome_pattern`) is DETERMINISTIC and MIRRORED in the
frontend (``components/presentation/chrome-pattern.ts``), so the footer the
tool result reads back is the footer the audience sees. The rule for an empty
placeholder is the whole subtlety: it renders as nothing AND the separator next
to it collapses, so ``{{event}} - {{title}} - {{classification}}`` with no
event reads ``Roadmap - Internal``, never ``- Roadmap - Internal``. Precisely:

* the pattern is cut into literal and placeholder tokens;
* a pattern with no placeholder renders as written;
* when no placeholder has a value, nothing renders;
* the literal BEFORE the first placeholder and the literal AFTER the last one
  render whenever at least one placeholder has a value;
* a literal BETWEEN two placeholders renders only when the placeholder right
  before it has a value AND a later placeholder has one — it is the separator
  of the value on its left, and a separator needs something on both sides;
* the result is stripped.

Validation (:func:`normalize_template_chrome`) is the write-side twin of the
palette validators in ``template_containers``: a pattern naming an unknown or
undeclared placeholder, a variable with a bad key, a duplicate, a declared
variable no pattern uses, or too many of them is REPORTED to the author and
NOT stored, so a deck never renders a literal ``{{classifcation}}`` because of a
typo nobody was told about. The reader (:func:`template_chrome`) is lenient:
it renders what a stored declaration says and ignores what it cannot read, so
a template written before a rule existed keeps dressing its decks.

Deliberately stdlib-only (plus the equally stdlib-only inline-markup tokenizer
``presentation_markup``): ``presentation_spec`` imports the field grammar from
here for ``Chrome.fields``, so this module must never import the spec back.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.services.presentation_markup import markup_to_plain_text_lenient

#: The ``extra_metadata`` key the declaration lives under.
CHROME_KEY = "chrome"

#: The declaration's switch for the body-slide watermark (see the module docstring).
WATERMARK_KEY = "watermark"

#: The declaration's switch for the corner emblem (see the module docstring).
EMBLEM_KEY = "emblem"

#: The furniture switches a declaration may carry alongside its patterns.
_FURNITURE_KEYS = (WATERMARK_KEY, EMBLEM_KEY)

#: The footer's right-corner pattern (see the module docstring).
FOOTER_RIGHT_KEY = "footer_right"
#: The placeholders only ``footer_right`` reads: filled per SLIDE, not per deck.
SLIDE_PLACEHOLDERS: tuple[str, ...] = ("page", "year")
#: How the corner emblem is placed (see the module docstring).
EMBLEM_SIZE_KEY = "emblem_size"
EMBLEM_ALIGN_KEY = "emblem_align"
EMBLEM_ALIGNS: tuple[str, ...] = ("margin", "title")
EMBLEM_INK_KEY = "emblem_ink"
EMBLEM_INKS: tuple[str, ...] = ("mark", "accent")
EMBLEM_SIZE_MIN = 24
EMBLEM_SIZE_MAX = 96

#: The placeholders every pattern may use without declaring them: read from
#: the deck itself, never typed as a field.
BUILTIN_PLACEHOLDERS: tuple[str, ...] = ("title", "subtitle", "event")

#: What a declared variable's key (and a ``chrome.fields`` key) may look like:
#: a lowercase identifier the pattern spells as ``{{key}}``.
VARIABLE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

#: A ``{{...}}`` slot as it appears in a pattern. Deliberately wider than
#: ``VARIABLE_KEY_RE`` (any word, any case, inner spaces allowed) so that a
#: mistyped slot such as ``{{ Title }}`` is REPORTED as unknown instead of
#: rendering literally on every slide.
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
#: Any ``{{...}}`` chunk at all — what the validator compares against
#: ``PLACEHOLDER_RE`` to report a slot that would render literally.
_BRACES_RE = re.compile(r"\{\{(.*?)\}\}")

#: Bounds of a declaration. A footer is one line: eight variables is already a
#: generous form, two hundred characters of pattern is a paragraph.
MAX_CHROME_VARIABLES = 8
MAX_PATTERN_CHARS = 200
MAX_VARIABLE_LABEL_CHARS = 60
MAX_VARIABLE_HINT_CHARS = 160
#: A field value is a footer label: same ceiling as ``event_label`` /
#: ``footer_text`` (``presentation_spec.MAX_LABEL_CHARS``, pinned by a test).
MAX_FIELD_VALUE_CHARS = 120
#: ``chrome.fields`` entries a deck may carry. Twice the variable cap: a deck
#: relinked to a template with other variables keeps the values it had (they
#: are simply not drawn), and a rename back finds them again.
MAX_DECK_FIELDS = 16

_PATTERN_SLOTS = ("footer", "header")


@dataclass(frozen=True, slots=True)
class ChromeVariable:
    """One value a template asks of every deck (``{{key}}`` in its patterns)."""

    key: str
    label: str
    hint: str | None = None
    default: str | None = None

    def as_dict(self) -> dict[str, str]:
        out = {"key": self.key, "label": self.label}
        if self.hint:
            out["hint"] = self.hint
        if self.default:
            out["default"] = self.default
        return out


@dataclass(frozen=True, slots=True)
class TemplateChrome:
    """A template's header / footer declaration, as the renderer reads it."""

    footer: str | None
    header: str | None
    variables: tuple[ChromeVariable, ...] = ()
    watermark: bool = False
    emblem: bool = False
    footer_right: str | None = None
    emblem_size: int | None = None
    emblem_align: str | None = None
    emblem_ink: str | None = None

    @property
    def declares_pattern(self) -> bool:
        return bool(self.footer or self.header)

    @property
    def placeholders(self) -> tuple[str, ...]:
        """Every placeholder the patterns use, in order of first appearance."""
        seen: list[str] = []
        for pattern in (self.footer, self.header):
            for name in PLACEHOLDER_RE.findall(pattern or ""):
                if name not in seen:
                    seen.append(name)
        return tuple(seen)

    def uses(self, key: str) -> bool:
        return key in self.placeholders

    @property
    def variable_keys(self) -> tuple[str, ...]:
        return tuple(variable.key for variable in self.variables)

    def as_dict(self) -> dict[str, Any]:
        """The canonical stored shape (what the validator writes back).

        ``watermark`` and ``emblem`` are written only when they are on, so a
        declaration that never asked for one keeps the shape it always had.
        """
        out: dict[str, Any] = {
            "footer": self.footer,
            "header": self.header,
            "variables": [variable.as_dict() for variable in self.variables],
        }
        if self.watermark:
            out[WATERMARK_KEY] = True
        if self.emblem:
            out[EMBLEM_KEY] = True
        if self.footer_right:
            out[FOOTER_RIGHT_KEY] = self.footer_right
        if self.emblem_size is not None:
            out[EMBLEM_SIZE_KEY] = self.emblem_size
        if self.emblem_align:
            out[EMBLEM_ALIGN_KEY] = self.emblem_align
        if self.emblem_ink:
            out[EMBLEM_INK_KEY] = self.emblem_ink
        return out


# ── Reading a declaration (lenient) ──────────────────────────────────────────


def _clean_str(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:limit] if text else None


def _read_variable(raw: object) -> ChromeVariable | None:
    if not isinstance(raw, Mapping):
        return None
    key = raw.get("key")
    if not isinstance(key, str) or not VARIABLE_KEY_RE.match(key.strip().lower()):
        return None
    key = key.strip().lower()
    if key in BUILTIN_PLACEHOLDERS:
        return None
    label = _clean_str(raw.get("label"), MAX_VARIABLE_LABEL_CHARS) or key.replace("_", " ").capitalize()
    return ChromeVariable(
        key=key,
        label=label,
        hint=_clean_str(raw.get("hint"), MAX_VARIABLE_HINT_CHARS),
        default=_clean_str(raw.get("default"), MAX_FIELD_VALUE_CHARS),
    )


def template_chrome(extra_metadata: object) -> TemplateChrome | None:
    """The declaration stored in *extra_metadata*, or ``None`` when there is none.

    The READ side: never raises, renders what it can. A declaration with no
    usable footer, no usable header, no watermark and no emblem is no
    declaration; one with furniture alone draws no pattern
    (``declares_pattern`` is false), so the deck keeps its v1 footer. A
    malformed variable is skipped, a duplicate key keeps its first entry, and
    only a literal ``true`` turns the watermark or the emblem on. The strict
    twin is :func:`normalize_template_chrome`.
    """
    if not isinstance(extra_metadata, Mapping):
        return None
    raw = extra_metadata.get(CHROME_KEY)
    if not isinstance(raw, Mapping):
        return None
    footer = _clean_str(raw.get("footer"), MAX_PATTERN_CHARS)
    header = _clean_str(raw.get("header"), MAX_PATTERN_CHARS)
    watermark = raw.get(WATERMARK_KEY) is True
    emblem = raw.get(EMBLEM_KEY) is True
    footer_right = _clean_str(raw.get(FOOTER_RIGHT_KEY), MAX_PATTERN_CHARS)
    size_raw = raw.get(EMBLEM_SIZE_KEY)
    emblem_size = (
        size_raw
        if isinstance(size_raw, int)
        and not isinstance(size_raw, bool)
        and EMBLEM_SIZE_MIN <= size_raw <= EMBLEM_SIZE_MAX
        else None
    )
    align_raw = raw.get(EMBLEM_ALIGN_KEY)
    emblem_align = align_raw if align_raw in EMBLEM_ALIGNS else None
    ink_raw = raw.get(EMBLEM_INK_KEY)
    emblem_ink = ink_raw if ink_raw in EMBLEM_INKS else None
    if not footer and not header and not watermark and not emblem and not footer_right:
        return None
    variables: list[ChromeVariable] = []
    seen: set[str] = set()
    raw_variables = raw.get("variables")
    if isinstance(raw_variables, (list, tuple)):
        for item in raw_variables:
            variable = _read_variable(item)
            if variable is not None and variable.key not in seen:
                seen.add(variable.key)
                variables.append(variable)
    return TemplateChrome(
        footer=footer,
        header=header,
        variables=tuple(variables[:MAX_CHROME_VARIABLES]),
        watermark=watermark,
        emblem=emblem,
        footer_right=footer_right,
        emblem_size=emblem_size,
        emblem_align=emblem_align,
        emblem_ink=emblem_ink,
    )


# ── Validating a declaration (strict) ────────────────────────────────────────


def normalize_template_chrome(raw: object) -> tuple[dict[str, Any] | None, list[str]]:
    """Validate one ``chrome`` declaration as an author wrote it.

    Returns ``(canonical, problems)``. *canonical* is the shape to store
    (``footer`` / ``header`` strings or ``None``, ``variables`` in the order
    given, ``watermark: true`` / ``emblem: true`` when they are on), or
    ``None`` when the declaration says nothing (``null``, ``{}``, or two blank
    patterns with no variable and no furniture) — the caller then DROPS the
    key, which is how "no pattern, v1 footer" is spelled. *problems* are the
    author-facing lines; when any is present the caller must store nothing of it.
    """
    if raw is None:
        return None, []
    if not isinstance(raw, Mapping):
        return None, [
            f"extra_metadata.{CHROME_KEY} must be an object "
            '{"footer": "...", "header": "...", "variables": [...], "watermark": true, '
            f'"emblem": true}}, got {type(raw).__name__}.'
        ]
    problems: list[str] = []
    unknown_keys = sorted(
        str(k)
        for k in raw
        if k
        not in (
            *_PATTERN_SLOTS,
            "variables",
            *_FURNITURE_KEYS,
            FOOTER_RIGHT_KEY,
            EMBLEM_SIZE_KEY,
            EMBLEM_ALIGN_KEY,
            EMBLEM_INK_KEY,
        )
    )
    if unknown_keys:
        problems.append(
            f"extra_metadata.{CHROME_KEY} has keys the renderer does not read: "
            f"{', '.join(unknown_keys)}. Use footer, header, footer_right, variables, watermark, "
            "emblem, emblem_size, emblem_align and emblem_ink only."
        )
    placement: dict[str, Any] = {}
    size_raw = raw.get(EMBLEM_SIZE_KEY)
    if size_raw is not None:
        if (
            isinstance(size_raw, bool)
            or not isinstance(size_raw, int)
            or not EMBLEM_SIZE_MIN <= size_raw <= EMBLEM_SIZE_MAX
        ):
            problems.append(
                f"extra_metadata.{CHROME_KEY}.{EMBLEM_SIZE_KEY} must be a whole number of canvas "
                f"pixels from {EMBLEM_SIZE_MIN} to {EMBLEM_SIZE_MAX}, got {size_raw!r}."
            )
        else:
            placement[EMBLEM_SIZE_KEY] = size_raw
    align_raw = raw.get(EMBLEM_ALIGN_KEY)
    if align_raw is not None:
        if align_raw not in EMBLEM_ALIGNS:
            problems.append(
                f"extra_metadata.{CHROME_KEY}.{EMBLEM_ALIGN_KEY} must be one of "
                f"{', '.join(EMBLEM_ALIGNS)}, got {align_raw!r}."
            )
        else:
            placement[EMBLEM_ALIGN_KEY] = align_raw
    ink_raw = raw.get(EMBLEM_INK_KEY)
    if ink_raw is not None:
        if ink_raw not in EMBLEM_INKS:
            problems.append(
                f"extra_metadata.{CHROME_KEY}.{EMBLEM_INK_KEY} must be one of "
                f"{', '.join(EMBLEM_INKS)}, got {ink_raw!r}."
            )
        else:
            placement[EMBLEM_INK_KEY] = ink_raw
    furniture: dict[str, bool] = {}
    for key in _FURNITURE_KEYS:
        value = raw.get(key)
        if value is not None and not isinstance(value, bool):
            problems.append(
                f"extra_metadata.{CHROME_KEY}.{key} must be true or false, got {type(value).__name__}."
            )
        if value is True:
            furniture[key] = True

    patterns: dict[str, str | None] = {}
    for slot in (*_PATTERN_SLOTS, FOOTER_RIGHT_KEY):
        value = raw.get(slot)
        if value is None:
            patterns[slot] = None
            continue
        if not isinstance(value, str):
            problems.append(
                f"extra_metadata.{CHROME_KEY}.{slot} must be a string pattern such as "
                f'"{{{{event}}}} - {{{{title}}}}" or null, got {type(value).__name__}.'
            )
            patterns[slot] = None
            continue
        text = value.strip()
        if len(text) > MAX_PATTERN_CHARS:
            problems.append(
                f"extra_metadata.{CHROME_KEY}.{slot} is {len(text)} characters; the "
                f"maximum is {MAX_PATTERN_CHARS} - a footer is one line."
            )
        if text.count("{{") != text.count("}}"):
            problems.append(
                f"extra_metadata.{CHROME_KEY}.{slot} has an unbalanced placeholder: every "
                "{{ needs its }}."
            )
        malformed = [
            f"{{{{{inner}}}}}"
            for inner in _BRACES_RE.findall(text)
            if not PLACEHOLDER_RE.fullmatch(f"{{{{{inner}}}}}")
        ]
        if malformed:
            problems.append(
                f"extra_metadata.{CHROME_KEY}.{slot} has slots that are not placeholders: "
                + ", ".join(malformed)
                + ". A placeholder is {{key}} - one word, letters, digits and underscores."
            )
        patterns[slot] = text or None

    variables: list[dict[str, str]] = []
    declared: list[str] = []
    raw_variables = raw.get("variables")
    if raw_variables is None:
        raw_variables = []
    if not isinstance(raw_variables, (list, tuple)):
        problems.append(
            f"extra_metadata.{CHROME_KEY}.variables must be a list of "
            '{"key", "label", "hint"?, "default"?} objects, got '
            f"{type(raw_variables).__name__}."
        )
        raw_variables = []
    if len(raw_variables) > MAX_CHROME_VARIABLES:
        problems.append(
            f"extra_metadata.{CHROME_KEY}.variables declares {len(raw_variables)} variables; "
            f"the maximum is {MAX_CHROME_VARIABLES} - a footer is one line, not a form."
        )
    for index, item in enumerate(raw_variables):
        where = f"extra_metadata.{CHROME_KEY}.variables[{index}]"
        if not isinstance(item, Mapping):
            problems.append(f"{where} must be an object with key and label, got {type(item).__name__}.")
            continue
        key_raw = item.get("key")
        key = key_raw.strip().lower() if isinstance(key_raw, str) else ""
        if not VARIABLE_KEY_RE.match(key):
            problems.append(
                f"{where}.key {key_raw!r} is not a valid key: use a lowercase identifier "
                "(letters, digits, underscores, up to 32 characters) - the pattern spells it {{key}}."
            )
            continue
        if key in BUILTIN_PLACEHOLDERS:
            problems.append(
                f"{where}.key '{key}' is a built-in placeholder ({', '.join(BUILTIN_PLACEHOLDERS)} "
                "are read from the deck itself) and cannot be declared as a variable."
            )
            continue
        if key in declared:
            problems.append(f"{where}.key '{key}' is declared twice.")
            continue
        # A valid key DECLARES its placeholder even when the rest of the row is
        # wrong: a missing label is reported once, as a label problem, never a
        # second time as "no variable declares {{key}}".
        declared.append(key)
        label_raw = item.get("label")
        label = label_raw.strip() if isinstance(label_raw, str) else ""
        if not label:
            problems.append(f"{where}.label is required: it names the field in the deck settings.")
            continue
        if len(label) > MAX_VARIABLE_LABEL_CHARS:
            problems.append(f"{where}.label is over {MAX_VARIABLE_LABEL_CHARS} characters.")
            continue
        extra = sorted(str(k) for k in item if k not in ("key", "label", "hint", "default"))
        if extra:
            problems.append(f"{where} has keys the renderer does not read: {', '.join(extra)}.")
            continue
        entry: dict[str, str] = {"key": key, "label": label}
        for name, limit in (("hint", MAX_VARIABLE_HINT_CHARS), ("default", MAX_FIELD_VALUE_CHARS)):
            value = item.get(name)
            if value is None:
                continue
            if not isinstance(value, str):
                problems.append(f"{where}.{name} must be a string, got {type(value).__name__}.")
                continue
            if len(value.strip()) > limit:
                problems.append(f"{where}.{name} is over {limit} characters.")
                continue
            if value.strip():
                entry[name] = value.strip()
        variables.append(entry)

    right = patterns.get(FOOTER_RIGHT_KEY)
    if right:
        placement[FOOTER_RIGHT_KEY] = right
    misplaced = [
        name
        for slot in _PATTERN_SLOTS
        for name in PLACEHOLDER_RE.findall(patterns[slot] or "")
        if name in SLIDE_PLACEHOLDERS
    ]
    if misplaced:
        problems.append(
            f"extra_metadata.{CHROME_KEY}: "
            + ", ".join(f"{{{{{name}}}}}" for name in dict.fromkeys(misplaced))
            + f" is read per slide and only {FOOTER_RIGHT_KEY} may use it."
        )

    if not patterns["footer"] and not patterns["header"] and not right:
        # No pattern: "no declaration", which the caller spells by dropping
        # the key - unless variables were declared into the void - or the
        # furniture alone.
        if variables:
            problems.append(
                f"extra_metadata.{CHROME_KEY} declares variables but no footer or header "
                "pattern to fill them into."
            )
        if problems or not furniture:
            return None, problems
        return {"footer": None, "header": None, "variables": [], **furniture, **placement}, []

    used: list[str] = []
    for slot in (*_PATTERN_SLOTS, FOOTER_RIGHT_KEY):
        for name in PLACEHOLDER_RE.findall(patterns.get(slot) or ""):
            if name not in used:
                used.append(name)
    unknown = [
        name
        for name in used
        if name not in BUILTIN_PLACEHOLDERS and name not in SLIDE_PLACEHOLDERS and name not in declared
    ]
    if unknown:
        allowed = ", ".join(
            f"{{{{{name}}}}}" for name in (*BUILTIN_PLACEHOLDERS, *declared, *SLIDE_PLACEHOLDERS)
        )
        problems.append(
            f"extra_metadata.{CHROME_KEY} uses placeholders no variable declares: "
            + ", ".join(f"{{{{{name}}}}}" for name in unknown)
            + f". Available: {allowed}. Declare the missing ones in variables (key, label)."
        )
    unused = [key for key in declared if key not in used]
    if unused:
        problems.append(
            f"extra_metadata.{CHROME_KEY}.variables declares "
            + ", ".join(f"'{key}'" for key in unused)
            + ", which no footer or header pattern uses: put {{"
            + unused[0]
            + "}} in a pattern or remove the variable."
        )
    if problems:
        return None, problems
    canonical: dict[str, Any] = {
        "footer": patterns["footer"],
        "header": patterns["header"],
        "variables": variables,
        **furniture,
        **placement,
    }
    return canonical, []


# ── Rendering ────────────────────────────────────────────────────────────────


def _tokenize(pattern: str) -> list[tuple[str, str]]:
    """``[("lit", text) | ("ph", key), ...]`` — the pattern cut at its placeholders."""
    tokens: list[tuple[str, str]] = []
    cursor = 0
    for match in PLACEHOLDER_RE.finditer(pattern):
        if match.start() > cursor:
            tokens.append(("lit", pattern[cursor : match.start()]))
        tokens.append(("ph", match.group(1)))
        cursor = match.end()
    if cursor < len(pattern):
        tokens.append(("lit", pattern[cursor:]))
    return tokens


def render_chrome_pattern(pattern: str | None, values: Mapping[str, object]) -> str:
    """Fill *pattern* with *values*; an empty value takes its separator with it.

    See the module docstring for the rule. *values* maps placeholder names to
    strings; anything missing, ``None`` or blank counts as empty. The frontend
    mirror (``renderChromePattern``) implements exactly this.
    """
    if not pattern or not pattern.strip():
        return ""
    tokens = _tokenize(pattern)
    resolved: list[str | None] = []
    for kind, text in tokens:
        if kind == "ph":
            value = values.get(text)
            resolved.append(value.strip() if isinstance(value, str) else "")
        else:
            resolved.append(None)
    placeholder_positions = [i for i, (kind, _text) in enumerate(tokens) if kind == "ph"]
    if not placeholder_positions:
        return pattern.strip()
    if not any(resolved[i] for i in placeholder_positions):
        return ""
    first, last = placeholder_positions[0], placeholder_positions[-1]
    out: list[str] = []
    for i, (kind, text) in enumerate(tokens):
        if kind == "ph":
            out.append(resolved[i] or "")
            continue
        if i < first or i > last:
            out.append(text)
            continue
        # A separator: the placeholder right before it must have a value, and
        # so must one of the placeholders after it.
        before = max(p for p in placeholder_positions if p < i)
        if resolved[before] and any(resolved[p] for p in placeholder_positions if p > i):
            out.append(text)
    return "".join(out).strip()


def opening_subtitle(spec: Mapping[str, Any]) -> str:
    """The ``{{subtitle}}`` value: the opening (title) slide's subtitle, as plain text.

    The subtitle is inline MARKUP (``**bold**``, ``[[accent:text]]``) drawn by
    the slide; the footer draws text, so the markers are stripped the way the
    frontend tokenizer strips them (``markup_to_plain_text_lenient`` walks the
    same grammar as ``markupToPlainText``) and the line is folded onto one
    line - a footer never wraps.

    The edit tool's redraw decision (``subtitle_changed`` in
    ``builtin_tools/presentation.py``) compares THIS value before and after an
    edit, so a subtitle change is compared as the footer DRAWS it - plain
    text: an edit that only changes the subtitle's markup is not a redraw. That
    is sound only because the chrome renders plain text; if it ever renders
    markup, the comparison must move to the raw subtitle.
    """
    for slide in spec.get("slides") or []:
        if isinstance(slide, Mapping) and slide.get("layout") == "title":
            subtitle = slide.get("subtitle")
            if not isinstance(subtitle, str):
                return ""
            return " ".join(markup_to_plain_text_lenient(subtitle).split())
    return ""


def canonical_field_key(raw: object) -> str:
    """The ONE spelling of a ``chrome.fields`` key: stripped, lower-cased.

    The single normaliser for deck field keys, shared by the validator
    (``Chrome._clean_fields`` runs it before checking ``VARIABLE_KEY_RE``),
    the ``update_deck`` merge (:func:`merge_deck_fields`) and the reader
    (:func:`deck_fields`), so ``{"Classification": ...}`` names the same field
    everywhere it is looked at - never two spellings for the validator to
    collapse later.
    """
    return str(raw).strip().lower()


def field_value_unset(value: object) -> bool:
    """``None`` and blank text both mean "no value" for a deck field.

    The validator drops both; the ``update_deck`` merge REMOVES the key for
    both, so "clear the classification" works with ``null`` and with ``""``.
    """
    return value is None or (isinstance(value, str) and not value.strip())


def merge_deck_fields(existing: object, incoming: Mapping[str, object]) -> dict[str, object] | None:
    """Key-by-key merge of ``chrome.fields`` for ``update_deck``.

    Both maps are read through :func:`canonical_field_key`, so a value sent
    as ``{"Classification": "Confidential"}`` overwrites the stored
    ``classification`` (and ``{"Classification": null}`` clears it) instead
    of landing beside it. An unset value (``None`` or blank) removes the key;
    any other value is stored as sent and validated with the deck. ``None``
    when nothing is left, which is how an empty map is stored (absent).
    """
    merged: dict[str, object] = {}
    if isinstance(existing, Mapping):
        for key, value in existing.items():
            merged[canonical_field_key(key)] = value
    for key, value in incoming.items():
        canonical = canonical_field_key(key)
        if field_value_unset(value):
            merged.pop(canonical, None)
        else:
            merged[canonical] = value
    return merged or None


def deck_fields(spec: Mapping[str, Any]) -> dict[str, str]:
    """The deck's ``chrome.fields`` as a clean ``{key: value}`` map."""
    chrome = spec.get("chrome")
    fields = chrome.get("fields") if isinstance(chrome, Mapping) else None
    if not isinstance(fields, Mapping):
        return {}
    return {
        canonical_field_key(key): value.strip()
        for key, value in fields.items()
        if isinstance(value, str) and value.strip()
    }


def chrome_values(spec: Mapping[str, Any], chrome: TemplateChrome | None) -> dict[str, str]:
    """Every value a pattern of *chrome* may draw from *spec*.

    Built-ins come from the deck itself; a declared variable reads the deck's
    ``chrome.fields[key]`` and falls back to the variable's ``default``. With
    no declaration only the built-ins are returned.
    """
    deck_chrome = spec.get("chrome") if isinstance(spec.get("chrome"), Mapping) else {}
    event = deck_chrome.get("event_label") if isinstance(deck_chrome, Mapping) else None
    title = spec.get("title")
    values: dict[str, str] = {
        "title": title.strip() if isinstance(title, str) else "",
        "subtitle": opening_subtitle(spec),
        "event": event.strip() if isinstance(event, str) else "",
    }
    fields = deck_fields(spec)
    for variable in chrome.variables if chrome is not None else ():
        values[variable.key] = fields.get(variable.key) or variable.default or ""
    return values


@dataclass(frozen=True, slots=True)
class RenderedChrome:
    footer: str
    header: str


def render_chrome(spec: Mapping[str, Any], chrome: TemplateChrome | None) -> RenderedChrome:
    """The footer and header text a deck wearing *chrome* draws (``""`` = nothing).

    A deck with no template pattern draws nothing THROUGH this path - its v1
    footer (``event_label`` + ``footer_text``) is the frontend's own business.
    """
    if chrome is None:
        return RenderedChrome(footer="", header="")
    values = chrome_values(spec, chrome)
    return RenderedChrome(
        footer=render_chrome_pattern(chrome.footer, values),
        header=render_chrome_pattern(chrome.header, values),
    )


def describe_template_chrome(chrome: TemplateChrome | None, *, indent: str = "  - ") -> list[str]:
    """The template's footer / header declaration as the agent reads it back.

    Shared by ``search_templates`` / ``get_template`` and the authoring tool's
    read-back, so the two describe one declaration in one wording: the pattern
    as written, then which deck setting fills each placeholder - the built-ins
    from the deck itself, the declared variables from ``chrome.fields`` - and
    the watermark when the template draws one.
    """
    if chrome is None:
        return []
    lines: list[str] = []
    if chrome.watermark:
        lines.append(
            f"{indent}watermark: the chrome draws the template's mark faint on every body slide "
            "(chrome.show_logo false hides it with the logo)"
        )
    if chrome.emblem:
        placed = ""
        if chrome.emblem_size is not None:
            placed += f", {chrome.emblem_size} px"
        if chrome.emblem_align == "title":
            placed += ", aligned with the top of the slide title"
        if chrome.emblem_ink == "accent":
            placed += ", inked in the slide's accent on a light slide and its foreground on a dark one"
        lines.append(
            f"{indent}emblem: the chrome draws the template's mark small in the top-right corner "
            f"of every body slide{placed} (chrome.show_logo false hides it with the logo)"
        )
    if chrome.footer_right:
        lines.append(
            f'{indent}footer right on every body slide: "{chrome.footer_right}" '
            "({{page}} = the page, two digits from the first slide after the cover, empty when "
            "chrome.show_slide_numbers is false; {{year}} = the current year)"
        )
    if not chrome.declares_pattern:
        return lines
    if chrome.footer:
        lines.append(f'{indent}footer on every slide: "{chrome.footer}"')
    if chrome.header:
        lines.append(f'{indent}header on every slide: "{chrome.header}"')
    used_builtins = [key for key in BUILTIN_PLACEHOLDERS if chrome.uses(key)]
    filled: list[str] = []
    if used_builtins:
        sources = {
            "title": "{{title}} = the deck title",
            "subtitle": "{{subtitle}} = the opening slide's subtitle",
            "event": "{{event}} = chrome.event_label",
        }
        filled.append(", ".join(sources[key] for key in used_builtins))
    for variable in chrome.variables:
        default = f', default "{variable.default}"' if variable.default else ", empty unless set"
        filled.append(f"{{{{{variable.key}}}}} = chrome.fields.{variable.key} ({variable.label}{default})")
    if filled:
        lines.append(
            f"{indent}filled once per deck from its settings, never from slide text: " + "; ".join(filled)
        )
    return lines


def template_chrome_redraws(
    chrome: TemplateChrome | None,
    deck_fields: Sequence[str],
    *,
    subtitle_changed: bool = False,
) -> bool:
    """Whether an edit redrew every slide THROUGH the template pattern.

    The layout check after an edit is scoped to the slides the edit wrote,
    widened to the whole deck only for a deck-level field drawn on every slide
    (``LAYOUT_DECK_FIELDS``: theme, aspect ratio, chrome). A template
    footer that reads ``{{title}}`` makes the deck TITLE such a field for that
    deck, and one that reads ``{{subtitle}}`` makes the opening slide's
    subtitle one; this is the per-deck half of that decision, kept out of the
    global set on purpose - a rename on a deck whose footer never shows the
    title must still not render the whole deck.

    *subtitle_changed* is whether the ``{{subtitle}}`` value differs between
    the baseline and the saved spec (``opening_subtitle`` on both), not whether
    the opening slide was written: a delete or a move that puts another title
    slide first changes the value while writing no slide, and a rewrite that
    leaves the subtitle alone changes nothing the footer shows. The value is
    the PLAIN-FOLDED one, as the footer draws it, so a markup-only subtitle
    edit is not a redraw - correct only while the chrome renders plain text
    (see ``opening_subtitle``).

    The edit tool asks this ONE question twice - whether to render the whole
    deck for the layout check, and whether to read the footer / header back
    (``footer_status_line``) - so the two can never disagree on which deck a
    rename or a subtitle change concerns.
    """
    if chrome is None or not chrome.declares_pattern:
        return False
    if "title" in deck_fields and chrome.uses("title"):
        return True
    return subtitle_changed and chrome.uses("subtitle")


def undeclared_fields(spec: Mapping[str, Any], chrome: TemplateChrome | None) -> list[str]:
    """``chrome.fields`` keys the deck carries that no template variable reads."""
    fields = deck_fields(spec)
    if not fields:
        return []
    known = set(chrome.variable_keys) if chrome is not None else set()
    return sorted(key for key in fields if key not in known)


def footer_status_line(spec: Mapping[str, Any], chrome: TemplateChrome | None) -> str:
    """The ``Footer:`` line of a tool result — what the footer READS, and where it comes from.

    Grounding, not prompting: after "mark the deck Confidential" the
    agent quotes this line instead of guessing what the slides now say. With a
    template pattern it names the pattern and the fields it draws from, so the
    next request ("use the customer's name") is answered by setting ONE
    ``chrome.fields`` value, never by editing every slide; without one it
    reads the v1 ``event_label`` + ``footer_text`` pair.
    """
    deck_chrome = spec.get("chrome") if isinstance(spec.get("chrome"), Mapping) else {}
    show_footer = deck_chrome.get("show_footer", True) if isinstance(deck_chrome, Mapping) else True
    if show_footer is False:
        return "Footer: hidden (chrome.show_footer is false)."
    if chrome is not None and chrome.declares_pattern:
        values = chrome_values(spec, chrome)
        rendered = render_chrome(spec, chrome)
        parts: list[str] = []
        if chrome.footer:
            reads = f'"{rendered.footer}"' if rendered.footer else "nothing (every placeholder is empty)"
            parts.append(f"Footer: reads {reads} - template pattern \"{chrome.footer}\"")
        if chrome.header:
            reads = f'"{rendered.header}"' if rendered.header else "nothing (every placeholder is empty)"
            parts.append(f"Header: reads {reads} - template pattern \"{chrome.header}\"")
        slots = [
            f"{{{{{name}}}}}=" + (f'"{values.get(name, "")}"' if values.get(name) else "empty")
            for name in chrome.placeholders
        ]
        fields = [
            f"chrome.fields.{variable.key} ({variable.label})" for variable in chrome.variables
        ]
        line = "; ".join(parts) + (f". Values: {', '.join(slots)}" if slots else "")
        if fields:
            line += (
                f". Set once for the whole deck with chrome.fields: {', '.join(fields)} "
                "- never by editing slides."
            )
        else:
            line += ". Filled from the deck title, the opening slide's subtitle and chrome.event_label."
        ignored = undeclared_fields(spec, chrome)
        if ignored:
            line += f" Ignored chrome.fields (not declared by the template): {', '.join(ignored)}."
        return line
    event = deck_chrome.get("event_label") if isinstance(deck_chrome, Mapping) else None
    text = deck_chrome.get("footer_text") if isinstance(deck_chrome, Mapping) else None
    shown = [part.strip() for part in (event, text) if isinstance(part, str) and part.strip()]
    if shown:
        # The v1 footer draws its two parts with a middle dot between them.
        return (
            f'Footer: reads "{" · ".join(shown)}" - from chrome.event_label and chrome.footer_text '
            "(no template pattern; a template can define the footer for every deck it dresses)."
        )
    return (
        "Footer: empty - set chrome.event_label / chrome.footer_text, or link a template that "
        "defines the footer."
    )
