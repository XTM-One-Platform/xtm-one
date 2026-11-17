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

"""``PresentationSpec`` — the validated contract for agent-generated decks.

This module is the **single source of truth** for what a slide deck may contain
(a block-based v2). The agent-facing
``generate_presentation`` tool, the REST endpoints, the template-container
``layouts`` guardrail, the built-in brand seed and the tests all validate through
:func:`validate_spec` and describe the format through :func:`layout_catalog`,
:func:`block_catalog` and :func:`spec_reference`, so the tool description, the
API and the frontend renderer can never drift on the accepted shape.

One generation of layouts: ``title``, ``section``, ``agenda``,
``content``, ``roadmap``, ``takeaways``, ``dashboard``, ``closing``. ``content``
is the workhorse: a slide is a list of typed **blocks** (text, list, callout,
metrics, table, chart, timeline, process flow, diagram, columns...), each with a
fixed prop set. This is the typed equivalent of the Filigran Presentations
framework's content blocks — with every "marker class" (``mt-auto``,
``callout-compact``, ``flex-1``...) turned into exactly one prop
(``spacer.grow``, ``density``, ``fill``), so the spec carries no class strings,
no raw HTML and no inline SVG. ``dashboard`` is the one layout built
for FIGURES: a KPI tile row over a 3-column grid of panels (two equal columns
with ``panel_columns``), each panel holding a native ``chart`` (drawn by
the renderer from series and categories - never a picture of a chart), a
``table`` or a metric.

The v1 layouts (``two_columns``, ``comparison``, ``generic`` and the v1
``dashboard`` - a metrics row over an HTML fragment) are RETIRED: no
model here, no component in the renderer, no entry in any catalog. A stored
deck that still carries one is upgraded to the slide that says the same thing
by :mod:`app.services.presentation_legacy` — on every read of the ``spec``
column and, in :func:`validate_spec`, before every write — so nothing
downstream meets a v1 slide. The v1 ``dashboard`` shares its id with the v2
layout and is told apart by SHAPE (it has no ``panels``); the two never meet
in one model. What survives of v1 is the *values* it
accepted: v1 let an optional string be ``""`` (its chapter dividers are title
slides with ``"subtitle": ""``), v2 requires a present string to be non-empty —
so :class:`_Strict` reads a blank optional string as *absent*. Without
that, every stored v1-era deck failed the re-validation behind a theme change,
a duplicate, a restore and a package import, on fields nobody touched.

Design constraints that shaped it
---------------------------------
* **Controlled layouts and blocks, not free-form HTML.** Each slide declares
  one of :data:`SLIDE_LAYOUTS` and each block one of :data:`BLOCK_TYPES`; every
  id maps to a dedicated React component with a fixed prop set. An agent
  therefore cannot take over the page. The free-form "arbitrary HTML in a
  sandboxed iframe" mode is explicitly out of scope, and no field can express it.
* **Inline markup instead of HTML** (``app.services.presentation_markup``):
  ``**bold**``, ``_em_``, ```` `code` ````, ``[[tone:text]]`` and line breaks —
  flat, escape-first, rendered to React nodes. A tag-like sequence is refused so
  the agent learns the markup rather than seeing ``<b>`` printed on the slide.
  A deck has NO HTML field: the v1 ``rich_text`` escape hatch retired with its
  layouts (its words are kept as plain ``text`` blocks by the upgrade), and the
  shared :mod:`app.services.rich_text_guard` now serves reports only.
* **Icons come from a generated allow-list** (``presentation_icons``, the
  installed ``lucide-react`` catalog). An unknown icon is a field-addressed
  error with close-match suggestions — never a silent blank box.
* **Fail loudly on malformed input.** Models forbid unknown fields: an LLM that
  invents ``{"layout": "title", "content": "..."}`` gets a precise error naming
  the offending key, which is what makes the retry productive.
* **Bounded.** Every collection has a cap, block nesting is capped at two
  container levels (``columns`` -> ``callout``/``comparison`` -> atomic blocks),
  so one tool call can neither blow up the JSON column nor produce a 400-slide
  deck nobody asked for.
* **Stable slide ids.** Each slide gets an ``id`` (server-assigned when the
  agent omits it) so the renderer keys slides by identity — an in-place edit
  that reorders slides no longer remounts every slide after the edit point —
  and so manual edits and collaborators can address one slide.
"""

from __future__ import annotations

import functools
import math
import re
import types
import typing
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Union

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from app.services.chart_export import (
    VALUE_LABEL_SIZE_MAX_PX,
    VALUE_LABEL_SIZE_MIN_PX,
    compact_tier,
    with_currency,
    with_unit,
)
from app.services.country_codes import ISO_3166_ALPHA2
from app.services.presentation_chrome import (
    MAX_DECK_FIELDS,
    MAX_FIELD_VALUE_CHARS,
    VARIABLE_KEY_RE,
    canonical_field_key,
)
from app.services.presentation_icons import normalize_icon_name, suggest_icon_names
from app.services.presentation_markup import MARKUP_SUMMARY, MARKUP_TONES, check_markup, markup_to_plain_text

# ── Catalog constants ────────────────────────────────────────────────────────

#: Visual themes — the COLOUR SCHEME a deck is read in, and nothing else.
#: These are OUR themes (rendered from platform design tokens in
#: ``frontend/src/components/presentation/presentation.css``), not reveal.js's
#: bundled stylesheets: a deck has to look like the product in both light and
#: dark mode, and reveal's themes hardcode their own palettes. The BRAND is the
#: other axis: a ``TemplateContainer`` lays its palette, fonts and logo
#: over whichever scheme the deck names, through its ``theme_config`` (plus a
#: ``theme_variants`` palette for the other scheme), resolved when the deck is
#: read — so a theme never carries a brand, and a brand never picks a scheme.
PRESENTATION_THEMES: tuple[str, ...] = ("dark", "light")

#: Theme names decks were stored with before the theme became a pure scheme,
#: mapped to the scheme they rendered as. ``filigran`` was a hard-coded copy of
#: the design-system dark tokens in the stylesheet; that palette now lives in
#: the Filigran Brand Deck template, which is what a brand template is for. A
#: stored spec that still says ``filigran`` validates as ``dark`` — no data
#: migration, no refused re-save of a deck nobody re-themed.
LEGACY_THEME_ALIASES: dict[str, str] = {"filigran": "dark"}


def normalize_theme(value: Any) -> Any:
    """``"filigran"`` → ``"dark"``, ``" Light "`` → ``"light"``; anything else untouched.

    Applied before validation (so a legacy value is accepted) and on every
    read of a stored theme (so no consumer — renderer, studio prompt, tool
    result — ever sees a name that is no longer a theme).
    """
    if isinstance(value, str):
        folded = value.strip().lower()
        return LEGACY_THEME_ALIASES.get(folded, folded if folded in PRESENTATION_THEMES else value)
    return value


#: reveal.js transition styles, passed straight through to the deck config.
PRESENTATION_TRANSITIONS: tuple[str, ...] = (
    "none",
    "fade",
    "slide",
    "convex",
    "concave",
    "zoom",
)

#: Deck formats. ``16:9`` and ``4:3`` are slides; ``A4`` and ``letter`` are
#: PORTRAIT PAGES — the one-pager / product-brief / datasheet family of
#: collateral. Same layouts, same blocks, same 1920px-wide authoring canvas,
#: only taller than wide (``CANVAS_SIZES_PX``), so the type scale a slide uses
#: reads as document body text once the page is printed at paper size. The
#: spellings are the CSS ``@page size`` keywords, accepted case-insensitively.
PRESENTATION_ASPECT_RATIOS: tuple[str, ...] = ("16:9", "4:3", "A4", "letter")
#: The subset that prints as a document rather than as slides.
DOCUMENT_ASPECT_RATIOS: tuple[str, ...] = ("A4", "letter")
#: The authoring canvas of each format in CSS pixels — the single source of
#: truth mirrored by ``geometry.ts`` (``DECK_SIZE``), the export renderer and
#: the PPTX writer. Portrait pages keep the 1920px width so every block keeps
#: the proportions it was designed with on a slide.
CANVAS_SIZES_PX: dict[str, tuple[int, int]] = {
    "16:9": (1920, 1080),
    "4:3": (1440, 1080),
    "A4": (1920, 2715),  # 210 x 297 mm
    "letter": (1920, 2485),  # 8.5 x 11 in
}
#: Paper each format prints on, in inches: PowerPoint's own 16:9 / 4:3 slide
#: sizes (so a PDF page and a PPTX slide of one deck are the same size), ISO
#: A4 and US Letter for the portrait pages.
PAGE_SIZES_IN: dict[str, tuple[float, float]] = {
    "16:9": (13.3333, 7.5),
    "4:3": (10.0, 7.5),
    "A4": (8.2677, 11.6929),
    "letter": (8.5, 11.0),
}
_ASPECT_RATIO_ALIASES: dict[str, str] = {ratio.lower(): ratio for ratio in PRESENTATION_ASPECT_RATIOS}


def normalize_aspect_ratio(value: Any) -> Any:
    """``"a4"`` → ``"A4"``, ``"Letter"`` → ``"letter"``; anything else untouched."""
    if isinstance(value, str):
        return _ASPECT_RATIO_ALIASES.get(value.strip().lower(), value)
    return value


def is_document_format(aspect_ratio: str | None) -> bool:
    """True for the portrait page formats (``A4`` / ``letter``)."""
    return normalize_aspect_ratio(aspect_ratio) in DOCUMENT_ASPECT_RATIOS


#: What a presentation IS to its reader — the two families of
#: ``PRESENTATION_ASPECT_RATIOS`` surfaced as a first-class attribute. A ``deck``
#: is *presented*: slides, transitions, fullscreen, speaker view. A ``page`` is
#: *read*: pages stacked and scrolled, zoomed, printed on real paper — a product
#: brief is not a slide show that happens to be tall. The API serializes it, the
#: list endpoint filters on it, and the frontend picks the viewer (deck stage vs
#: document view) from it. DERIVED from the aspect ratio rather than stored: the
#: spec already carries the format, and a second attribute that could disagree
#: with the first is a bug waiting to be filed.
PRESENTATION_KINDS: tuple[str, ...] = ("deck", "page")


def presentation_kind(aspect_ratio: str | None) -> str:
    """``"page"`` for the portrait formats (``A4`` / ``letter``), ``"deck"`` otherwise."""
    return "page" if is_document_format(aspect_ratio) else "deck"


def spec_kind(spec: dict | None) -> str:
    """:func:`presentation_kind` of a stored spec (``16:9`` when it names none)."""
    return presentation_kind((spec or {}).get("aspect_ratio"))


def unit_noun(spec: dict | None, count: int) -> str:
    """``"slide"`` / ``"page"`` (pluralised) — how a tool result names one unit.

    A page brief that reports "2 slides" tells the agent, and through it the
    user, that it is a slide show; the noun follows the kind everywhere the
    tools speak.
    """
    noun = "page" if spec_kind(spec) == "page" else "slide"
    return noun if count == 1 else f"{noun}s"


def slide_theme_override(slide: object) -> str | None:
    """The scheme ONE slide names for itself, ``None`` when it inherits.

    Read through :func:`normalize_theme` like the deck theme, so a stored
    ``filigran`` or a ``" Light "`` reads as the scheme it is; anything that is
    not a scheme reads as "inherits" rather than raising - this is a reader,
    the validator is what refuses.
    """
    if not isinstance(slide, dict):
        return None
    value = normalize_theme(slide.get("theme"))
    return value if value in PRESENTATION_THEMES else None


def resolved_slide_scheme(spec: dict | None, slide: object) -> str:
    """The scheme a slide is READ in: its own theme when it has one, else the deck's.

    The precedence rule in one place: ``slide.theme`` wins over
    ``spec.theme`` whenever it is set. Mirrored by ``resolveSlideScheme`` in
    ``frontend/src/components/presentation/theme.ts``, which every renderer
    (the reveal deck, the static export page, the thumbnails) resolves through.
    """
    own = slide_theme_override(slide)
    if own is not None:
        return own
    return "light" if normalize_theme((spec or {}).get("theme")) == "light" else "dark"


def logo_file_ids(spec: object) -> list[str]:
    """The file ids of every logo a ``logo_grid`` / ``logo_wall`` / ``formula`` / ``config_cards`` / ``timeline`` step / ``positioning_map`` item / ``architecture`` node or zone draws, in deck order.

    What the geometry of the deck's logos is read for (the trim box served as
    ``asset_meta``): an architecture lays its logos out by their ratio before
    their bytes arrive. A reader: anything that is not deck-shaped yields nothing.
    """
    found: list[str] = []

    def _items(value: object) -> list:
        return value if isinstance(value, list) else []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            kind = node.get("type")
            if kind == "logo_grid":
                logos = _items(node.get("items"))
            elif kind == "logo_wall":
                logos = [logo for group in _items(node.get("groups")) if isinstance(group, dict)
                         for logo in _items(group.get("logos"))]
            elif kind in ("timeline", "positioning_map"):
                logos = [item.get("logo_file_id") for item in _items(node.get("items")) if isinstance(item, dict)]
            elif kind == "formula":
                logos = [
                    logo
                    for term in _items(node.get("terms"))
                    if isinstance(term, dict)
                    for logo in (
                        term.get("logo_file_id"),
                        term.get("logo_light_file_id"),
                        *(
                            variant
                            for item in _items(term.get("logos"))
                            for variant in (
                                (item.get("file_id"), item.get("logo_light_file_id"))
                                if isinstance(item, dict)
                                else (item,)
                            )
                        ),
                    )
                ]
            elif kind == "config_cards":
                logos = [
                    logo
                    for card in _items(node.get("cards"))
                    if isinstance(card, dict)
                    for row in _items(card.get("rows"))
                    if isinstance(row, dict)
                    for logo in (row.get("logo_file_id"), row.get("logo_light_file_id"))
                ]
            elif kind == "architecture":
                logos = [
                    part.get("logo_file_id")
                    for part in (*_items(node.get("zones")), *_items(node.get("nodes")))
                    if isinstance(part, dict)
                ]
            else:
                logos = []
            for logo in logos:
                file_id = logo.get("file_id") if isinstance(logo, dict) else logo
                if isinstance(file_id, str) and file_id and file_id not in found:
                    found.append(file_id)
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(spec)
    return found


def slide_theme_overrides(spec: dict | None) -> list[tuple[int, str]]:
    """``(0-based index, scheme)`` of every slide that carries its own theme."""
    overrides: list[tuple[int, str]] = []
    for index, slide in enumerate((spec or {}).get("slides") or []):
        own = slide_theme_override(slide)
        if own is not None:
            overrides.append((index, own))
    return overrides


def whole_noun(spec: dict | None) -> str:
    """``"deck"`` / ``"presentation"`` — how a tool result names the whole item.

    The counterpart of :func:`unit_noun` for what is not one unit: a note with
    no anchor is about "the whole deck", or "the whole presentation" when the
    units are pages — never "deck" about a page.
    """
    return "deck" if spec_kind(spec) == "deck" else "presentation"


#: Layout catalog — THE list of layouts: what an agent may author, what
#: the editor's layout picker offers, what a template may advertise
#: and what the renderer draws. Each entry has a matching React component
#: (``SlideBody`` in ``frontend/src/components/presentation/layouts/``); a slide
#: whose layout the renderer does not know is drawn as a titled blank rather
#: than silently dropped, so the two lists are kept in sync deliberately. The
#: retired v1 layouts live in ``app.services.presentation_legacy.RETIRED_LAYOUTS``
#: and are upgraded, never listed. ``dashboard`` is the v2 layout of
#: that name - the v1 one is a retired SHAPE, recognised and upgraded by the
#: same module.
SLIDE_LAYOUTS: tuple[str, ...] = (
    "title",
    "section",
    "agenda",
    "content",
    "roadmap",
    "takeaways",
    "dashboard",
    "closing",
)

#: Block catalog. Each entry has a matching React component in
#: ``frontend/src/components/presentation/blocks/``; the vitest catalog
#: tripwire renders every one of them.
BLOCK_TYPES: tuple[str, ...] = (
    "text",
    "heading",
    "quote",
    "list",
    "callout",
    "metric",
    "metrics_grid",
    "table",
    "chart",
    "key_value",
    "badges",
    "feature_list",
    "phase_card",
    "numbered_cards",
    "pricing_tiers",
    "personas",
    "profiles",
    "timeline",
    "process_flow",
    "comparison",
    "section_header",
    "banner",
    "challenge_answer",
    "image",
    "video",
    "logo_grid",
    "logo_wall",
    "formula",
    "config_cards",
    "diagram",
    "architecture",
    "org_chart",
    "positioning_map",
    "poll",
    "columns",
    "spacer",
    "divider",
)

#: Blocks that hold other blocks. Nesting is capped by construction: a
#: ``columns`` block holds leaf blocks, a ``callout`` / ``comparison`` holds
#: atomic blocks, and atomic blocks hold nothing.
CONTAINER_BLOCK_TYPES: tuple[str, ...] = ("columns", "callout", "comparison")

#: Grid blocks that take an opt-in ``fill``: the block stretches to
#: the height its run leaves it and spreads its rows over it, instead of
#: packing them under the title and leaving the bottom of the slide bare.
#: Off by default, so a deck that never sets it renders as before. The
#: renderer emits ``data-fill`` on each block's root; the CSS hooks are per
#: root class in ``presentation.css``. The spread is BOUNDED: the
#: row-spreading roots stop once every gap has grown by a readable amount
#: (``spread.ts``), so three bullets never fill a slide - what is left stays
#: below the block and the layout check reports it. ``numbered_cards``
#: grows differently: its cards SHARE the height (the takeaways look) - the
#: cards grow, not the gaps - so its wording names the cards, never a gap
#: (:func:`fill_description`), or the agent-facing contract would promise a
#: gap the renderer does not keep; each row of cards is bounded too,
#: or one row of three takeaways becomes three near-empty pillars. The
#: ``table`` spreads its ROWS the same bounded way: each row grows by
#: at most the readable extra, the text centred in it, and what is left stays
#: below the footnote.
FILL_BLOCK_TYPES: tuple[str, ...] = (
    "list",
    "metrics_grid",
    "key_value",
    "feature_list",
    "numbered_cards",
    "personas",
    "table",
)
FILL_ROWS_DESCRIPTION = "spread the rows over the available height, up to a readable gap"
FILL_CARDS_DESCRIPTION = "stretch the cards to share the available height, up to a readable card height"
#: A lone card (callout, phase_card) is sized to its content: `fill`
#: lines it up with the cards beside it, never stretches it to the floor.
CARD_FILL_DESCRIPTION = "share the row's height with the cards beside it; never taller than its content alone"
#: Columns whose blocks line up as rows: the Nth block of every column
#: takes the height of the tallest one, so a 2 x 2 of phase cards is two rows
#: of equal cards. It takes over from `distribute` for those columns.
EQUAL_ROWS_DESCRIPTION = "the Nth block of every column shares one height (rows of equal cards)"


def fill_description(block: str) -> str:
    """The wording of ``fill`` on one grid block: the bounded row spread, or the
    bounded card stretch on ``numbered_cards`` - the one root whose cards, not
    its gaps, take the extra height (``measureCardRows`` in ``spread.ts``)."""
    return FILL_CARDS_DESCRIPTION if block == "numbered_cards" else FILL_ROWS_DESCRIPTION

#: Bounds. Generous enough for a real deck, small enough that a runaway
#: generation cannot produce an unusable artifact or an oversized JSON column.
#:
#: The slide cap is the TEMPLATE's to set (``extra_metadata.max_slides``,
#: read by ``presentations.template_slide_cap``): a board deck that follows the
#: layout rules (one dense dashboard split in two once its charts fall under
#: 160 px) outgrows a flat cap, and how long a deck may run is a property of
#: the kind of deck a template is for. ``DEFAULT_SLIDE_CAP`` holds for a free
#: deck and for a template that sets none; ``MAX_SLIDE_CAP`` is the ceiling no
#: template may raise - the bound the model enforces, past which the export's
#: render budget and the JSON column stop being credible.
DEFAULT_SLIDE_CAP = 100
MAX_SLIDE_CAP = 300
MAX_TITLE_CHARS = 300
MAX_TEXT_CHARS = 500
MAX_NOTES_CHARS = 2000
MAX_LABEL_CHARS = 120
MAX_MARKUP_CHARS = 1500
MAX_DIAGRAM_CHARS = 4000
MAX_BLOCKS_PER_SLIDE = 12
MAX_BLOCKS_PER_COLUMN = 8
MAX_BLOCKS_PER_CONTAINER = 6
MAX_COLUMNS = 4
MAX_LIST_ITEMS = 12
MAX_GRID_ITEMS = 8
MAX_TABLE_ROWS = 14
MAX_TABLE_COLUMNS = 6
MAX_KEY_VALUE_ITEMS = 10
MAX_BADGES = 10
MAX_PRICING_TIERS = 4
MAX_TIER_FEATURES = 8
MAX_PERSONAS = 4
MAX_PERSONA_POINTS = 4
MAX_BANNER_ACTIONS = 3
MAX_URL_CHARS = 300
MAX_FEATURE_ITEMS = 8
MAX_CARD_ITEMS = 6
MAX_TIMELINE_ITEMS = 8
MAX_FLOW_STEPS = 6
MAX_AGENDA_ITEMS = 10
#: The 3-column agenda grid - `N. | TITLE` cells with the description
#: under - needs two rows the items fill in FULL to read as a grid, and stops
#: reading as one past three rows: three columns are refused below six items
#: (`1 2 3 / 4 5` is a chip list with a hole, not a grid) and above nine (a 3 x 4
#: no board slide holds); ten items take two columns of five. Seven and eight
#: items leave the last row short - seven does in every column count, and eight
#: only fills up as four rows of two, one row more than the grid wants - so the
#: fewest rows wins there.
AGENDA_GRID_COLUMNS = 3
AGENDA_GRID_MIN_ITEMS = 6
AGENDA_GRID_MAX_ITEMS = 9
MAX_ROADMAP_PHASES = 4
MAX_TAKEAWAYS = 6
MAX_CONTACTS = 6
MAX_CREDENTIALS = 3
#: A title slide's speakers: a keynote's one, a co-presented talk's two, a
#: panel's three or four - a row of portraits under the headline holds four.
MAX_COVER_SPEAKERS = 4
MAX_ICON_STRIP = 6
MAX_LOGO_GRID_ITEMS = 16
#: The native ``logo_wall``: logos grouped by sector / category, one
#: card per group. Six cards is two rows of three on a 16:9 slide; twelve
#: logos is three rows of four inside a card - past that it is a second slide.
MAX_LOGO_WALL_GROUPS = 6
MAX_LOGO_WALL_GROUP_LOGOS = 12
#: ``formula``: 2-5 cards joined by operators fit one 16:9 row; a term's
#: logo row holds what a card's width shows legibly. ``config_cards``: 2-4
#: cards side by side, six rows each before a card outgrows half a slide.
MIN_FORMULA_TERMS = 2
MAX_FORMULA_TERMS = 5
MAX_FORMULA_TERM_LOGOS = 4
MIN_CONFIG_CARDS = 2
MAX_CONFIG_CARDS = 4
MAX_CONFIG_CARD_ROWS = 6
#: The visual height of one logo per ``logo_height`` step, in canvas px on the
#: 1920x1080 canvas. The height a WIDE logo is drawn at: optical
#: balance lets a squarer mark grow up to ``LOGO_BALANCE_MAX_BOOST`` times it,
#: never below it, and the cell width caps a wordmark too wide for its cell.
#: Mirrored by ``LOGO_HEIGHT_PX`` in ``components/presentation/logo-sizing.ts``.
LOGO_HEIGHT_PX: dict[str, int] = {"sm": 28, "md": 40, "lg": 56, "xl": 72}
#: ``tile_height``: the height of every cell of a ``logo_grid`` /
#: ``logo_wall`` in canvas px, independent of ``logo_height`` - sibling grids
#: with different logo mixes share one tile size, and the mark fits inside the
#: cell (a seal up to its full inner height). Bounded by what a slide holds: a
#: tile shorter than the smallest mark, or taller than a third of the canvas,
#: is a mistake. The inspector's number field repeats the bounds as literals
#: (``block-schema.ts``, pinned by ``tests/test_presentation_editor.py``).
LOGO_TILE_HEIGHT_MIN_PX = 32
LOGO_TILE_HEIGHT_MAX_PX = 400
#: The native ``architecture`` diagram: a grid of nodes, labeled edges
#: and zones drawn as vector graphics by the deck itself. Twelve nodes on a
#: 6x6 grid is already a busy slide; a label is a name, not a sentence.
MAX_ARCHITECTURE_NODES = 12
MAX_ARCHITECTURE_EDGES = 24
MAX_ARCHITECTURE_ZONES = 6
MAX_ARCHITECTURE_LEGEND_ITEMS = 6
MAX_ARCHITECTURE_GRID = 6
MAX_ARCHITECTURE_CHIPS = 4
#: A zone's chips are one row on its head row, never wrapped.
MAX_ARCHITECTURE_ZONE_CHIPS = 6
MAX_ARCHITECTURE_LABEL_CHARS = 40
#: The lanes of one pair of nodes: a pair holds one edge per direction,
#: so a handful of lane numbers is plenty to order them.
MAX_ARCHITECTURE_LANE = 6
MAX_ARCHITECTURE_CHIP_CHARS = 24
MAX_ARCHITECTURE_DESCRIPTION_CHARS = 240
#: The native ``org_chart`` block: reporting lines, teams and headcount as a
#: TREE of seats, drawn by the deck (a tidy tree, never a grid). A hundred and
#: twenty seats is a whole department with its teams folded into groups; past
#: it the chart is a second slide, or a group per team. A seat names a person
#: in one line and a role in another; a note is the tooltip, never drawn.
MAX_ORG_CHART_NODES = 120
#: How many seats one slide shows readably at once: past it, a chart that
#: folds nothing (no collapse_below, depth_limit or collapsed seat) is told to
#: by the layout check - the cap above is what the validator allows.
ORG_CHART_READABLE_NODES = 30
MAX_ORG_CHART_ROOTS = 4
MAX_ORG_CHART_CHIPS = 3
MAX_ORG_CHART_DOTTED = 3
MAX_ORG_CHART_DEPTH = 12
MAX_ORG_CHART_EXPAND = 40
MAX_ORG_CHART_COUNT = 100_000
MAX_ORG_CHART_LEGEND_ITEMS = 6
MAX_ORG_CHART_NAME_CHARS = 60
MAX_ORG_CHART_ROLE_CHARS = 80
MAX_ORG_CHART_CHIP_CHARS = 24
MAX_ORG_CHART_SHORT_CHARS = 24
MAX_ORG_CHART_LOCATION_CHARS = 40
MAX_ORG_CHART_NOTE_CHARS = 240
#: The native ``positioning_map`` block: items placed by x / y (0-100) on two
#: real axes. Thirty marks is a crowded landscape already; a label is a name.
MAX_POSITIONING_MAP_ITEMS = 30
MAX_POSITIONING_MAP_LABEL_CHARS = 40
MAX_POSITIONING_MAP_AXIS_CHARS = 60
#: What makes the highlighted item stand where it stands: up to four short
#: callouts drawn with the map, and one short note per item (shown on hover).
MAX_POSITIONING_MAP_CALLOUTS = 4
MAX_POSITIONING_MAP_CALLOUT_TEXT_CHARS = 160
MAX_POSITIONING_MAP_NOTE_CHARS = 160
#: The native ``chart`` block: series over categories, drawn by the
#: renderer. Six series is where a legend stops being readable on a slide and
#: sixteen categories is two years of months or four of quarters; a sparkline
#: is a shape, not a plot, so it gets two dozen points and no axis.
MAX_CHART_SERIES = 6
MAX_CHART_CATEGORIES = 16
#: A ``map`` names each country once, so its categories run to every
#: ISO 3166-1 code: a world choropleth of customers is read as a whole, not
#: category by category, and is not held to an axis' sixteen.
MAX_MAP_CATEGORIES = len(ISO_3166_ALPHA2)
MAX_CHART_SERIES_NAME_CHARS = 40
MAX_CHART_CATEGORY_CHARS = 24
MAX_CHART_UNIT_CHARS = 8
MAX_CHART_GOAL_LABEL_CHARS = 24
MAX_SPARKLINE_POINTS = 24
#: A ``waterfall`` holds every total to the running sum of the steps
#: before it. The slack is a share of the MOVEMENT since the previous total
#: (the sizes of those steps added up), never of the level reached: 0.5% of a
#: 19M ending ARR is 0.10M, enough to swallow the churned logo a board member
#: catches by adding the bars up, while 0.5% of the 5M the steps moved is the
#: rounding of figures written for a slide. The cap keeps it a rounding
#: allowance; a bigger miss is fixed, or drawn as ``WATERFALL_GAP_LABEL``.
DEFAULT_WATERFALL_TOLERANCE = 0.5
MAX_WATERFALL_TOLERANCE = 5
WATERFALL_GAP_LABEL = "Unexplained"
#: The ``dashboard`` layout: a KPI row of 2-8 tiles over a 3-column
#: grid of 1-6 panels. Six is the grid's two rows; a panel's ``span`` counts
#: against the same six cells, so a deck cannot ask for a third row the
#: canvas has no height for. ``panel_columns`` draws the grid on two
#: equal columns instead - the half-and-half split no span of three columns
#: makes - which holds four single panels on the same two rows.
MAX_DASHBOARD_TILES = 8
MIN_DASHBOARD_TILES = 2
MAX_DASHBOARD_PANELS = 6
DASHBOARD_GRID_COLUMNS = 3
DashboardPanelColumns = Literal[2, 3]
DASHBOARD_PANEL_COLUMNS: tuple[int, ...] = typing.get_args(DashboardPanelColumns)
assert DASHBOARD_GRID_COLUMNS in DASHBOARD_PANEL_COLUMNS  # noqa: S101 - import-time contract
DASHBOARD_GRID_ROWS = 2
#: The ``profiles`` block: the founders / speakers / leadership card.
#: Four people side by side is the widest a 16:9 slide reads, so twelve is
#: three rows of four (a 3 x 3 team is one block, never three stacked ones);
#: five one-line facts per person is a biography, not a card; a fact is one
#: line, so it is bounded like a label.
MAX_PROFILES = 12
MAX_PROFILE_LINES = 5
MAX_PROFILE_LINE_CHARS = 140
#: The ``quote`` block: up to four figures under the credit - the
#: "74K employees / $32.7B revenue" row of a customer proof slide; more is a
#: metrics slide. A ``hero`` quote steps down one size of the type scale rather
#: than wrap past three lines (``fitHeroQuote`` in ``blocks/quote-style.ts``), and a statement past four
#: lines at its size is a layout finding (``presentation_layout.py``): a
#: statement is read in one breath.
MAX_QUOTE_FIGURES = 4
HERO_QUOTE_MAX_LINES = 3
STATEMENT_MAX_LINES = 4


# ── Shared enums ─────────────────────────────────────────────────────────────
#
# Every framework "marker class" becomes exactly one typed prop. These aliases
# are referenced by name in ``spec_reference()`` so the tool description lists
# each value set once.

Tone = Literal["accent", "info", "success", "warning", "error", "muted"]
Density = Literal["normal", "compact", "tight"]
Size = Literal["sm", "md", "lg"]
MediaSize = Literal["sm", "md", "lg", "full"]
# Inner padding of an ``image`` block: `none` (the picture fills its
# box, the default and the historical look) or one of the surface paddings, so
# a picture drawn on a light card is never glued to the card's edge.
Padding = Literal["none", "sm", "md", "lg"]
# The showcase frame of an ``image`` block: `none` (the bare picture,
# the default and the historical look), `browser` (a window chrome - a title
# bar with three dots and an address pill - around a product screenshot),
# `browser_slim` (the same window with a bar a third as tall and no address
# pill, for several screenshots side by side) or `device` (a thin bezel with a
# camera dot). All sit on a soft glow in the
# template's brand gradient and hug the PICTURE at its ratio, never a
# letterboxed box: with ``fill`` the window is the largest box of that ratio
# the room holds. Mirrored by ``IMAGE_FRAMES`` in ``block-schema.ts`` and
# ``DeckImageFrame`` in ``types/index.ts``.
ImageFrame = Literal["none", "browser", "browser_slim", "device"]
IMAGE_FRAMES: tuple[str, ...] = typing.get_args(ImageFrame)
SpacerSize = Literal["xs", "sm", "md", "lg", "xl"]
Align = Literal["start", "center", "end"]
VAlign = Literal["start", "center", "end", "stretch"]
ListStyle = Literal["bullet", "check", "arrow", "cross", "numbered", "plain"]
Ratio = Literal["equal", "wide_left", "wide_right"]
Trend = Literal["up", "down", "flat"]
# Whether a KPI's change is good news. `trend` is the direction and
# colours the delta by it (up green, down red), which reads a fall in a loss, a
# burn or churn as bad news; `delta_tone` names the news itself and colours the
# delta and its arrow, the arrow still pointing the way `trend` says.
DeltaTone = Literal["success", "error", "warning", "muted"]
# The type size of a dashboard's tile row. `md` is the row as it always
# drew; `sm` is the step a 7- or 8-tile row takes (value 36px, tighter padding,
# a smaller sparkline) with the label, sublabel and delta at 16px, so 5-6 tiles
# keep a comparison note on one line. Not an alias: two values read inline.
DashboardTileSize = Literal["sm", "md"]
DASHBOARD_TILE_SIZES: tuple[str, ...] = typing.get_args(DashboardTileSize)
TimelineStyle = Literal["phases", "events"]
FlowDirection = Literal["horizontal", "vertical"]
ContactKind = Literal["web", "email", "linkedin", "x", "github", "phone"]
BadgeVariant = Literal["outline", "solid"]
# `soft` is the quiet band (tinted surface, regular text) for a page that already
# opens on a gradient hero — two gradient bands on one A4 page is a wall of colour.
BannerVariant = Literal["gradient", "solid", "soft", "outline"]
# The native architecture diagram. A `circle` is the hub the rest runs
# on, a `pill` a compact band or actor; `core` is the one emphasis the renderer
# draws larger and glowing, `ghost` the context a slide mentions but is not
# about. Edge routing is a hint, not a promise: `auto` goes straight to and from
# a core node and takes orthogonal elbows everywhere else.
NodeShape = Literal["card", "pill", "circle"]
NodeEmphasis = Literal["normal", "core", "ghost"]
EdgeArrows = Literal["end", "both", "none"]
EdgeRoute = Literal["auto", "straight", "orthogonal", "curve"]
# The side of a node (or of a zone's panel) an edge leaves or enters:
# a pinned side is honoured even when it faces away from the other end.
EdgeSide = Literal["top", "right", "bottom", "left"]
LegendMarker = Literal["arrow", "line", "dashed", "node"]
# A colour of the deck's template palette, named by its `theme_config`
# key: a brand hue the template declares (`brand_1`..`brand_4`, the product
# colours on the built-in brand decks) or one of its chart series. An
# architecture node or zone wears it in place of a tone, for what no tone
# means - a product box in its product's colour. Mirrors
# `BRAND_COLOR_THEME_CONFIG_KEYS` + `CHART_THEME_CONFIG_KEYS` in
# `presentations.py` (an import-time contract there).
PaletteColor = Literal[
    "brand_1",
    "brand_2",
    "brand_3",
    "brand_4",
    "chart_1",
    "chart_2",
    "chart_3",
    "chart_4",
    "chart_5",
    "chart_6",
]
PALETTE_COLORS: tuple[str, ...] = typing.get_args(PaletteColor)
# The native positioning map (a competitive landscape, a 2x2): two real
# axes crossing at the centre (`center`) or along the left and bottom edges
# (`edges`); an item is `highlight` (the one the slide is about: larger, in
# the accent, glowing), `normal`, or `ghost` (context, dimmed).
MapAxes = Literal["center", "edges"]
MapEmphasis = Literal["normal", "highlight", "ghost"]
MapCalloutsPosition = Literal["below", "right"]
# The native org chart. Each KIND of seat has one meaning deck-wide and its
# own card: `person` a real seat (a photo or initials, the name, the role),
# `vacant` a seat open today (dashed, warning, "Open"), `planned` a future
# hire (accent, the period it is planned for, `count` > 1 as one "+N" card),
# `group` a team with no individual drawn (its headcount, optionally its
# leader's name), `bubble` a team drawn as a circle whose AREA is its
# headcount, `external` a contractor, partner or agency seat (muted, dotted).
OrgNodeKind = Literal["person", "vacant", "planned", "group", "bubble", "external"]
ORG_NODE_KINDS: tuple[str, ...] = typing.get_args(OrgNodeKind)
# How the tree is laid: levels top to bottom, left to right, or rings around
# the root (a leadership wheel). `style` draws the seats: `cards` each kind's
# card, `compact` smaller cards on a tighter tree, `bubbles` every seat a
# circle (a team's AREA its headcount), `hybrid` the people as cards over
# their teams as bubbles.
OrgOrientation = Literal["top_down", "left_right", "radial"]
OrgChartStyle = Literal["cards", "compact", "bubbles", "hybrid"]
# What a roll-up counts: the seats filled today, the target once every open
# and planned seat is filled, or both ("14 -> 47").
OrgCountMode = Literal["current", "target", "both"]
OrgLegend = Literal["auto", "none", "custom"]
# The vertical column a run of leaves folds into under one rail: when a level
# is wider than the room (`auto`), always, or never.
OrgSpine = Literal["auto", "always", "never"]
# The agenda's two looks: `list` is the numbered chip in front of the
# title, `grid` the board-deck cell - `N. | TITLE` in accent caps with a thin
# separator bar and the description under it. `flow` is the fill order of the
# grid: `row` reads 1 2 3 / 4 5 6 like the PPTX, `column` runs down the first
# column first. Neither is registered as an alias: two values read better inline.
AgendaStyle = Literal["list", "grid"]
AgendaFlow = Literal["row", "column"]
# The native chart. `stacked_bar` is `bar` + `stacked`, kept as its own
# kind because that is the name the agent reaches for; `combo` mixes bars and
# lines, each series saying which through `ChartSeries.kind`;
# `gauge` reads ONE value against its `target`; `waterfall` is a bridge
# - one series of changes, the `totals` drawn from zero, each total held to the
# running sum before it; `butterfly` mirrors TWO series about a central
# spine - the first drawn leftwards, the second rightwards, both read as
# positive sizes (a population pyramid); `map` colours the countries
# its categories name - ISO 3166-1 alpha-2 codes - by one series, `MapRegion`
# the part of the world it is zoomed on. `ValueFormat` decides how a
# number is written everywhere a chart writes one (axis, label, tooltip, the
# PowerPoint number format of the exported chart): `currency` compacts to
# `16.5M` / `527.1k` with the `unit` as the sign, `percent` appends `%`.
# A bar, line or combo chart reads a second unit against a right-hand value
# axis: a series on `axis` `secondary` is written in `secondary_format`
# / `secondary_unit`, and that axis takes its own reference line
# (`secondary_target`) - a count beside a price, millions beside a ratio.
# `ChartDecimals` is how many decimals every value of an axis is
# written with (`decimals`, `secondary_decimals`); unset, a value under 10
# keeps two significant digits and no two values share a label
# (`chartValueWriter` in chart-format.ts, `run_decimals` in chart_export.py).
# `ChartUnitScale` is the one compact letter every currency amount of
# an axis is written in (`unit_scale`, `secondary_unit_scale`); `auto`, the
# same as unset, compacts each amount by its own size (`pinnedTier` in
# chart-format.ts, `pinned_tier` in chart_export.py).
ChartKind = Literal[
    "bar", "stacked_bar", "line", "area", "pie", "donut", "gauge", "combo", "waterfall", "butterfly", "map"
]
MapRegion = Literal["world", "europe", "americas", "apac"]
MAP_REGIONS: tuple[str, ...] = typing.get_args(MapRegion)
# How a map colours its countries and draws its legend: `ramp` (the
# same as unset) is the continuous ramp from the lowest value to the highest;
# `classes` cuts the distinct values by natural breaks into flat steps of that
# ramp, listed beside the plot. `MapClassCount` forces the number of classes;
# unset, a classed map draws up to `MAP_DEFAULT_CLASSES` (`mapClasses` in
# chart-format.ts, never more classes than distinct values).
MapLegendStyle = Literal["ramp", "classes"]
MAP_LEGEND_STYLES: tuple[str, ...] = typing.get_args(MapLegendStyle)
MapClassCount = Literal[2, 3, 4, 5, 6, 7]
MAP_DEFAULT_CLASSES = 5
SeriesKind = Literal["bar", "line"]
# Where a butterfly writes its category labels: `center` (unset) in a
# column on the spine between its two sides, `left` on the left axis with the
# two sides meeting at the spine. Not an alias: two values read inline.
ButterflySpine = Literal["center", "left"]
BUTTERFLY_SPINES: tuple[str, ...] = typing.get_args(ButterflySpine)
SeriesAxis = Literal["primary", "secondary"]
ValueFormat = Literal["number", "currency", "percent"]
ChartDecimals = Literal[0, 1, 2, 3]
ChartUnitScale = Literal["auto", "k", "M", "B"]
# Where a chart's legend sits. `top` is the historical placement - on
# the panel title's row in a dashboard panel, above the plot on a slide - and
# what an unset `legend_position` draws; `bottom` is one centred line under the
# x-axis labels, the plot taking the height freed at the top; `right` a column
# beside the plot. The PPTX writer puts PowerPoint's legend on the same side.
LegendPosition = Literal["top", "bottom", "right"]
LEGEND_POSITIONS: tuple[str, ...] = typing.get_args(LegendPosition)
# How the values of the bars are fitted when they do not fit at the size
# asked. `auto` is the historical rule and what an
# unset `value_label_fit` draws: set smaller, then turned on their side.
# `stagger` keeps the values' first size (`value_label_size`, else
# `label_size`) - above upright bars on two alternating rows; on a horizontal
# chart a value in the way of another moves along its row past it and
# a segment's value too long for its segment is written past the stack with a
# leader; `upright` keeps it in place - above upright bars, one row higher
# where that row is clear - and leaves out a value that still
# touches another or that its segment cannot hold. Both let the bars give way
# first, except a group of upright bars: it keeps the room `auto`
# gives it, its values overhanging the bars beside them and clearing only one
# another. Bar, stacked bar and combo charts, upright or horizontal; the PPTX
# writer draws the same bars and labels.
ValueLabelFit = Literal["auto", "stagger", "upright"]
# The size the values on an axis chart are tried at first, the ticks
# and the category labels staying at `label_size`: a text scale's values (`sm`
# = 18px down to 10, `CHART_TEXT_SCALES`), or a whole number of canvas px,
# tried at that size and then 2px at a time down to 8px under it, never under
# the minimum (`chartValueScale` in chart-layout.ts). Unset, the values follow
# `label_size`.
ValueLabelSize = Size | Annotated[int, Field(ge=VALUE_LABEL_SIZE_MIN_PX, le=VALUE_LABEL_SIZE_MAX_PX)]
#: A gauge's dial diameter at each chart ``size``, in canvas px: the box it
#: draws - the half disc and the reading inside it - is centred in the chart's
#: canvas, on a slide and in a dashboard panel, never larger than the canvas
#: holds; ``fill`` draws the largest it holds. Mirrors ``GAUGE_DIAMETERS`` in
#: ``chart-gauge.ts`` (pinned by a test).
GAUGE_DIAMETERS: dict[str, int] = {"sm": 240, "md": 360, "lg": 480}
CHART_SIZE_DESCRIPTION = (
    "how tall the chart stands on a slide, full as fill; a gauge's dial diameter, "
    + " / ".join(f"{name} {px}" for name, px in GAUGE_DIAMETERS.items())
    + " px, centred on its slide or panel"
)
CHART_FILL_DESCRIPTION = "take all the room the slide leaves; a gauge's dial as large as its slide or panel holds"
#: Kinds that plot ONE series (a share of a whole, or one value on a dial).
SINGLE_SERIES_CHART_KINDS: frozenset[str] = frozenset({"pie", "donut", "gauge"})
#: Kinds drawn on a category axis, where `target` is a reference line.
AXIS_CHART_KINDS: frozenset[str] = frozenset({"bar", "stacked_bar", "line", "area", "combo", "waterfall"})
#: Kinds drawn from a central spine: two series, each read against its
#: own side, with no reference line and no value axis an author pins.
MIRRORED_CHART_KINDS: frozenset[str] = frozenset({"butterfly"})
#: How many series a butterfly plots: its left side and its right side.
BUTTERFLY_SERIES = 2
#: The fields only a waterfall reads.
WATERFALL_FIELDS: tuple[str, ...] = ("totals", "tolerance", "allow_gap")
#: Kinds drawn on the world's countries: one series, a country code per category.
MAP_CHART_KINDS: frozenset[str] = frozenset({"map"})
#: Kinds whose series may read against the right-hand value axis.
SECONDARY_AXIS_CHART_KINDS: frozenset[str] = frozenset({"bar", "line", "combo"})
# Per-slide theme: the colour scheme ONE slide is read in, whatever the
# deck says. Absent (the default) inherits the deck theme; a value always wins
# over it, and only an explicit null / "Inherit" gives the slide back to the
# deck. A slide theme is held to the deck's template like the deck theme is.
SlideTheme = Literal["dark", "light"]
# The hero variants of the opening and closing slides. All of them are
# scheme-agnostic: they take their colours from the slide's RESOLVED scheme, so
# a dark hero in a light deck is `theme: "dark"` plus any hero on that slide.
# `mosaic` is a seeded pixel-block mosaic clustered in the corners the text
# leaves free, drawn from the template's six chart series for the `brand`
# tone and from the tone pair otherwise. `cascade`, `horizon` and `pillar` are
# the same tiles in other shapes - stepping down from the top-right corner, a
# skyline along the bottom edge, a column down the right edge - and the first
# two also lay the cover out around them: the logo large in the top corner,
# the headline low over a rule and the speaker / date row (`cascade`).
Hero = Literal["plain", "gradient", "split", "spotlight", "mosaic", "cascade", "horizon", "pillar"]
HERO_VARIANTS: tuple[str, ...] = typing.get_args(Hero)
# The covers every deck may wear, whatever its template.
SHARED_HEROES: tuple[str, ...] = ("plain", "gradient", "split", "spotlight")
# The tiled covers are a brand's own artwork (the THREAD marketing kit's pixel
# tiles): a deck wears one only under a template that declares
# it in `extra_metadata.heroes` (`presentations.template_heroes`).
TEMPLATE_HEROES: tuple[str, ...] = tuple(hero for hero in HERO_VARIANTS if hero not in SHARED_HEROES)
# The colour a hero is drawn in, read off the slide's palette so a
# cover stays on brand whatever the template: `brand` is the template's own
# gradient (`gradient_from` -> `gradient_to`, the default); every other value
# is ONE palette colour used alone, named by its `theme_config` key or its
# tone - so a brand whose gradient ends in a secondary colour can still open
# on its primary alone (`accent`, or `gradient_from`).
HeroTone = Literal[
    "brand", "accent", "gradient_from", "gradient_to", "info", "success", "warning", "error", "muted"
]
HERO_TONES: tuple[str, ...] = typing.get_args(HeroTone)
# The scrim drawn between a slide's background image and its content,
# in the slide's scheme: nothing, a soft wash, or a strong one.
Overlay = Literal["none", "soft", "strong"]
OVERLAY_VARIANTS: tuple[str, ...] = typing.get_args(Overlay)
#: A slide's own palette inside its template (spec
#: "PPT Design System" 3.2): ``xtm_one`` draws the slide's accent, block
#: gradients and chart series from the XTM One palette (Violet, Light violet,
#: Medium violet, Sky Blue, Aqua) for a slide about XTM One. ``None`` (the
#: default) keeps the template palette, byte for byte. Mirrored by
#: ``DeckSlidePalette`` in ``types/index.ts``.
SlidePalette = Literal["xtm_one"]
SLIDE_PALETTES: tuple[str, ...] = typing.get_args(SlidePalette)
#: Bento content slide (spec 3.7): one large image cell plus 2 to 4
#: text or KPI cells. The blocks ARE the cells; the first image is the large one.
BENTO_MIN_CELLS = 3
BENTO_MAX_CELLS = 5
BENTO_MAX_IMAGES = 3
# Logo walls. `LogoHeight` is the visual height of one logo
# (`LOGO_HEIGHT_PX`); `LogoTone` how it is inked - its own colours, or every
# logo in the text colour / the muted colour through a mask of its alpha (the
# "trusted by" look, no clash of thirty brand colours), or `ink`: every logo
# recoloured by its luminance - dark pixels in the text colour, light ones
# transparent - so the inner detail of a badge stays legible; `LogoAlign` how the
# cells of a row share its width - `even` gives every cell the same width and
# centres its logo in it (a grid under a timeline lines up with the steps),
# `stretch` spreads cells that hug their logos edge to edge (`space-between`),
# `start` / `center` pack them - `start` with `columns` in the row's equal
# cells, packed from the start, so a short row lines up with its siblings'
# columns. Unset, a grid with `columns` is `even` and one without is
# `stretch` (`logoGridAlign` in `logo-sizing.ts`).
LogoHeight = Literal["sm", "md", "lg", "xl"]
LogoTone = Literal["original", "mono", "muted", "ink"]
LogoAlign = Literal["start", "center", "even", "stretch"]
# The callout's two looks. `CalloutVariant` is the BOX - a tone wash
# with a bar (`tinted`, the historical look), a card on the surface token, a
# line only, or no box at all - and never the colour: the tone colours the icon
# and the title whatever the box. `CalloutHeader` places the icon and the
# title: on one line (`inline`) or the icon above the title (`stacked`), the
# rest of the callout at its full width under them; `none` draws no icon.
CalloutVariant = Literal["tinted", "card", "outline", "plain"]
CalloutHeader = Literal["inline", "stacked", "none"]
# The looks of a `quote`. `card` is the historical rendering and the
# default (a surface with an accent left bar, italic text); `statement` is a
# quote that IS the slide (no surface, heading font, bold, centred, one
# oversized accent quote mark); `pull` sits beside other content (a large
# accent left rule, heading-size text); `testimonial` is a card whose credit
# row carries a portrait or a company logo.
QuoteVariant = Literal["card", "statement", "pull", "testimonial"]
QUOTE_VARIANTS: tuple[str, ...] = typing.get_args(QuoteVariant)
# Steps of the deck type scale (`presentation.css`): `md` is the card's
# historical size, `hero` the title-slide headline - which steps down once
# rather than wrap past `HERO_QUOTE_MAX_LINES`.
QuoteSize = Literal["md", "lg", "xl", "hero"]
QUOTE_SIZES: tuple[str, ...] = typing.get_args(QuoteSize)
# `inline` = curly marks around the text; `decorative` = one oversized glyph in
# the accent tone, above a centred quote or hanging left of a start-aligned one.
QuoteMarks = Literal["none", "inline", "decorative"]
QUOTE_MARK_STYLES: tuple[str, ...] = typing.get_args(QuoteMarks)
# A KPI tile's look: the card (a hairline surface lit by its tone along
# the top edge, the value in its tone), or `plain` - no surface, no border, no
# light; the value in its tone and the label.
MetricVariant = Literal["card", "plain"]
METRIC_VARIANTS: tuple[str, ...] = typing.get_args(MetricVariant)

_ENUM_ALIASES: dict[str, Any] = {
    "SlideTheme": SlideTheme,
    "Hero": Hero,
    "HeroTone": HeroTone,
    "Overlay": Overlay,
    "QuoteVariant": QuoteVariant,
    "QuoteSize": QuoteSize,
    "QuoteMarks": QuoteMarks,
    "MetricVariant": MetricVariant,
    "Tone": Tone,
    "Density": Density,
    "Size": Size,
    "MediaSize": MediaSize,
    "Padding": Padding,
    "ImageFrame": ImageFrame,
    "SpacerSize": SpacerSize,
    "Align": Align,
    "VAlign": VAlign,
    "ListStyle": ListStyle,
    "Ratio": Ratio,
    "Trend": Trend,
    "DeltaTone": DeltaTone,
    "TimelineStyle": TimelineStyle,
    "FlowDirection": FlowDirection,
    "ContactKind": ContactKind,
    "BadgeVariant": BadgeVariant,
    "BannerVariant": BannerVariant,
    "NodeShape": NodeShape,
    "NodeEmphasis": NodeEmphasis,
    "EdgeArrows": EdgeArrows,
    "EdgeRoute": EdgeRoute,
    "EdgeSide": EdgeSide,
    "LegendMarker": LegendMarker,
    "PaletteColor": PaletteColor,
    "MapAxes": MapAxes,
    "MapEmphasis": MapEmphasis,
    "OrgNodeKind": OrgNodeKind,
    "OrgOrientation": OrgOrientation,
    "OrgChartStyle": OrgChartStyle,
    "OrgCountMode": OrgCountMode,
    "OrgLegend": OrgLegend,
    "OrgSpine": OrgSpine,
    "ChartKind": ChartKind,
    "MapRegion": MapRegion,
    "MapLegendStyle": MapLegendStyle,
    "SeriesKind": SeriesKind,
    "SeriesAxis": SeriesAxis,
    "ValueFormat": ValueFormat,
    "LogoHeight": LogoHeight,
    "LogoTone": LogoTone,
    "LogoAlign": LogoAlign,
    "CalloutVariant": CalloutVariant,
    "CalloutHeader": CalloutHeader,
}
assert set(LOGO_HEIGHT_PX) == set(typing.get_args(LogoHeight))  # noqa: S101 - import-time contract
assert tuple(typing.get_args(Tone)) == MARKUP_TONES  # noqa: S101 - import-time contract
assert set(typing.get_args(ChartKind)) == (  # noqa: S101 - import-time contract
    SINGLE_SERIES_CHART_KINDS | AXIS_CHART_KINDS | MIRRORED_CHART_KINDS | MAP_CHART_KINDS
)
assert SECONDARY_AXIS_CHART_KINDS <= AXIS_CHART_KINDS  # noqa: S101 - import-time contract


# ── Building blocks ──────────────────────────────────────────────────────────


def _admits_none(annotation: Any) -> bool:
    """``X | None`` (possibly wrapped in ``Annotated``) — a field that may be absent."""
    while typing.get_origin(annotation) is Annotated:
        annotation = typing.get_args(annotation)[0]
    origin = typing.get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        return type(None) in typing.get_args(annotation)
    return annotation is type(None)


@functools.cache
def _optional_field_names(model: type[BaseModel]) -> frozenset[str]:
    return frozenset(name for name, info in model.model_fields.items() if _admits_none(info.annotation))


class _Strict(BaseModel):
    """Base for every spec model: unknown keys are an error, not a shrug."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @model_validator(mode="before")
    @classmethod
    def _blank_optional_strings_are_absent(cls, data: Any) -> Any:
        """``"subtitle": ""`` means "no subtitle" — the same thing as leaving it out.

        v1 typed its optional strings as ``str | None`` with only a ceiling, so
        every stored v1 chapter divider is a title slide carrying
        ``"subtitle": ""``; v2 requires a present string to be non-empty. Read
        blank (or whitespace-only) values of *optional* fields as ``None`` before
        the field validators run, so those decks keep validating and an
        agent that writes ``""`` for a field it has nothing to say in — a common
        habit — does not pay a failed round-trip for it either. Required strings
        are untouched and still refuse a blank; the normalised spec that gets
        persisted simply omits the field.
        """
        if not isinstance(data, dict):
            return data
        blanks = [
            name for name in _optional_field_names(cls) if isinstance(data.get(name), str) and not data[name].strip()
        ]
        if not blanks:
            return data
        cleaned = dict(data)
        for name in blanks:
            cleaned[name] = None
        return cleaned


def check_icon(value: str) -> str:
    """Canonicalise a Lucide icon name or refuse it with suggestions."""
    canonical = normalize_icon_name(value)
    if canonical is None:
        hint = suggest_icon_names(value)
        suggestion = f" Did you mean: {', '.join(hint)}?" if hint else ""
        raise ValueError(
            f"unknown icon {value!r}: use a Lucide icon name such as "
            f"shield-check, rocket, users, chart-line or target.{suggestion}"
        )
    return canonical


#: What a banner action may link to: an ``https://`` page or a ``mailto:``
#: address — the two things a printed call to action ever points at. No
#: ``javascript:``, no ``data:``, no plain ``http://`` (a printed link that
#: downgrades the reader is not one worth shipping).
_ACTION_URL_RE = re.compile(
    r"^(?:https://[^\s<>\"'`]+|mailto:[^\s<>\"'`@]+@[^\s<>\"'`@]+\.[^\s<>\"'`@]+)$",
    re.IGNORECASE,
)


def check_action_url(value: str) -> str:
    """Accept an ``https://`` URL or a ``mailto:`` address, refuse the rest."""
    if not _ACTION_URL_RE.match(value):
        raise ValueError("must be an https:// URL or a mailto: address")
    return value


def _markup(max_length: int, *, min_length: int = 1) -> Any:
    return Annotated[
        str,
        Field(min_length=min_length, max_length=max_length),
        AfterValidator(check_markup),
    ]


Title = Annotated[str, Field(min_length=1, max_length=MAX_TITLE_CHARS)]
#: A slide image is a reference to an existing ``FileAttachment`` (a template
#: asset, or an artifact the agent generated earlier) — never inline
#: bytes and never an external URL, so nothing in a deck can trigger an
#: outbound request when it renders.
FileId = Annotated[str | None, Field(default=None, max_length=36)]
RequiredFileId = Annotated[str, Field(min_length=1, max_length=36)]
#: Inline-markup strings (see module docstring) in three sizes.
Label = _markup(MAX_LABEL_CHARS)
Markup = _markup(MAX_TEXT_CHARS)
LongMarkup = _markup(MAX_MARKUP_CHARS)
TitleMarkup = _markup(MAX_TITLE_CHARS)
CellMarkup = _markup(MAX_TEXT_CHARS, min_length=0)
Icon = Annotated[str, Field(min_length=1, max_length=60), AfterValidator(check_icon)]
#: The grammar of a slide id. URL-fragment safe by construction: a slide's
#: address is ``/presentations/<deck>#/<id>``, so the deck marker
#: (``channels/base.py``) and the frontend's link parser spell this same class.
SLIDE_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
SlideId = Annotated[str, Field(pattern=SLIDE_ID_PATTERN)]


def _coerce_text_items(field: str):
    """``["a", {"text": "b"}]`` -> objects, so the stored spec has one shape."""

    def _coerce(value: object) -> object:
        if isinstance(value, list):
            return [{field: item} if isinstance(item, str) else item for item in value]
        return value

    return _coerce


def _coerce_cell(item: object) -> object:
    """A table cell written as a scalar becomes the object form.

    ``True`` / ``False`` are the feature-matrix shorthand — a check or a cross
    in the success / error tone, the way every "Community vs Enterprise" grid
    in the sales collateral reads — and ``None`` is an empty cell. Strings keep
    their existing coercion.
    """
    if item is True:
        return {"icon": "check", "tone": "success"}
    if item is False:
        return {"icon": "x", "tone": "error"}
    if item is None:
        return {"text": ""}
    if isinstance(item, str):
        return {"text": item}
    return item


def _coerce_rows(value: object) -> object:
    if isinstance(value, list):
        return [[_coerce_cell(cell) for cell in row] if isinstance(row, list) else row for row in value]
    return value


# ── Blocks: atomic ───────────────────────────────────────────────────────────


class TextBlock(_Strict):
    """A paragraph of inline-markup text."""

    type: Literal["text"]
    content: LongMarkup
    size: Size = "md"
    align: Align = "start"
    tone: Tone | None = None


class HeadingBlock(_Strict):
    """An in-slide heading (the slide title is a layout field, not a block)."""

    type: Literal["heading"]
    content: Markup
    level: Literal[2, 3, 4] = 3
    icon: Icon | None = None
    align: Align = "start"
    tone: Tone | None = None


#: What a list or feature_list row's picture (``logo_file_id``) is described as.
ROW_LOGO_DESCRIPTION = (
    "a platform file (an SVG flag, a small logo) drawn in place of the row's marker or icon, "
    "at the height of the text's first line (list) or of the icon tile (feature_list), its aspect "
    "ratio kept and centred on the first line; one or the other with icon"
)
#: What ``logo_border`` does.
ROW_LOGO_BORDER_DESCRIPTION = "a 1 px hairline around the picture, so a white flag or logo reads on a white card"


def _row_logo_checks(icon: str | None, logo_file_id: str | None, logo_border: bool) -> None:
    """The rules a list or feature_list row's picture follows, in one place."""
    if icon and logo_file_id:
        # One medium per row: a second would validate and change nothing.
        raise ValueError("give a row an icon OR a logo_file_id, not both: the logo takes the icon's place.")
    if logo_border and not logo_file_id:
        raise ValueError("logo_border frames a row's picture - set logo_file_id, or leave logo_border out.")


class ListItem(_Strict):
    text: Markup
    #: Colours this item's marker + text; overrides the list style's default.
    tone: Tone | None = None
    #: Per-item icon, replacing the style marker for this item only.
    icon: Icon | None = None
    logo_file_id: FileId = Field(default=None, description=ROW_LOGO_DESCRIPTION)
    logo_border: bool = Field(default=False, description=ROW_LOGO_BORDER_DESCRIPTION)

    @model_validator(mode="after")
    def _one_marker(self) -> ListItem:
        _row_logo_checks(self.icon, self.logo_file_id, self.logo_border)
        return self


class ListBlock(_Strict):
    type: Literal["list"]
    items: Annotated[
        list[ListItem],
        BeforeValidator(_coerce_text_items("text")),
        Field(min_length=1, max_length=MAX_LIST_ITEMS),
    ]
    style: ListStyle = Field(
        default="bullet",
        description="check = green ticks, arrow = accent arrows, cross = red crosses",
    )
    density: Density = "normal"
    columns: Literal[1, 2] = 1
    size: Size = "md"
    fill: bool = Field(default=False, description=FILL_ROWS_DESCRIPTION)


#: A plotted number. Finite by construction: ``NaN`` / ``Infinity`` are valid
#: JSON to Python but not to the renderer, the PowerPoint chart or a person.
Number = Annotated[float, Field(allow_inf_nan=False)]


class MetricItem(_Strict):
    """One KPI tile (standalone ``metric`` block, a ``metrics_grid`` cell or a
    ``dashboard`` tile). ``sparkline`` is the tile's recent history,
    drawn as a small line beside the value - a shape, not a plot: no axis, no
    labels, 2-24 points."""

    value: Annotated[str, Field(min_length=1, max_length=40)]
    label: Label
    sublabel: Markup | None = None
    icon: Icon | None = None
    tone: Tone = "accent"
    delta: str | None = Field(default=None, max_length=50)
    trend: Trend | None = None
    delta_tone: DeltaTone | None = Field(default=None, description="colours the delta and its arrow; unset = by trend")
    sparkline: Annotated[list[Number], Field(min_length=2, max_length=MAX_SPARKLINE_POINTS)] | None = Field(
        default=None, description="recent values drawn as a small line beside the value (oldest first)"
    )


#: The two looks of a KPI tile, worded once for both blocks. A plain
#: tile draws its value and label and nothing else (``tileAdornments`` in
#: ``blocks/metric-look.ts``): the icon, sparkline, sublabel and delta are the
#: card's, kept in the spec for it like a slide field that acts on another form.
METRIC_VARIANT_DESCRIPTION = "plain = unboxed, value + label only"
#: A grid's ``size`` sets its tiles' type and padding on top of the density.
#: The renderer draws a size the same on every slide: a body shrunk to fit
#: keeps the grid at its size and shrinks the rest (``fit.ts``).
METRICS_GRID_SIZE_DESCRIPTION = "tile type + padding, the same on every slide: sm ~70% of md, lg ~125%"


class MetricBlock(MetricItem):
    type: Literal["metric"]
    variant: MetricVariant = Field(default="card", description=METRIC_VARIANT_DESCRIPTION)
    align: Literal["start", "center"] = "start"


class MetricsGridBlock(_Strict):
    type: Literal["metrics_grid"]
    items: Annotated[list[MetricItem], Field(min_length=1, max_length=MAX_GRID_ITEMS)]
    columns: Literal[2, 3, 4] | None = Field(
        default=None, description="omit for auto (min(items, 4))"
    )
    density: Density = "normal"
    size: Size = Field(default="md", description=METRICS_GRID_SIZE_DESCRIPTION)
    variant: MetricVariant = Field(default="card", description=METRIC_VARIANT_DESCRIPTION)
    align: Literal["start", "center"] = "start"
    fill: bool = Field(default=False, description=FILL_ROWS_DESCRIPTION)


#: What an omitted look field of a ``quote`` reads as, per variant. The
#: renderer resolves the same table (``QUOTE_VARIANT_DEFAULTS`` in
#: ``frontend/src/components/presentation/blocks/quote-style.ts``, pinned by
#: the test suite), so the look follows the variant: a card switched to a
#: statement becomes a centred bold statement, not a statement that kept the
#: card's size and italics. ``card`` is the historical rendering, byte for byte.
QUOTE_VARIANT_DEFAULTS: dict[str, dict[str, str | bool]] = {
    "card": {"size": "md", "align": "start", "italic": True, "quote_marks": "none"},
    "statement": {"size": "xl", "align": "center", "italic": False, "quote_marks": "decorative"},
    "pull": {"size": "lg", "align": "start", "italic": False, "quote_marks": "none"},
    "testimonial": {"size": "md", "align": "start", "italic": False, "quote_marks": "none"},
}
assert tuple(QUOTE_VARIANT_DEFAULTS) == QUOTE_VARIANTS  # noqa: S101 - import-time contract


def _spell(value: object) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def quote_variant_default_text(field: str) -> str:
    """``"statement xl, pull lg, otherwise md"``: what an omitted *field* reads as.

    Derived from :data:`QUOTE_VARIANT_DEFAULTS` so the reference the agent reads
    can never disagree with the renderer: the value most variants share is the
    ``otherwise``, the rest are named in catalog order.
    """
    values = [(variant, QUOTE_VARIANT_DEFAULTS[variant][field]) for variant in QUOTE_VARIANTS]
    counts: dict[str, int] = {}
    for _, value in values:
        counts[_spell(value)] = counts.get(_spell(value), 0) + 1
    usual = max(counts, key=lambda spelled: counts[spelled])
    named = [f"{variant} {_spell(value)}" for variant, value in values if _spell(value) != usual]
    return ", ".join([*named, f"otherwise {usual}"])


#: The split credit of a quote, in the order it is read.
QUOTE_CREDIT_FIELDS: tuple[str, ...] = ("author", "role", "organization")


class QuoteFigure(_Strict):
    """A figure under a quote's credit: the value, in its tone, over its
    label - drawn as a ``plain`` tile, which shows nothing else, so a figure has
    no field a card would draw (it would validate and show nothing)."""

    value: Annotated[str, Field(min_length=1, max_length=40)]
    label: Label
    tone: Tone = "accent"


class QuoteBlock(_Strict):
    """A quotation - a customer verbatim, a finding, the sentence a deck is built
    around. ``variant`` picks the look and every look field left out
    follows it (:data:`QUOTE_VARIANT_DEFAULTS`). The credit is ``author`` /
    ``role`` / ``organization``, drawn "Author, Role, Organization" with the
    author in medium weight, or the legacy one-line ``attribution`` - never
    both. A ``statement`` alone on a ``content`` slide is centred vertically in
    its body (``vertical_align: end`` still pins it to the bottom)."""

    type: Literal["quote"]
    content: LongMarkup
    # Read by the agent on every deck turn (invariant 16): the choice each
    # value makes and what an omitted look reads as, no more.
    variant: QuoteVariant = Field(
        default="card",
        description=(
            "statement = the quote is the slide (centred when alone on a content slide); "
            "pull = beside other content; testimonial = boxed, portrait / logo in the credit row"
        ),
    )
    size: QuoteSize | None = Field(
        default=None,
        description=(
            f"hero = the title headline, one step down past {HERO_QUOTE_MAX_LINES} lines; "
            f"default: {quote_variant_default_text('size')}"
        ),
    )
    align: Literal["start", "center"] | None = Field(
        default=None, description=f"default: {quote_variant_default_text('align')}"
    )
    italic: bool | None = Field(default=None, description=f"default: {quote_variant_default_text('italic')}")
    quote_marks: QuoteMarks | None = Field(
        default=None,
        description=f"decorative = one large accent mark; default: {quote_variant_default_text('quote_marks')}",
    )
    author: Label | None = None
    role: Label | None = None
    organization: Label | None = None
    attribution: Label | None = Field(default=None, description="legacy, never with author / role / organization")
    logo_file_id: FileId = Field(default=None, description="role=customer_logo / partner_logo")
    photo_file_id: FileId = Field(default=None, description="role=people_photo")
    figures: Annotated[list[QuoteFigure], Field(max_length=MAX_QUOTE_FIGURES)] | None = None

    @model_validator(mode="after")
    def _one_credit(self) -> QuoteBlock:
        split = [name for name in QUOTE_CREDIT_FIELDS if getattr(self, name)]
        if self.attribution and split:
            raise ValueError(
                "give the credit as attribution OR as author / role / organization, not both "
                f"(this quote sets attribution and {', '.join(split)}) - drop attribution"
            )
        return self


def quote_credit(block: Mapping[str, Any]) -> str | None:
    """The one line a quote is credited with, ``None`` when it is uncredited.

    The legacy ``attribution`` as written, or ``Author, Role, Organization`` -
    the split fields that are set, in that order, joined the way the renderer
    draws them (``quoteCredit`` in ``blocks/quote-style.ts``). Each part is
    inline markup, so the joined line is too.
    """
    legacy = block.get("attribution")
    if isinstance(legacy, str) and legacy.strip():
        return legacy
    parts = [block.get(name) for name in QUOTE_CREDIT_FIELDS]
    kept = [part for part in parts if isinstance(part, str) and part.strip()]
    return ", ".join(kept) or None


class TableHeader(_Strict):
    text: Label
    icon: Icon | None = None
    align: Align = "start"
    #: Tints the header cell AND its column — how a feature matrix singles out
    #: the edition being sold.
    tone: Tone | None = None


class TableCell(_Strict):
    text: CellMarkup = ""
    tone: Tone | None = None
    icon: Icon | None = None


#: A table's ``size`` steps its type along the deck scale and scales
#: its cell padding with it; ``density`` keeps setting the padding's base and
#: ``tight`` still steps the cell type down one. ``md`` is exactly the look a
#: table had before the field.
TABLE_SIZE_DESCRIPTION = "header, cell and footnote type one step down (sm) or up (lg), padding 80% / 125%"


class TableBlock(_Strict):
    """Rows x columns. A cell may be a string, ``true`` / ``false`` (check /
    cross — the feature-matrix shorthand) or ``null`` (empty)."""

    type: Literal["table"]
    headers: Annotated[
        list[TableHeader],
        BeforeValidator(_coerce_text_items("text")),
        Field(min_length=1, max_length=MAX_TABLE_COLUMNS),
    ]
    rows: Annotated[
        list[list[TableCell]],
        BeforeValidator(_coerce_rows),
        Field(min_length=1, max_length=MAX_TABLE_ROWS),
    ]
    density: Density = "normal"
    size: Size = Field(default="md", description=TABLE_SIZE_DESCRIPTION)
    highlight_first_column: bool = False
    footnote: Markup | None = Field(default=None, description="small print under the table")
    fill: bool = Field(default=False, description=FILL_ROWS_DESCRIPTION)

    @model_validator(mode="after")
    def _rows_match_headers(self) -> TableBlock:
        expected = len(self.headers)
        for index, row in enumerate(self.rows):
            if len(row) != expected:
                raise ValueError(
                    f"rows.{index} has {len(row)} cell(s) but the table has "
                    f"{expected} header(s) — give every row exactly one cell per "
                    "header (an empty string is fine)."
                )
        return self


def _coerce_category(value: object) -> object:
    """A category written as a number (``2024``, a quarter index) is its text."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return value


#: One axis label of a ``chart``: PLAIN text (no inline markup on an axis), 24
#: characters (``Q3 FY26``, ``6.2 -> 6.4``, ``Enterprise``). Longer names go in
#: the caption. Stripped by ``_Strict`` before the length check, so a blank
#: category is refused as too short.
Category = Annotated[
    str,
    BeforeValidator(_coerce_category),
    Field(min_length=1, max_length=MAX_CHART_CATEGORY_CHARS),
]


#: A total and its running sum closer than this share of the figures are
#: equal: ``16.5 + 1.8 + 1.3 - 0.4`` is ``19.200000000000003`` in floats.
_BRIDGE_FLOAT_SLACK = 1e-9


@dataclass(frozen=True)
class BridgeGap:
    """A ``waterfall`` total the steps before it do not add up to."""

    index: int
    total: float
    running: float
    #: The sizes of the steps since the previous total, added up.
    moved: float
    #: The previous total, the running sum's start; ``None`` when it started from zero.
    since: int | None = None

    @property
    def gap(self) -> float:
        return self.total - self.running


def waterfall_gaps(values: Sequence[float], totals: Iterable[int], tolerance: float) -> list[BridgeGap]:
    """The totals of a waterfall that miss the running sum of the steps before
    them by more than ``tolerance`` percent of those steps' movement.

    A total is the level from there on, so a miss is reported once and never
    carried into the next total; the first category has nothing before it and
    is where the bridge starts. Two totals with no step between them have
    nothing to absorb a difference: they must be equal. ``waterfallSteps`` in
    the viewer's ``chart-format.ts`` draws the same misses as gap steps.
    """
    marked = set(totals)
    misses: list[BridgeGap] = []
    running = moved = 0.0
    since: int | None = None
    for index, value in enumerate(values):
        if index not in marked:
            running += value
            moved += abs(value)
            continue
        if index > 0:
            slack = _BRIDGE_FLOAT_SLACK * max(1.0, abs(value), abs(running))
            if abs(value - running) > tolerance / 100 * moved + slack:
                misses.append(BridgeGap(index=index, total=value, running=running, moved=moved, since=since))
        running, moved, since = value, 0.0, index
    return misses


def _gap_decimals(scaled_gap: float, cap: int) -> int:
    """The decimals that show a gap written at ``scaled_gap`` (two figures of it), at most ``cap``."""
    return min(cap, max(1, 1 - math.floor(math.log10(scaled_gap)))) if scaled_gap > 0 else 1


def _bridge_amounts(fmt: str, unit: str | None, miss: BridgeGap) -> tuple[str, str, str, str]:
    """The total, running sum, gap and movement of ``miss`` written the way the
    chart writes a value - a currency in ONE compact tier, so the gap reads
    against the figures (``€19.14M vs €19.2M, gap €0.06M``) - with the
    decimals that show the gap, which the slide's one decimal would round away.
    A gap too small for the figures' tier is written in its own (``€2.5B vs
    running sum €2.5B, gap €1``), never as ``€0B``."""
    divisor, letter = compact_tier(max(abs(miss.total), abs(miss.running))) if fmt == "currency" else (1.0, "")
    gap = abs(miss.gap)
    decimals = _gap_decimals(gap / divisor, 4)

    def write(value: float, divisor: float = divisor, letter: str = letter, decimals: int = decimals) -> str:
        scaled = round(abs(value) / divisor, decimals)
        amount = f"{scaled:,.{decimals}f}".rstrip("0").rstrip(".") + letter
        sign = "-" if value < 0 and scaled else ""
        if fmt == "percent":
            return f"{sign}{amount}%"
        return sign + (with_currency(amount, unit) if fmt == "currency" else with_unit(amount, unit))

    gap_text = write(gap)
    if round(gap / divisor, decimals) == 0:
        own_divisor, own_letter = compact_tier(gap) if fmt == "currency" else (1.0, "")
        gap_text = write(gap, own_divisor, own_letter, _gap_decimals(gap / own_divisor, 12))
    return write(miss.total), write(miss.running), gap_text, write(miss.moved)


class ChartSeries(_Strict):
    """One series of a ``chart``: a name and exactly one value per category."""

    name: Annotated[str, Field(min_length=1, max_length=MAX_CHART_SERIES_NAME_CHARS)]
    values: Annotated[list[Number], Field(min_length=1, max_length=MAX_MAP_CATEGORIES)]
    tone: Tone | None = Field(
        default=None,
        description="semantic colour (success / warning / error / ...); omit for the template's chart palette, in series order",
    )
    kind: SeriesKind | None = Field(
        default=None, description="combo charts only: draw this series as bars or as a line"
    )
    axis: SeriesAxis | None = Field(
        default=None,
        description="bar / line / combo charts: secondary reads this series against a right-hand axis written in "
        "secondary_format; omit for primary (the left-hand axis)",
    )
    show_values: bool | None = Field(
        default=None,
        description="false leaves this series' values to the tooltip; omit to follow the chart (bar / line / "
        "unstacked area / combo; on a stacked bar, its segments' values - the stack totals still count it)",
    )


#: The value-axis range fields of a ``chart``, by the axis they pin.
AXIS_RANGE_FIELDS: dict[str, tuple[str, str]] = {
    "primary": ("y_min", "y_max"),
    "secondary": ("secondary_y_min", "secondary_y_max"),
}


def _plain_number(value: float) -> str:
    """A number as an author typed it in a spec (``19,200,000``, ``1.27``)."""
    return f"{value:,.10g}"


class ChartBlock(_Strict):
    """A native chart: series over categories, drawn by the renderer
    in the template's palette and fonts, exported as an editable PowerPoint
    chart - never a picture. ``kind`` picks the figure; ``series`` and
    ``categories`` carry the numbers; ``unit`` / ``format`` say how a number is
    written wherever the chart writes one (``EUR 16.5M``, ``86%``, ``1,204``).
    ``target`` is a reference line on an axis chart and the maximum of a
    ``gauge``. Tones map to the deck's semantic colours; untoned series take
    the template's chart palette in order. A ``waterfall`` is a
    bridge: one series of changes, the ``totals`` drawn from zero, and every
    total held to the running sum of the steps before it.

    A ``butterfly`` mirrors exactly two series about a central spine,
    the categories its rungs from top to bottom: the first series drawn
    leftwards, the second rightwards, both given and written as positive
    sizes - a negative value is refused, never read as a side, and a 0 is a
    rung that side lacks, drawn with no bar and no value. ``spine`` puts
    the category labels in a column on the spine (``center``, unset) or on the
    left axis; ``symmetric`` (unset: true) draws both sides to one extent,
    ``false`` each to its own on the same scale; ``show_totals`` writes each
    side's total under its legend entry. It has no reference line and no
    value axis to pin, so ``target`` and ``y_min`` / ``y_max`` are refused.

    Two units on one figure: a series on ``axis`` ``secondary`` reads
    against a right-hand value axis that ``secondary_format`` /
    ``secondary_unit`` write the way ``format`` / ``unit`` write the left-hand
    one, with its own reference line, ``secondary_target``.

    ``unit_scale`` writes every currency amount of an axis chart in one
    compact letter (``EUR 0.25M`` beside ``EUR 2.50M``), ``secondary_unit_scale``
    those of the right-hand axis; unset or ``auto``, each amount is compacted
    by its own size.

    ``label_size`` sets the chart's text: its ticks and the largest
    size its values are written in (``CHART_TEXT_SCALES`` in
    ``chart_export.py``), ``sm`` one step down as on a dashboard card.
    ``value_label_fit`` says what the values of the bars do when that
    size does not fit: ``auto`` (unset) sets them smaller, then turns them;
    ``stagger`` keeps the size - on two alternating rows above upright bars,
    moved along their row and, for a segment too short for its value, past
    the stack on a horizontal chart; ``upright`` keeps it in place -
    above upright bars, one row higher where that row is clear -
    and leaves out, counted, a value that still touches another or that its
    segment cannot hold. A group of upright bars stays as tight as ``auto``
    draws it under both, a value free to overhang the bars beside it.
    Never materialised, so a deck saved before it is
    unchanged. ``value_label_size`` sizes the values of an axis
    chart apart from its ticks and category labels: another text scale's
    values, or a ladder down from a number of canvas px; unset (never
    materialised either), they follow ``label_size``.

    ``framed`` stands the chart on a card, as a dashboard panel does;
    off, it is drawn straight on the slide. A panel is its chart's card, so a
    chart in one draws no card of its own.

    A gauge's ``size`` is its dial's diameter (``GAUGE_DIAMETERS``), in a
    dashboard panel too, and ``fill`` the largest dial its canvas holds; the
    half disc and the reading inside it are centred in the canvas.

    ``y_min`` / ``y_max`` pin the value axis' range, the
    ``secondary_`` pair the right-hand one's - to part a line from the bars on
    purpose; a range keeps zero and every value its axis plots. A series'
    own ``show_values`` writes or leaves its values whatever the chart's says
    - on a stacked bar, its segments' values. A line's or an area's
    values with no room in one row over their points are written alternately
    above and below them, and a stacked segment too thin for its
    value has it written past the stack, after the total, in its series'
    colour with a leader, in the viewer and the exports alike.

    ``title`` names the figure at the top inside its frame, as a
    dashboard panel title does, the legend beside it on the same row;
    ``caption`` is the note under the plot. A panel titles its chart itself
    (``DashboardPanel.title``), so a chart in one is refused a title of its
    own: one title source per card.

    A ``map`` is a choropleth: its categories are ISO 3166-1 alpha-2
    country codes (``ISO_3166_ALPHA2``), each once, written upper-case, and
    its one series colours each country on a ramp from a wash to the series'
    colour, the countries without a value in the muted surface; ``region``
    zooms it on Europe, the Americas or Asia-Pacific. It has no value axis
    and no target. Its values are written on hover, and with ``show_values``
    on the countries large enough to hold one; its legend is the ramp. The
    PPTX carries it as a picture of the rendered map, its title, caption and
    legend as text - PowerPoint has no map chart that opens outside Office.
    ``legend_style`` ``classes`` cuts its values by natural breaks into
    at most five classes (``classes`` forces 2-7), each country filled with its
    class's flat step of the ramp, the legend a list of dots with their range
    beside the plot; ``markers`` false draws no dot over a country too small to
    see. Both are dropped on any other kind, like ``region``, and ``classes``
    on a map whose legend is the ramp; ``ramp`` is stored as unset, like a
    unit scale's ``auto``."""

    type: Literal["chart"]
    kind: ChartKind
    series: Annotated[list[ChartSeries], Field(min_length=1, max_length=MAX_CHART_SERIES)]
    categories: Annotated[
        list[Category],
        Field(
            min_length=1,
            max_length=MAX_MAP_CATEGORIES,
            description=f"one axis label per value of every series, at most {MAX_CHART_CATEGORIES}; a map's are "
            "ISO 3166-1 alpha-2 country codes (FR, US, GB), one per country",
        ),
    ]
    unit: Annotated[str, Field(min_length=1, max_length=MAX_CHART_UNIT_CHARS)] | None = Field(
        default=None,
        description='written with every value: a suffix for number ("h", "pts", "users"); the currency for '
        'currency ("EUR" by default, "USD", "$"); ignored for percent',
    )
    format: ValueFormat = Field(
        default="number",
        description="number = 1,204 (+ unit); currency = the raw amount written compact, EUR 16.5M / EUR 527.1k; "
        "percent = the value is already a percentage, 86 -> 86%",
    )
    decimals: ChartDecimals | None = Field(
        default=None,
        description="every value written with exactly this many decimals - labels, ticks, tooltips; of the compact "
        "amount for currency (2 -> EUR 19.14M); omit for 2 significant digits under 10",
    )
    unit_scale: ChartUnitScale | None = Field(
        default=None,
        description="currency on an axis chart: every amount - labels, ticks, tooltips, target - written in that one "
        "letter, with decimals (M + 2 -> EUR 0.25M next to EUR 2.50M, never EUR 250.00k); auto (the same as omitting "
        "it) compacts each amount by its own size. Ignored by pie / donut / gauge / map and for number / percent",
    )
    stacked: bool = Field(default=False, description="stack the series (bar / area); implied by kind stacked_bar")
    horizontal: bool = Field(default=False, description="bars left to right (bar / stacked_bar only)")
    show_values: bool = Field(
        default=True,
        description="write each value on its bar / point / slice; a line's or an area's values alternate above and "
        "below the points where one row has no room (a narrow panel); a stacked segment too thin for its value "
        "has it written past the stack, after the total, in its series' colour with a leader; a pie's or a donut's "
        "values sit outside the slices with a leader, smaller in a narrow panel, and inside the large slices when "
        "even the smallest size has no room outside; a map's on the countries large enough to hold one, every "
        "value on hover",
    )
    show_legend: bool | None = Field(
        default=None,
        description="omit for auto (shown when there is more than one series; a map's ramp or classes always)",
    )
    legend_position: LegendPosition | None = Field(
        default=None,
        description="where the legend sits; omit for top (today's placement: the panel title's row in a dashboard "
        "panel, above the plot on a slide; a butterfly's on a row of its own, each series over its side; a classed "
        "map's right). bottom = "
        "one centred line under the x-axis labels, the plot taking the height freed at the top - for a narrow panel "
        "whose legend wraps; right = a column beside the plot",
    )
    target: Number | None = Field(
        default=None, description="a reference line across an axis chart; the maximum of a gauge (100 for percent)"
    )
    goal_label: Annotated[str, Field(min_length=1, max_length=MAX_CHART_GOAL_LABEL_CHARS)] | None = Field(
        default=None, description='label of the target line / gauge maximum ("Plan", "Target")'
    )
    totals: Annotated[list[Annotated[int, Field(ge=0)]], Field(max_length=MAX_CHART_CATEGORIES)] | None = Field(
        default=None,
        description="waterfall only: indices of the categories drawn from zero (opening, subtotals, ending); "
        "every other value is a step from the running total",
    )
    tolerance: Annotated[float, Field(ge=0, le=MAX_WATERFALL_TOLERANCE, allow_inf_nan=False)] | None = Field(
        default=None,
        description="waterfall only: how far a total may miss the running sum, in % of the steps since the "
        f"previous total (default {DEFAULT_WATERFALL_TOLERANCE:g})",
    )
    allow_gap: bool | None = Field(
        default=None,
        description=f'waterfall only: draw a total\'s miss as an "{WATERFALL_GAP_LABEL}" step instead of refusing',
    )
    spine: ButterflySpine | None = Field(
        default=None,
        description="butterfly only: where the category labels sit - center (the same as omitting it) in a column "
        "on the spine between the two sides, left on the left axis with the two sides meeting at the spine",
    )
    symmetric: bool | None = Field(
        default=None,
        description="butterfly only: false ends each side at its own largest value (one scale still, so the bars "
        "compare; the spine moves off centre); omit for both sides drawn to the same extent",
    )
    show_totals: bool | None = Field(
        default=None, description="butterfly only: write each side's total under its legend entry"
    )
    region: MapRegion | None = Field(
        default=None,
        description="map only: the part of the world drawn - omit for world; europe, americas, apac (Asia-Pacific) "
        "zoom on it, its neighbours still drawn at the edges",
    )
    legend_style: MapLegendStyle | None = Field(
        default=None,
        description="map only: omit for ramp (each country on a continuous ramp, the legend the ramp); classes = the "
        f"values cut by natural breaks into at most {MAP_DEFAULT_CLASSES} classes, each country in its class's flat "
        "colour, the legend a list of dots with their range (43, 12 - 39, the top class 120 +) right of the plot "
        "unless legend_position places it",
    )
    classes: MapClassCount | None = Field(
        default=None,
        description=f"map with legend_style classes: how many classes, never more than its distinct values; omit for "
        f"up to {MAP_DEFAULT_CLASSES}",
    )
    markers: bool | None = Field(
        default=None,
        description="map only: false draws no dot over a country too small to see - it keeps its fill (a territory "
        "with no shape of its own on the map, RE or GF, keeps its dot); omit for true",
    )
    secondary_format: ValueFormat | None = Field(
        default=None,
        description="how the right-hand axis writes the series on axis secondary, as format does for the left-hand "
        "one; required with such a series",
    )
    secondary_unit: Annotated[str, Field(min_length=1, max_length=MAX_CHART_UNIT_CHARS)] | None = Field(
        default=None, description='the unit of the right-hand axis, as unit is of the left-hand one ("x", "EUR")'
    )
    secondary_decimals: ChartDecimals | None = Field(
        default=None, description="the decimals of the right-hand axis, as decimals is of the left-hand one (2 -> 1.27x)"
    )
    secondary_unit_scale: ChartUnitScale | None = Field(
        default=None,
        description="the unit scale of the right-hand axis, as unit_scale is of the left-hand one (with "
        "secondary_decimals)",
    )
    secondary_target: Number | None = Field(
        default=None, description="a reference line read against the right-hand axis"
    )
    y_min: Number | None = Field(default=None, description="the value axis' floor, zero or below; omit for the chart's own")
    y_max: Number | None = Field(
        default=None, description="the value axis' top - raise it to give a line room over the bars; omit for the chart's own"
    )
    secondary_y_min: Number | None = Field(
        default=None, description="as y_min, for the right-hand axis (below zero lifts its line over the bars)"
    )
    secondary_y_max: Number | None = Field(default=None, description="as y_max, for the right-hand axis")
    title: Label | None = Field(
        default=None,
        description="title drawn at the top inside the chart's frame (as a dashboard panel title), the legend "
        "beside it - the one message of the chart. Not in a dashboard panel: the panel's own title names its chart",
    )
    caption: Markup | None = Field(default=None, description="a note under the plot: the source, the unit, a reading")
    size: MediaSize = Field(default="md", description=CHART_SIZE_DESCRIPTION)
    label_size: Size = Field(
        default="md",
        description="how large the values and the axis text read: sm as on a dashboard card, lg from the back of "
        "the room",
    )
    value_label_fit: ValueLabelFit | None = Field(
        default=None,
        description="bar / stacked_bar / combo, upright or horizontal: what the values of the bars do when their "
        "size does not fit; omit for auto (set smaller, then turned on their side). stagger = every value at its "
        "size (value_label_size, else label_size): above upright bars on two alternating rows (grouped bars in a "
        "half-width column); on horizontal bars a value in the way of another moved along its row, a segment's "
        "value too long for its segment written past the stack with a leader (two share stacks in one column). "
        "upright = every value at that size in its place, one that would touch another or overflow its segment "
        "left out. Grouped upright bars keep their tight groups under both; a value may overhang the bars beside it",
    )
    value_label_size: ValueLabelSize | None = Field(
        default=None,
        description="bar / stacked_bar / line / area / combo / waterfall / butterfly: the size the values on the figure are "
        "tried at first, the axis text staying at label_size - sm / md / lg (that scale's values: sm 18 down to "
        f"10, md 22 down to 12) or a whole number of canvas px {VALUE_LABEL_SIZE_MIN_PX}-{VALUE_LABEL_SIZE_MAX_PX} "
        "(20 = 2px under md's 22, then 2px steps down to 8px under it); omit to follow label_size",
    )
    fill: bool = Field(default=False, description=CHART_FILL_DESCRIPTION)
    framed: bool = Field(
        default=True,
        description="draw the chart on a card (surface and hairline border); false draws it straight on the "
        "slide. Ignored in a dashboard panel, which is the chart's card",
    )

    @field_validator("unit_scale", "secondary_unit_scale")
    @classmethod
    def _auto_is_unset(cls, value: str | None) -> str | None:
        """``auto`` is what an unset scale draws, so it is stored as unset: a
        chart that names it saves, renders and exports as one that does not."""
        return None if value == "auto" else value

    @field_validator("legend_style")
    @classmethod
    def _ramp_is_unset(cls, value: str | None) -> str | None:
        """``ramp`` is what an unset map legend draws, stored as unset like ``auto``."""
        return None if value == "ramp" else value

    @field_validator("value_label_size", mode="before")
    @classmethod
    def _value_label_size_in_domain(cls, value: object) -> object:
        """A text scale or a whole number of canvas px in range, refused once
        and by name otherwise - never a bool, a fraction or a numeral read as
        a number."""
        in_range = (
            isinstance(value, int)
            and not isinstance(value, bool)
            and VALUE_LABEL_SIZE_MIN_PX <= value <= VALUE_LABEL_SIZE_MAX_PX
        )
        if value is None or in_range or (isinstance(value, str) and value in typing.get_args(Size)):
            return value
        raise ValueError(
            f"value_label_size takes sm, md or lg, or a whole number of canvas px from {VALUE_LABEL_SIZE_MIN_PX} "
            f"to {VALUE_LABEL_SIZE_MAX_PX}, not {value!r} - omit it to size the values with label_size."
        )

    @model_validator(mode="after")
    def _categories_within_the_cap(self) -> ChartBlock:
        """Sixteen categories on an axis, a pie or a dial; a map names each
        country once, up to every ISO 3166-1 code (``MAX_MAP_CATEGORIES``)."""
        count = len(self.categories)
        if self.kind not in MAP_CHART_KINDS and count > MAX_CHART_CATEGORIES:
            raise ValueError(
                f"a {self.kind} chart takes at most {MAX_CHART_CATEGORIES} categories, not {count} - group the "
                f"smallest ones or split the chart (a map takes one country code per category, up to "
                f"{MAX_MAP_CATEGORIES})."
            )
        return self

    @model_validator(mode="after")
    def _series_fit_the_categories(self) -> ChartBlock:
        expected = len(self.categories)
        for index, item in enumerate(self.series):
            if len(item.values) != expected:
                raise ValueError(
                    f"series.{index}.values has {len(item.values)} value(s) but the chart has "
                    f"{expected} categories (length mismatch) - give every series exactly one "
                    "value per category."
                )
        return self

    @model_validator(mode="after")
    def _kind_and_shape_agree(self) -> ChartBlock:
        kind = self.kind
        if kind in SINGLE_SERIES_CHART_KINDS and len(self.series) != 1:
            raise ValueError(
                f"a {kind} chart plots exactly one series (it shows a share of a whole), "
                f"not {len(self.series)} - keep one series or pick bar / line / area."
            )
        if kind in {"pie", "donut"}:
            negative = [value for value in self.series[0].values if value < 0]
            if negative:
                raise ValueError(f"a {kind} chart cannot show a negative share ({negative[0]:g}).")
        if kind in {"pie", "donut", "map"} and self.target is not None:
            raise ValueError(
                f"target is a reference line on an axis chart or the maximum of a gauge - "
                f"a {kind} has neither; drop target (and goal_label)."
            )
        if kind == "gauge":
            if len(self.categories) != 1:
                raise ValueError(
                    f"a gauge reads one value: give one category and one value, not {len(self.categories)}."
                )
            if self.target is None:
                if self.format != "percent":
                    raise ValueError(
                        "a gauge needs its maximum: set target (the value the dial fills up to), "
                        "or format percent for a 0-100 dial."
                    )
                self.target = 100.0
            elif self.target <= 0:
                raise ValueError("a gauge's target is its maximum and must be greater than zero.")
        if kind == "combo":
            drawn = {item.kind for item in self.series}
            if None in drawn:
                raise ValueError(
                    "a combo chart needs every series to say how it is drawn: set kind bar or line on each series."
                )
            if len(drawn) < 2:
                (only,) = drawn
                raise ValueError(
                    f"a combo chart mixes bars and lines; every series here is a {only} - use kind {only}."
                )
        else:
            with_kind = [index for index, item in enumerate(self.series) if item.kind is not None]
            if with_kind:
                raise ValueError(
                    f"series.{with_kind[0]}.kind only applies to a combo chart - "
                    "drop it, or make the chart a combo."
                )
        if kind in MIRRORED_CHART_KINDS:
            self._settle_the_butterfly()
        if kind == "waterfall":
            self._settle_the_bridge()
        else:
            # Dropped, not refused: a waterfall switched to another kind in the
            # inspector keeps the bridge it was saved with, and `totals` has no
            # control there - a refusal would leave the person no way to save.
            for name in WATERFALL_FIELDS:
                setattr(self, name, None)
        if kind in MAP_CHART_KINDS:
            self._read_the_countries()
            if self.legend_style != "classes":
                # A class count on a ramp counts nothing: dropped, so switching the legend back to the ramp saves.
                self.classes = None
        else:
            # Dropped, not refused, like the bridge: a map switched to another kind in the inspector still saves.
            self.region = None
            self.legend_style = None
            self.classes = None
            self.markers = None
        if kind == "stacked_bar":
            self.stacked = True
        elif kind not in {"bar", "area"} and self.stacked:
            raise ValueError(f"stacked only applies to bar / area charts, not {kind}.")
        if self.horizontal and kind in MIRRORED_CHART_KINDS:
            raise ValueError("horizontal is a bar chart's switch - a butterfly's bars always run sideways from its spine; drop horizontal.")
        if self.horizontal and kind not in {"bar", "stacked_bar"}:
            raise ValueError(f"horizontal only applies to bar / stacked_bar charts, not {kind}.")
        if self.goal_label is not None and self.target is None:
            raise ValueError("goal_label names the target: set target too, or drop the label.")
        return self

    def _settle_the_butterfly(self) -> None:
        """A butterfly's shape: one series per side, each value a size
        drawn away from the spine. The series order says the side, so a sign
        would say it twice - and would be written on every label it reaches:
        a negative value is refused by its field. A reference line has no
        side to stand on."""
        count = len(self.series)
        if count != BUTTERFLY_SERIES:
            entries = "entry" if count == 1 else "entries"
            raise ValueError(
                f"series has {count} {entries} but a butterfly plots exactly {BUTTERFLY_SERIES}: the first drawn "
                "leftwards from the spine, the second rightwards - keep two series, or pick bar / stacked_bar."
            )
        for index, item in enumerate(self.series):
            for category, value in enumerate(item.values):
                if value < 0:
                    raise ValueError(
                        f"series.{index}.values.{category} is {_plain_number(value)}: a butterfly reads both sides as "
                        f"positive sizes, the series order saying which side - write {_plain_number(-value)}."
                    )
        if self.target is not None:
            raise ValueError(
                "target is a reference line on an axis chart - a butterfly has none, each side reading from the "
                "spine; drop target (and goal_label)."
            )

    def _settle_the_bridge(self) -> None:
        """A waterfall's shape, its defaults made explicit, and the check a
        board member makes by adding the bars up: every total is the running
        sum of the steps before it, within ``tolerance``. A miss names the
        total, both figures and the gap - unless ``allow_gap`` draws it as a
        step of its own."""
        if len(self.series) != 1:
            raise ValueError(
                f"a waterfall plots one series of changes (the totals are the levels they reach), "
                f"not {len(self.series)} - keep one series, or pick bar / stacked_bar."
            )
        count = len(self.categories)
        outside = [index for index in self.totals or () if index >= count]
        if outside:
            raise ValueError(
                f"totals names category {outside[0]} but the chart has {count} categories (0-{count - 1})."
            )
        self.totals = sorted(set(self.totals or ()))
        if self.tolerance is None:
            self.tolerance = DEFAULT_WATERFALL_TOLERANCE
        if self.allow_gap is None:
            self.allow_gap = False
        if self.allow_gap:
            return
        misses = waterfall_gaps(self.series[0].values, self.totals, self.tolerance)
        if not misses:
            return
        found = []
        for miss in misses:
            total, running, gap, moved = _bridge_amounts(self.format, self.unit, miss)
            named = f'"{self.categories[miss.index]}" (categories.{miss.index}) {total}'
            if miss.moved:
                found.append(
                    f"{named} vs running sum {running}, gap {gap} "
                    f"(tolerance {self.tolerance:g}% of the {moved} the steps moved)"
                )
            elif miss.since is not None:
                found.append(
                    f'{named} vs the previous total "{self.categories[miss.since]}" (categories.{miss.since}) '
                    f"{running}, gap {gap} - no step between the two totals explains it"
                )
            else:
                found.append(f"{named} vs {running} before it, gap {gap} - no step before it explains it")
        raise ValueError(
            "a waterfall's totals must add up to the steps before them: "
            + "; ".join(found)
            + ". Fix the step or the total that is wrong, or set allow_gap: true to draw the gap as an "
            f'"{WATERFALL_GAP_LABEL}" step.'
        )

    def _read_the_countries(self) -> None:
        """A map's shape: one series, and categories that are ISO 3166-1
        alpha-2 codes, each once, stored upper-case. Every code that is not
        one is named with its index, all of them in one refusal."""
        if len(self.series) != 1:
            raise ValueError(
                f"a map colours each country by one series, not {len(self.series)} - keep one series of one "
                "value per country, or pick bar / stacked_bar."
            )
        unknown = [
            f'categories.{index} "{code}"'
            for index, code in enumerate(self.categories)
            if code.upper() not in ISO_3166_ALPHA2
        ]
        if unknown:
            verb = (
                "are not ISO 3166-1 alpha-2 country codes"
                if len(unknown) > 1
                else "is not an ISO 3166-1 alpha-2 country code"
            )
            raise ValueError(
                f"{', '.join(unknown)} {verb}: a map's categories are two-letter country codes "
                "(FR, US, GB, AU), one per country."
            )
        codes = [code.upper() for code in self.categories]
        first: dict[str, int] = {}
        for index, code in enumerate(codes):
            if code in first:
                raise ValueError(
                    f'categories.{index} "{code}" repeats categories.{first[code]}: a map takes one value per '
                    "country - add the two up, or keep one."
                )
            first[code] = index
        self.categories = codes

    @model_validator(mode="after")
    def _secondary_axis_reads(self) -> ChartBlock:
        kind = self.kind
        placed = [index for index, item in enumerate(self.series) if item.axis is not None]
        if placed and kind not in SECONDARY_AXIS_CHART_KINDS:
            raise ValueError(
                f"series.{placed[0]}.axis only applies to a bar, line or combo chart - "
                "drop it, or make the chart one of those."
            )
        secondary = [index for index, item in enumerate(self.series) if item.axis == "secondary"]
        if not secondary:
            for name in (
                "secondary_format",
                "secondary_unit",
                "secondary_decimals",
                "secondary_unit_scale",
                "secondary_target",
                *AXIS_RANGE_FIELDS["secondary"],
            ):
                if getattr(self, name) is not None:
                    raise ValueError(
                        f"{name} describes the right-hand axis, which only a series on axis secondary "
                        f"draws - put a series on axis secondary, or drop {name}."
                    )
            return self
        first = secondary[0]
        if len(secondary) == len(self.series):
            raise ValueError(
                f"series.{first}.axis secondary needs a primary series beside it: the right-hand axis "
                "reads a second unit next to the left-hand one - keep at least one series on the "
                "left-hand axis (omit axis there)."
            )
        if self.secondary_format is None:
            raise ValueError(
                f"series.{first}.axis secondary needs secondary_format or reads with format: set "
                "secondary_format to how the right-hand axis writes its numbers (number / currency / "
                "percent), or drop axis so the series reads with format on the left-hand axis."
            )
        if self.stacked:
            raise ValueError(
                f"series.{first}.axis secondary cannot join a stack: stacked adds the series up against "
                "one axis - drop stacked, or drop axis."
            )
        if self.horizontal:
            raise ValueError(
                f"series.{first}.axis secondary reads against a right-hand axis, which a horizontal "
                "chart does not have - drop horizontal, or drop axis."
            )
        return self

    @model_validator(mode="after")
    def _series_values_apply(self) -> ChartBlock:
        """A series' own ``show_values`` only where each series writes its own
        values: a stacked bar's segments do - its totals follow the
        chart - a stacked area writes its totals, a bridge, a share of a whole
        and a map their one series - the chart's ``show_values`` says it for them."""
        own = [index for index, item in enumerate(self.series) if item.show_values is not None]
        if not own:
            return self
        stacked_area = self.stacked and self.kind == "area"
        one_series = SINGLE_SERIES_CHART_KINDS | MAP_CHART_KINDS
        if self.kind in one_series or self.kind == "waterfall" or stacked_area:
            shape = "stacked area" if stacked_area else self.kind
            raise ValueError(
                f"series.{own[0]}.show_values only applies to a bar, stacked bar, line, unstacked area or combo "
                f"chart - a {shape} writes its values as one: drop it, and use show_values on the chart."
            )
        return self

    @model_validator(mode="after")
    def _axis_ranges_hold(self) -> ChartBlock:
        """``y_min`` / ``y_max`` (and the ``secondary_`` pair) pin a value
        axis' range: on an axis chart only, the low end under the high one, and
        never cutting off zero or a value the axis plots - a bar, a stack, a
        bridge level, a point, its target. The refusal names the value."""
        pinned = {
            name: getattr(self, name) for names in AXIS_RANGE_FIELDS.values() for name in names
        }
        if self.kind in SINGLE_SERIES_CHART_KINDS | MAP_CHART_KINDS:
            named = [name for name, value in pinned.items() if value is not None]
            if named:
                raise ValueError(
                    f"{named[0]} pins a value axis, which a {self.kind} chart does not have - drop {named[0]}."
                )
            return self
        if self.kind in MIRRORED_CHART_KINDS:
            named = [name for name, value in pinned.items() if value is not None]
            if named:
                raise ValueError(
                    f"{named[0]} pins one value axis, and a butterfly reads two sides from its spine - drop "
                    f"{named[0]} (symmetric false ends each side at its own largest value)."
                )
            return self
        two_axes = any(item.axis == "secondary" for item in self.series)
        for axis, (low_name, high_name) in AXIS_RANGE_FIELDS.items():
            low, high = pinned[low_name], pinned[high_name]
            if low is None and high is None:
                continue
            if low is not None and high is not None and low >= high:
                raise ValueError(
                    f"{low_name} {_plain_number(low)} must be under {high_name} {_plain_number(high)}."
                )
            named = (
                "the right-hand axis" if axis == "secondary" else "the left-hand axis" if two_axes else "the value axis"
            )
            (floor, floor_at), (ceiling, ceiling_at) = self._axis_reach(axis)
            if low is not None and low > floor:
                raise ValueError(
                    f"{low_name} {_plain_number(low)} is above {floor_at} ({_plain_number(floor)}): {named} keeps "
                    f"zero and every value it plots - set {low_name} to {_plain_number(floor)} or less, or drop it."
                )
            if high is not None and high < ceiling:
                raise ValueError(
                    f"{high_name} {_plain_number(high)} is under {ceiling_at} ({_plain_number(ceiling)}): {named} "
                    f"keeps zero and every value it plots - set {high_name} to {_plain_number(ceiling)} or more, "
                    "or drop it."
                )
        return self

    def _axis_reach(self, axis: str) -> tuple[tuple[float, str], tuple[float, str]]:
        """The lowest and the highest value one value axis must show, each with
        where it comes from: zero, a series' value (a stack's total, a bridge's
        level) or the axis' target - ``axisFloor`` / ``axisCeiling`` in
        ``chart-format.ts``, the target on both sides so its line is drawn."""
        two_axes = any(item.axis == "secondary" for item in self.series)
        reached: list[tuple[float, str]] = [(0.0, "zero")]
        indexed = [
            (index, item)
            for index, item in enumerate(self.series)
            if not two_axes or (item.axis == "secondary") == (axis == "secondary")
        ]
        if self.kind == "waterfall":
            totals = set(self.totals or ())
            running = 0.0
            for category, value in enumerate(self.series[0].values):
                where = f"the bridge level at categories.{category}"
                if category in totals:
                    reached.append((float(value), where))
                    running = float(value)
                    continue
                running += value
                reached.append((running, where))
        elif self.stacked:
            for category in range(len(self.categories)):
                column = [item.values[category] for _index, item in indexed]
                where = f"the stack of categories.{category}"
                reached.append((float(sum(v for v in column if v > 0)), where))
                reached.append((float(sum(v for v in column if v < 0)), where))
        else:
            for index, item in indexed:
                reached.extend((float(value), f"series.{index}.values.{category}") for category, value in enumerate(item.values))
        target = self.secondary_target if axis == "secondary" else self.target
        if target is not None:
            reached.append((float(target), "secondary_target" if axis == "secondary" else "target"))
        return min(reached, key=lambda entry: entry[0]), max(reached, key=lambda entry: entry[0])


class FeatureItem(_Strict):
    icon: Icon | None = Field(default=None, description="the row's icon tile; required unless logo_file_id is given")
    title: Markup
    description: Markup | None = None
    tone: Tone = "accent"
    logo_file_id: FileId = Field(default=None, description=ROW_LOGO_DESCRIPTION)
    logo_border: bool = Field(default=False, description=ROW_LOGO_BORDER_DESCRIPTION)

    @model_validator(mode="after")
    def _one_marker(self) -> FeatureItem:
        if not self.icon and not self.logo_file_id:
            raise ValueError("a feature needs an icon or a logo_file_id.")
        _row_logo_checks(self.icon, self.logo_file_id, self.logo_border)
        return self


class FeatureListBlock(_Strict):
    """Icon boxes: an icon tile with a title and a one-line description each."""

    type: Literal["feature_list"]
    items: Annotated[list[FeatureItem], Field(min_length=1, max_length=MAX_FEATURE_ITEMS)]
    columns: Literal[1, 2] = 1
    size: Size = "md"
    fill: bool = Field(default=False, description=FILL_ROWS_DESCRIPTION)


class Goal(_Strict):
    label: Label
    value: Markup


class Phase(_Strict):
    """A roadmap phase card (also the ``phase_card`` block)."""

    icon: Icon | None = None
    title: Markup
    subtitle: Markup | None = None
    items: Annotated[list[Markup], Field(default_factory=list, max_length=MAX_GRID_ITEMS)]
    goal: Goal | None = Field(default=None, description="highlighted target line")
    tone: Tone = "accent"


class PhaseCardBlock(Phase):
    type: Literal["phase_card"]
    fill: bool = Field(default=False, description=CARD_FILL_DESCRIPTION)


class NumberedCard(_Strict):
    number: Annotated[int, Field(ge=1, le=99)] | None = None
    title: Markup
    description: Markup | None = None
    icon: Icon | None = None


class NumberedCardsBlock(_Strict):
    """Principle / takeaway cards with a big number."""

    type: Literal["numbered_cards"]
    items: Annotated[
        list[NumberedCard],
        BeforeValidator(_coerce_text_items("title")),
        Field(min_length=1, max_length=MAX_CARD_ITEMS),
    ]
    columns: Literal[1, 2, 3] | None = Field(default=None, description="omit for auto")
    tone: Tone = "accent"
    fill: bool = Field(default=False, description=FILL_CARDS_DESCRIPTION)


class TimelineItem(_Strict):
    """One step. Its logo - the lead investor of a funding round, the partner of
    a phase - is drawn under the description, centred on the node and sized
    like a ``logo_grid``'s (trimmed mark, optical balance), so it can never
    drift from its step the way a separate grid under the timeline can."""

    marker: Annotated[str, Field(min_length=1, max_length=4)] | None = Field(
        default=None, description="1-4 chars shown in the node (default: index)"
    )
    label: Label | None = Field(default=None, description='e.g. "Q1 2026"')
    title: Markup
    description: Markup | None = None
    tone: Tone | None = None
    icon: Icon | None = None
    logo_file_id: FileId = Field(
        default=None, description="the step's logo (get_template role=partner_logo / customer_logo)"
    )
    logo_height: Size = Field(default="sm", description="sm 28 / md 40 / lg 56 px")
    framed: bool = Field(default=True, description="a tile behind the logo")


class TimelineBlock(_Strict):
    type: Literal["timeline"]
    items: Annotated[
        list[TimelineItem], Field(min_length=2, max_length=MAX_TIMELINE_ITEMS)
    ]
    style: TimelineStyle = Field(
        default="phases",
        description="phases = horizontal nodes, events = vertical dated list",
    )
    density: Density = "normal"
    fill: bool = False


class FlowStep(_Strict):
    icon: Icon | None = None
    title: Markup
    description: Markup | None = None
    tone: Tone | None = None


class ProcessFlowBlock(_Strict):
    """Steps joined by arrows; ``loop`` closes the last step back to the first."""

    type: Literal["process_flow"]
    steps: Annotated[list[FlowStep], Field(min_length=2, max_length=MAX_FLOW_STEPS)]
    direction: FlowDirection = "horizontal"
    loop: bool = False


class SectionHeaderBlock(_Strict):
    """A small header separating groups of blocks inside one slide."""

    type: Literal["section_header"]
    title: Markup
    icon: Icon | None = None
    tone: Tone = "accent"


class ImageBlock(_Strict):
    type: Literal["image"]
    file_id: RequiredFileId = Field(
        description="platform file id (template asset or generated file)"
    )
    alt: str | None = Field(default=None, max_length=MAX_TITLE_CHARS)
    fit: Literal["contain", "cover"] = "contain"
    size: MediaSize = "md"
    fill: bool = False
    align: Align = "center"
    padding: Padding = Field(
        default="none",
        description="inner padding around the picture: a picture on a card is never glued to its edge",
    )
    frame: ImageFrame = Field(
        default="none",
        description="showcase chrome around a product screenshot: browser = window bar, browser_slim = thin bar "
        "(several screenshots on one slide), device = bezel; all on a brand-gradient glow, hugging the picture at "
        "its ratio (with fill, the largest it can be in the room)",
    )
    caption: Markup | None = None


#: Seconds a ``video`` block may start or end at (a deck video is minutes long).
MAX_VIDEO_SECONDS = 4 * 60 * 60
VideoSeconds = Annotated[float, Field(ge=0, le=MAX_VIDEO_SECONDS, allow_inf_nan=False)]


#: The players a ``video`` block may embed: YouTube in its privacy-enhanced
#: mode and Vimeo. Any other host is a direct MP4 / WebM link, imported on save.
VIDEO_EMBED_HOSTS: dict[str, tuple[str, ...]] = {
    "youtube": (
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    ),
    "vimeo": ("vimeo.com", "www.vimeo.com", "player.vimeo.com"),
}
#: Where each provider's player is served from (the CSP ``frame-src`` of the app).
VIDEO_EMBED_ORIGINS: dict[str, str] = {
    "youtube": "https://www.youtube-nocookie.com",
    "vimeo": "https://player.vimeo.com",
}
_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_VIMEO_ID_RE = re.compile(r"^[0-9]{1,12}$")
MAX_VIDEO_URL_CHARS = 2048


def video_embed(url: str) -> tuple[str, str] | None:
    """``(provider, video id)`` of a YouTube or Vimeo link, ``None`` for any other host.

    Raises :class:`ValueError` for a link on a player host that names no video
    (a channel, a playlist, a search): it cannot be played and is not a file.
    """
    from urllib.parse import parse_qs, urlsplit

    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    provider = next((name for name, hosts in VIDEO_EMBED_HOSTS.items() if host in hosts), None)
    if provider is None:
        return None
    segments = [segment for segment in parts.path.split("/") if segment]
    candidate = ""
    if provider == "youtube":
        if host == "youtu.be":
            candidate = segments[0] if segments else ""
        elif segments[:1] == ["watch"]:
            candidate = (parse_qs(parts.query).get("v") or [""])[0]
        elif len(segments) >= 2 and segments[0] in ("embed", "shorts", "live", "v"):
            candidate = segments[1]
        if _YOUTUBE_ID_RE.match(candidate):
            return provider, candidate
        raise ValueError("is a YouTube link that names no video (use a watch, youtu.be, shorts or embed link)")
    if host == "player.vimeo.com":
        candidate = segments[1] if len(segments) >= 2 and segments[0] == "video" else ""
    else:
        candidate = next((segment for segment in segments if _VIMEO_ID_RE.match(segment)), "")
    if _VIMEO_ID_RE.match(candidate):
        return provider, candidate
    raise ValueError(
        "is a Vimeo link that names no video (use a vimeo.com link ending in the video's number, or a player link)"
    )


def check_video_url(value: str) -> str:
    """An ``https://`` link: a YouTube or Vimeo video, or a direct MP4 / WebM file."""
    from urllib.parse import urlsplit

    parts = urlsplit(value)
    if parts.scheme.lower() != "https" or not parts.hostname or any(ch.isspace() for ch in value):
        raise ValueError("must be an https:// link to a YouTube or Vimeo video, or to an MP4 / WebM file")
    video_embed(value)
    return value


class VideoBlock(_Strict):
    """A video that plays inside the slide: a Video Studio video, an MP4 / WebM
    file, or an external link (``url``).

    ``url`` on YouTube or Vimeo plays through the provider's own player
    (YouTube in its privacy-enhanced ``youtube-nocookie.com`` mode); the PDF
    and the PPTX show its poster with a link. Any other ``https://`` link must
    be an MP4 or WebM file: it is imported once on save as a platform file
    (``file_id``), so it plays, shares and exports like an uploaded file and
    never vanishes with the site that hosted it.

    ``video_id`` names a Video Studio video: on save the deck takes its current
    ready render (``file_id``), its poster (``poster_file_id``) and its WebVTT
    captions (``captions_file_id``), so the slide plays what the video plays
    today and every reader of the deck is served those files through the deck's
    own file grant (a public link included). A block that already carries its
    ``file_id`` keeps it; to take a newer render, insert the block again.
    """

    type: Literal["video"]
    video_id: FileId = Field(
        default=None,
        description="Video Studio video id: plays its current ready render, poster and captions (resolved on save)",
    )
    file_id: FileId = Field(
        default=None, description="platform MP4 / WebM file id; filled from video_id or an MP4 / WebM url on save"
    )
    url: str | None = Field(
        default=None,
        max_length=MAX_VIDEO_URL_CHARS,
        description="https link: a YouTube / Vimeo video (embedded player) or an MP4 / WebM file (imported on save)",
    )
    poster_file_id: FileId = Field(
        default=None, description="picture shown until it plays; default: the render's poster"
    )
    captions_file_id: FileId = Field(default=None, description="WebVTT track; filled from video_id on save")
    alt: str | None = Field(default=None, max_length=MAX_TITLE_CHARS)
    caption: Markup | None = None
    autoplay: bool = Field(default=False, description="starts when the slide is shown, pauses when it leaves")
    muted: bool | None = Field(default=None, description="default: muted when autoplay")
    loop: bool = False
    controls: bool = True
    captions: bool = Field(default=True, description="show the captions track when there is one")
    start: VideoSeconds | None = None
    end: VideoSeconds | None = None
    size: MediaSize = "md"
    fill: bool = False
    align: Align = "center"
    frame: ImageFrame = Field(
        default="none", description="showcase chrome as on image: browser, browser_slim, device, on a glow"
    )

    @model_validator(mode="after")
    def _source(self) -> VideoBlock:
        if not self.video_id and not self.file_id and not self.url:
            raise ValueError(
                "a video block needs video_id (a Video Studio video), file_id (an MP4 / WebM file) "
                "or url (a YouTube / Vimeo video, or an MP4 / WebM link)."
            )
        if self.url is not None:
            try:
                check_video_url(self.url)
            except ValueError as exc:
                raise ValueError(f"url {exc}") from None
            if self.video_id:
                raise ValueError("a video block plays one source: video_id or url, not both.")
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise ValueError(f"end ({self.end:g} s) must come after start ({self.start:g} s).")
        return self


class ChallengeAnswerBlock(_Strict):
    """A problem and its answer as two stacked cards joined by an arrow.

    The challenge card wears the ``error`` tone, the answer card ``success``:
    the two meanings every deck already maps those tones to (the "before" and
    the "after" of a fix). The cards share one width and one left edge, so the
    eye reads them as one statement rather than two notes; the arrow between
    them is the connector the keynote slide drew by hand. ``icon`` leads the
    challenge card (the subject), the answer card keeps its check.
    """

    type: Literal["challenge_answer"]
    challenge: LongMarkup = Field(description="the problem, in the audience's words")
    answer: LongMarkup = Field(description="what resolves it")
    icon: Icon | None = Field(default=None, description="leads the challenge card; the answer card keeps a check")
    challenge_label: Label = Field(default="Challenge", description="eyebrow of the top card")
    answer_label: Label = Field(default="Answer", description="eyebrow of the bottom card")
    fill: bool = Field(default=False, description="stretch the two cards to share the available height")


class LogoItem(_Strict):
    """One logo of a ``logo_grid`` / ``logo_wall``: a platform file, optionally labelled."""

    file_id: RequiredFileId = Field(
        description="platform file id of the logo (a template asset - get_template role=partner_logo / integration_logo / customer_logo)"
    )
    label: Label | None = Field(default=None, description="alt text; drawn under the logo with show_labels")
    scale: Annotated[float, Field(ge=0.5, le=1.5)] | None = Field(
        default=None, description="multiplies this logo's computed height"
    )


LOGO_HEIGHT_DESCRIPTION = "sm 28 / md 40 / lg 56 / xl 72 px"
OPTICAL_BALANCE_DESCRIPTION = (
    "size by trimmed shape and ink: seals and wordmarks read alike (in a tile_height cell a seal grows to its height)"
)
LOGO_TONE_DESCRIPTION = "mono = text colour, muted = muted colour, ink = recoloured by luminance, inner detail kept"
OPTICAL_WEIGHT_DESCRIPTION = "every logo at one visual weight (area x ink): wide wordmarks shrink, seals grow"
LOGO_TILE_HEIGHT_DESCRIPTION = (
    f"canvas px of every cell ({LOGO_TILE_HEIGHT_MIN_PX}-{LOGO_TILE_HEIGHT_MAX_PX}), independent of logo_height: "
    "sibling grids share one tile size; the mark fits inside, a seal up to the inner height"
)
LogoTileHeight = Annotated[int, Field(ge=LOGO_TILE_HEIGHT_MIN_PX, le=LOGO_TILE_HEIGHT_MAX_PX)]


class LogoGridBlock(_Strict):
    """A grid of logos referenced by file id (sized by the LOGO).

    The "trusted by" / "integrates with" / "our partners" slide. Every logo is
    drawn at ``logo_height`` (``size`` is its alias) measured on the logo's
    TRIMMED box - the transparent margins of the file are cut server-side when
    it is stored - and, with ``optical_balance``, weighted by its shape and
    ink so a seal does not read lighter than a wordmark. A row shares its width
    in equal cells, spreads over it or is packed (``align``), rows are
    ``columns`` logos long (auto: balanced rows of at most six) or ``rows`` in
    number, and ``fill`` spreads them over the height the container leaves.
    ``tile_height`` gives every cell one height in canvas px whatever
    it holds - the way sibling grids with different logo mixes share one tile
    size - and the mark fits inside it: a wordmark takes the cell's width, a
    seal grows up to its inner height, where the balance's 1.5x ceiling no
    longer applies (the tile is the bound). Up to
    :data:`MAX_LOGO_GRID_ITEMS` items, which may be written as bare file ids;
    they are normalised to objects so the stored spec has one shape, and every
    id is checked by ``verify_file_references`` like any other ``file_id``.
    """

    type: Literal["logo_grid"]
    items: Annotated[
        list[LogoItem],
        BeforeValidator(_coerce_text_items("file_id")),
        Field(min_length=1, max_length=MAX_LOGO_GRID_ITEMS),
    ]
    columns: Literal[2, 3, 4, 5, 6] | None = Field(
        default=None, description="logos per row; omit for auto (balanced rows of <= 6)"
    )
    size: Size = Field(default="md", description="alias of logo_height, used only while logo_height is unset")
    framed: bool = Field(default=True, description="a tile per logo, the whole cell when the cells are equal")
    caption: Markup | None = None
    logo_height: LogoHeight | None = Field(default=None, description=LOGO_HEIGHT_DESCRIPTION)
    optical_balance: bool = Field(default=True, description=OPTICAL_BALANCE_DESCRIPTION)
    optical_weight: bool | None = Field(default=None, description=OPTICAL_WEIGHT_DESCRIPTION)
    tile_height: LogoTileHeight | None = Field(default=None, description=LOGO_TILE_HEIGHT_DESCRIPTION)
    rows: Annotated[int, Field(ge=1, le=MAX_LOGO_GRID_ITEMS)] | None = Field(
        default=None, description="with columns: exactly this many rows of columns logos, the last may be short"
    )
    gap: Size = "md"
    align: LogoAlign | None = Field(
        default=None,
        description="even = equal cells (default with columns); stretch = edge to edge (default without); "
        "start = equal cells packed from the start with columns, packed logos without; center = packed",
    )
    fill: bool = Field(default=False, description="take the height left, rows spread over it")
    tone: LogoTone = Field(default="original", description=LOGO_TONE_DESCRIPTION)
    show_labels: bool = False

    @model_validator(mode="after")
    def _rows_hold_the_items(self) -> LogoGridBlock:
        count = len(self.items)
        if self.rows is None:
            return self
        if self.rows > count:
            raise ValueError(
                f"rows is {self.rows} but the grid holds {count} logo(s): every row needs at least one logo."
            )
        if self.columns is not None:
            cells = self.rows * self.columns
            if cells < count:
                raise ValueError(
                    f"rows x columns ({self.rows} x {self.columns} = {cells}) holds fewer cells than the "
                    f"{count} logos - raise one of them."
                )
            if (self.rows - 1) * self.columns >= count:
                raise ValueError(
                    f"rows x columns ({self.rows} x {self.columns}) leaves the last row empty for {count} "
                    f"logos - {-(-count // self.columns)} row(s) of {self.columns} hold them."
                )
        return self


class LogoWallGroup(_Strict):
    """One card of a ``logo_wall``: a sector or a category and its logos."""

    title: Label = Field(description='e.g. "Government"')
    icon: Icon | None = None
    tone: Tone = Field(default="accent", description="icon and title colour")
    logos: Annotated[
        list[LogoItem],
        BeforeValidator(_coerce_text_items("file_id")),
        Field(min_length=1, max_length=MAX_LOGO_WALL_GROUP_LOGOS),
    ]


class LogoWallBlock(_Strict):
    """Logos grouped by sector / category, one card per group.

    The "trusted by, sector by sector" slide as ONE block, instead of a
    ``callout`` per sector with a ``logo_grid`` inside it. Every group is a
    card - its icon and title on one line, its logos under them filling the
    card's width, the rows spread over the card's height - and the cards of a
    row share one height. ``columns`` is the number of cards per row (auto: all
    of them up to three, 2 x 2 for four), ``per_row`` the logos per row inside
    a card (auto: balanced rows). The logos are sized like a ``logo_grid``'s,
    ``tile_height`` included (one cell height across every card).
    """

    type: Literal["logo_wall"]
    groups: Annotated[
        list[LogoWallGroup], Field(min_length=1, max_length=MAX_LOGO_WALL_GROUPS)
    ]
    columns: Literal[1, 2, 3] | None = Field(
        default=None, description="cards per row; omit for auto (<= 3, 2 x 2 for 4)"
    )
    per_row: Annotated[int, Field(ge=1, le=MAX_LOGO_WALL_GROUP_LOGOS)] | None = Field(
        default=None, description="logos per row in a card; omit for auto"
    )
    logo_height: LogoHeight = Field(default="md", description=LOGO_HEIGHT_DESCRIPTION)
    optical_balance: bool = Field(default=True, description=OPTICAL_BALANCE_DESCRIPTION)
    optical_weight: bool | None = Field(default=None, description=OPTICAL_WEIGHT_DESCRIPTION)
    tile_height: LogoTileHeight | None = Field(default=None, description=LOGO_TILE_HEIGHT_DESCRIPTION)
    tone: LogoTone = Field(default="original", description=LOGO_TONE_DESCRIPTION)
    framed: bool = Field(default=False, description="a tile hugging each logo")
    show_labels: bool = False
    fill: bool = Field(default=True, description="the cards share the height left")
    caption: Markup | None = None


# ── Formula and configuration cards ──────────────────────────────────────────
#
# The licensing / packaging slide as native blocks: "A + B + C = the offer"
# (``formula``) above the configurations a buyer can pick (``config_cards``).
# Before them the only way was a static picture - not editable, deaf to the
# theme and to the light / dark scheme.

FormulaOperator = Literal["+", "=", "-", "x"]
FORMULA_OPERATORS: tuple[str, ...] = typing.get_args(FormulaOperator)
FormulaVariant = Literal["card", "result"]
FormulaAlign = Literal["start", "center"]
ConfigRowState = Literal["active", "dimmed"]
ConfigRowStyle = Literal["plain", "outlined"]
# A formula card's bar, a configuration card's title and a row's chip wear a
# tone or a colour of the template palette (a product's own hue: the core's
# "Included" chip in its brand colour), drawn as `data-color` like an
# architecture node.
ToneOrColor = Tone | PaletteColor


class FormulaLogo(_Strict):
    """One logo of a formula term's row: a file and its variant for a light slide."""

    file_id: RequiredFileId = Field(description="platform file id of the logo (a template asset)")
    logo_light_file_id: FileId = Field(
        default=None, description="the logo's variant for a light slide (a white wordmark's dark twin)"
    )


class FormulaTerm(_Strict):
    """One card of a ``formula``: an icon, a logo or a row of logos over a title."""

    title: Label
    subtitle: Markup | None = None
    icon: Icon | None = None
    logo_file_id: FileId = Field(default=None, description="a product logo instead of the icon")
    logo_light_file_id: FileId = Field(
        default=None, description="the logo's variant for a light slide (a white wordmark's dark twin)"
    )
    logos: Annotated[
        list[FormulaLogo],
        BeforeValidator(_coerce_text_items("file_id")),
        Field(max_length=MAX_FORMULA_TERM_LOGOS),
    ] = Field(
        default_factory=list,
        description="a row of logos (modules) instead of the icon: file ids, or {file_id, logo_light_file_id}",
    )
    tone: ToneOrColor = Field(
        default="accent", description="the bar on the card's top edge; a tone or brand_1-4 / chart_1-6"
    )
    variant: FormulaVariant = Field(default="card", description="result = brand gradient, white text (one term)")
    show_title: bool = Field(
        default=True, description="false: a wordmark logo names the term, the title stays its alt text"
    )

    @model_validator(mode="after")
    def _one_mark(self) -> FormulaTerm:
        if sum(bool(mark) for mark in (self.icon, self.logo_file_id, self.logos)) > 1:
            raise ValueError("give a formula term ONE of icon, logo_file_id or logos.")
        if not self.show_title and not (self.logo_file_id or self.logos):
            raise ValueError("show_title false needs a logo_file_id or logos to name the term.")
        if self.logo_light_file_id and not self.logo_file_id:
            raise ValueError("logo_light_file_id is the light variant of logo_file_id: give logo_file_id too.")
        return self


class FormulaBlock(_Strict):
    """Cards joined by operators: "Core + Modules + Capacity = One license".

    ``operators`` holds one operator per gap (``len(terms) - 1``); omitted, every
    gap is ``+`` and the last one ``=``. At most ONE term is the ``result``
    (usually the last), drawn in the brand gradient with white text; a term the
    editor appends after it stays valid until the author moves it.
    """

    type: Literal["formula"]
    terms: Annotated[
        list[FormulaTerm], Field(min_length=MIN_FORMULA_TERMS, max_length=MAX_FORMULA_TERMS)
    ]
    operators: list[FormulaOperator] | None = Field(
        default=None, description="one per gap; omit for + ... + ="
    )
    align: FormulaAlign = Field(default="center", description="a card's mark, title and subtitle")
    fill: bool = Field(default=False, description="the cards take the height left")

    @model_validator(mode="after")
    def _shape(self) -> FormulaBlock:
        gaps = len(self.terms) - 1
        if self.operators is not None and len(self.operators) != gaps:
            raise ValueError(
                f"a formula of {len(self.terms)} terms needs {gaps} operator(s), got {len(self.operators)}."
            )
        if sum(term.variant == "result" for term in self.terms) > 1:
            raise ValueError("a formula has at most one result term (usually the last).")
        return self

    def resolved_operators(self) -> list[str]:
        """The operator drawn in each gap (the default: ``+`` then a final ``=``)."""
        if self.operators is not None:
            return list(self.operators)
        gaps = len(self.terms) - 1
        return ["+"] * (gaps - 1) + ["="]


class ConfigRow(_Strict):
    """One line of a configuration card: a logo or a label, and its status chip."""

    label: Label | None = Field(default=None, description="the name; the logo's alt text with one")
    logo_file_id: FileId = None
    logo_light_file_id: FileId = Field(
        default=None, description="the logo's variant for a light slide (a white wordmark's dark twin)"
    )
    chip: Label | None = Field(default=None, description='e.g. "Included", "Enterprise Edition"')
    chip_tone: ToneOrColor = Field(
        default="accent", description="solid chip in this tone or palette colour (brand_1-4, chart_1-6)"
    )
    tinted: bool = Field(
        default=False, description="the row is tinted in the chip's colour and the chip is its text (Included)"
    )
    state: ConfigRowState = Field(default="active", description="dimmed = grey logo, dashed chip (available, not used)")

    @model_validator(mode="after")
    def _named(self) -> ConfigRow:
        if not (self.logo_file_id or self.label):
            raise ValueError("a configuration row needs a logo_file_id or a label.")
        if self.logo_light_file_id and not self.logo_file_id:
            raise ValueError("logo_light_file_id is the light variant of logo_file_id: give logo_file_id too.")
        return self


class ConfigCard(_Strict):
    title: Label
    subtitle: Markup | None = None
    tone: ToneOrColor = "accent"
    rows: Annotated[list[ConfigRow], Field(min_length=1, max_length=MAX_CONFIG_CARD_ROWS)]
    footer: Markup | None = Field(default=None, description="a band under the rows; its label with footer_value")
    footer_value: Label | None = Field(default=None, description="the footer band's value, at its end")

    @model_validator(mode="after")
    def _footer(self) -> ConfigCard:
        if self.footer_value and not self.footer:
            raise ValueError("footer_value is the value of the footer band: give footer (its label) too.")
        return self


class ConfigCardsBlock(_Strict):
    """2-4 configuration cards side by side: stacked logo rows, each with a status chip."""

    type: Literal["config_cards"]
    cards: Annotated[
        list[ConfigCard], Field(min_length=MIN_CONFIG_CARDS, max_length=MAX_CONFIG_CARDS)
    ]
    heading: Label | None = Field(
        default=None, description="a label over the cards with a rule, e.g. Example configurations"
    )
    row_style: ConfigRowStyle = Field(
        default="outlined", description="outlined: active rows framed in the card tone, dimmed rows on a grey surface"
    )
    fill: bool = Field(default=False, description="the cards take the height left")


# ── Collateral blocks ────────────────────────────────────────────────────────
#
# The shapes every product brief, one-pager, battle card and pricing page in the
# sales collateral is built from — read off the marketing library and turned
# into typed blocks so a brief authored here reproduces them in either theme.


class Badge(_Strict):
    text: Label
    icon: Icon | None = None
    tone: Tone = "accent"


class BadgesBlock(_Strict):
    """A row of pills: certifications, editions, deployment options, tags."""

    type: Literal["badges"]
    items: Annotated[
        list[Badge],
        BeforeValidator(_coerce_text_items("text")),
        Field(min_length=1, max_length=MAX_BADGES),
    ]
    variant: BadgeVariant = "outline"
    size: Size = "md"
    align: Align = "start"


class KeyValueItem(_Strict):
    label: Label
    value: Markup
    icon: Icon | None = None
    tone: Tone | None = None


class KeyValueBlock(_Strict):
    """A spec sheet: label / value pairs (prerequisites, terms, a fact box)."""

    type: Literal["key_value"]
    items: Annotated[list[KeyValueItem], Field(min_length=1, max_length=MAX_KEY_VALUE_ITEMS)]
    columns: Literal[1, 2] = 1
    density: Density = "normal"
    framed: bool = Field(default=True, description="draw the pairs on a bordered surface")
    fill: bool = Field(default=False, description=FILL_ROWS_DESCRIPTION)


class PricingTier(_Strict):
    """One offer card: an edition, a plan, a support tier."""

    name: Markup
    price: Annotated[str, Field(min_length=1, max_length=40)] = Field(description='"Free", "Contact us", "EUR 25k"')
    period: Label | None = Field(default=None, description='"per year", "per analyst / month"')
    description: Markup | None = None
    features: Annotated[list[Markup], Field(default_factory=list, max_length=MAX_TIER_FEATURES)]
    cta: Label | None = Field(default=None, description="button label")
    badge: Label | None = Field(default=None, description='ribbon such as "Most popular"')
    icon: Icon | None = None
    tone: Tone = "accent"
    highlight: bool = Field(default=False, description="the tier being sold: raised, toned border")


class PricingTiersBlock(_Strict):
    """Side-by-side offer cards (editions, plans, support tiers)."""

    type: Literal["pricing_tiers"]
    tiers: Annotated[list[PricingTier], Field(min_length=1, max_length=MAX_PRICING_TIERS)]
    density: Density = "normal"


ActionUrl = Annotated[
    str,
    Field(min_length=1, max_length=MAX_URL_CHARS),
    AfterValidator(check_action_url),
]


class BannerAction(_Strict):
    label: Label
    url: ActionUrl | None = Field(default=None, description="https:// or mailto:")
    icon: Icon | None = None
    primary: bool | None = Field(default=None, description="filled button; when no action says so, the first one is")


class BannerBlock(_Strict):
    """A full-width brand band: the hero header of a page or its call to action."""

    type: Literal["banner"]
    title: TitleMarkup
    subtitle: Markup | None = None
    eyebrow: Label | None = Field(default=None, description="small label above the title")
    icon: Icon | None = None
    actions: Annotated[list[BannerAction], Field(default_factory=list, max_length=MAX_BANNER_ACTIONS)]
    variant: BannerVariant = "gradient"
    tone: Tone = "accent"
    align: Align = "start"
    size: Size = "md"


class Persona(_Strict):
    """One audience card: who they are, what hurts, what they get."""

    name: Markup = Field(description='the role - "CISO", "SOC analyst", "Threat hunter"')
    tagline: Markup | None = Field(default=None, description="one line: what they own or are measured on")
    icon: Icon | None = None
    pains: Annotated[list[Markup], Field(default_factory=list, max_length=MAX_PERSONA_POINTS)] = Field(
        description="their challenges today"
    )
    gains: Annotated[list[Markup], Field(default_factory=list, max_length=MAX_PERSONA_POINTS)] = Field(
        description="what changes for them with the product"
    )
    tone: Tone = "accent"

    @model_validator(mode="after")
    def _says_something(self) -> Persona:
        if not self.pains and not self.gains and not self.tagline:
            raise ValueError("a persona needs a tagline, pains or gains - a name alone is an empty card.")
        return self


class PersonasBlock(_Strict):
    """Audience cards side by side: the "who it is for" of a pitch, a brief or a
    battlecard. Each card pairs the role's challenges with the outcomes, so the
    pitch is read from the buyer's seat rather than from the feature list."""

    type: Literal["personas"]
    items: Annotated[list[Persona], Field(min_length=1, max_length=MAX_PERSONAS)]
    pains_label: Label = Field(default="Challenges", description="heading of the pains list on every card")
    gains_label: Label = Field(default="Outcomes", description="heading of the gains list on every card")
    columns: Literal[1, 2, 3, 4] | None = Field(default=None, description="omit for auto (one per persona)")
    density: Density = "normal"
    fill: bool = Field(default=False, description=FILL_ROWS_DESCRIPTION)


def check_single_line(value: str) -> str:
    """Refuse a line break in a field the renderer draws on ONE row.

    A profile fact is ``white-space: nowrap`` + an ellipsis, measured by the
    fit pass and the layout audit as ONE run; the inline markup renders ``\\n``
    as ``<br>``, which breaks a nowrap row into two and defeats all three. The
    field-addressed refusal names the fix; the stage editor never offers the
    break for this field (``singleLine``), so a person only meets this through
    the tool or the REST body.
    """
    if "\n" in value or "\r" in value:
        raise ValueError(
            "must be a single line (no line break): a profile fact is drawn on one row and "
            "cut with an ellipsis, never wrapped - shorten it or split it into two lines[] entries"
        )
    return value


ProfileLineText = Annotated[
    str,
    Field(min_length=1, max_length=MAX_PROFILE_LINE_CHARS),
    AfterValidator(check_markup),
    AfterValidator(check_single_line),
]


class ProfileLine(_Strict):
    """One fact about a person: a role, an employer, a credential - drawn on ONE
    line with a 32 px tile in front of it (a logo by file id, else a Lucide
    icon, else the tone dot). A line that does not fit shrinks one type step
    and is then cut with an ellipsis, which the layout check reports."""

    text: ProfileLineText
    icon: Icon | None = None
    logo_file_id: FileId = Field(
        default=None,
        description="platform file id of a small logo for the tile (a template asset, get_template role=partner_logo / customer_logo, or a file from fetch_image_asset); wins over icon",
    )


class Profile(_Strict):
    """One person: a circular portrait, the name, the title, then the lines."""

    name: Label
    title: Label | None = Field(default=None, description='the role under the name - "Chief Executive Officer"')
    photo_file_id: FileId = Field(
        default=None,
        description="platform file id of the portrait (get_template role=people_photo, or a fetch_image_asset file); the tile shows the initials without one",
    )
    lines: Annotated[
        list[ProfileLine],
        BeforeValidator(_coerce_text_items("text")),
        Field(min_length=1, max_length=MAX_PROFILE_LINES),
    ] = Field(description="1-5 one-line facts, top to bottom")
    tone: Tone = "accent"


class ProfilesBlock(_Strict):
    """People on a grid - the founders, the speakers, a leadership team:
    one card per person with a circular photo (96-128 px), the name
    and title on one line each, then one row per line with a 32 px logo tile (or icon) and the
    fact on a SINGLE line. The native form of the slide an agent used to build
    as a browser-rendered PNG, so one logo or one padding is a one-field edit.
    Container padding follows the surface tokens; a picture is never flush."""

    type: Literal["profiles"]
    items: Annotated[list[Profile], Field(min_length=1, max_length=MAX_PROFILES)]
    columns: Literal[1, 2, 3, 4] | None = Field(default=None, description="omit for a balanced grid (9 people: 3 x 3)")
    density: Density = "normal"
    photo_size: Size = Field(default="md", description="portrait diameter: sm 96 px, md 112 px, lg 128 px")


_DIAGRAM_TYPES: tuple[str, ...] = (
    "flowchart",
    "graph",
    "sequencediagram",
    "classdiagram",
    "statediagram",
    "statediagram-v2",
    "erdiagram",
    "journey",
    "gantt",
    "pie",
    "quadrantchart",
    "requirementdiagram",
    "gitgraph",
    "mindmap",
    "timeline",
    "sankey-beta",
    "xychart-beta",
    "block-beta",
    "packet-beta",
    "kanban",
    "architecture-beta",
    "radar-beta",
    "treemap-beta",
)
_DIAGRAM_BANNED_RE = re.compile(
    r"%%\{|<\s*script|javascript\s*:|^\s*click\s+|securitylevel|srcdoc\s*=|\son[a-z]+\s*=",
    re.IGNORECASE | re.MULTILINE,
)


def check_diagram_source(value: str) -> str:
    """Refuse Mermaid source that could reconfigure or script the renderer.

    Mermaid runs with ``securityLevel: "strict"`` on the frontend; this is the
    generation-time layer that keeps a directive (``%%{init: ...}%%``), a click
    handler or a script tag from ever being stored.
    """
    if _DIAGRAM_BANNED_RE.search(value):
        raise ValueError(
            "diagram source must not contain %%{ directives, click handlers, "
            "script tags or event-handler attributes — plain Mermaid syntax only."
        )
    first_line = next(
        (
            line.strip()
            for line in value.splitlines()
            if line.strip() and not line.strip().startswith("%%")
        ),
        "",
    )
    head = first_line.split(maxsplit=1)[0].lower().rstrip(":") if first_line else ""
    if head not in _DIAGRAM_TYPES:
        raise ValueError(
            "diagram source must start with a Mermaid diagram type "
            "(flowchart LR, sequenceDiagram, pie, mindmap, timeline, gantt, "
            f"quadrantChart, xychart-beta...), got {first_line[:40]!r}."
        )
    return value


class DiagramBlock(_Strict):
    """A Mermaid diagram, rendered client-side (strict security level)."""

    type: Literal["diagram"]
    source: Annotated[
        str,
        Field(min_length=1, max_length=MAX_DIAGRAM_CHARS),
        AfterValidator(check_diagram_source),
    ] = Field(description="Mermaid source, e.g. 'flowchart LR\\n A --> B'")
    caption: Markup | None = None
    size: MediaSize = "md"
    fill: bool = False


# ── Blocks: the native architecture diagram ──────────────────────────
#
# Mermaid draws a flowchart; it cannot draw the figure a platform or strategy
# deck needs — product logos in the nodes, labeled directional flows between
# modules, the core everything runs on, a hub band, a legend — and it never
# looks like the brand. The ``architecture`` block is that figure as DATA: the
# agent places nodes on a small grid and names the edges, the frontend
# (``blocks/Architecture.tsx``) lays it out deterministically on the fixed
# canvas and draws it as inline SVG through React — never from markup, so the
# "structured JSON, never HTML / SVG" contract of the deck holds — with the
# deck's own palette and fonts, arrow endpoints centered on the node sides,
# parallel lanes for an exchange in both directions and routes scored so an
# edge never runs through a node when it has an alternative.

#: Node ids are referenced by edges: a letter, then letters, digits, ``-`` / ``_``.
ARCHITECTURE_ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{0,31}$"
NodeId = Annotated[str, Field(pattern=ARCHITECTURE_ID_PATTERN, max_length=32)]
NodeRef = Annotated[str, Field(min_length=1, max_length=32)]
NodeLabel = _markup(MAX_ARCHITECTURE_LABEL_CHARS)
ChipLabel = _markup(MAX_ARCHITECTURE_CHIP_CHARS)
NodeDescription = _markup(MAX_ARCHITECTURE_DESCRIPTION_CHARS)
GridIndex = Annotated[int, Field(ge=1, le=MAX_ARCHITECTURE_GRID)]


class ArchitectureNode(_Strict):
    """A box on the architecture grid: a product, a system, an actor, the core."""

    id: NodeId = Field(description="unique in the block; edges refer to it")
    label: NodeLabel
    sublabel: NodeLabel | None = Field(default=None, description="one short qualifier under the label")
    description: NodeDescription | None = Field(
        default=None, description="detail shown on hover / focus in the live deck"
    )
    icon: Icon | None = None
    logo_file_id: Annotated[str | None, Field(max_length=36)] = Field(
        default=None, description="a template asset (a product logo) in place of icon: one or the other"
    )
    logo_height: LogoHeight | None = Field(
        default=None,
        description=(
            "a card's or band's logo height (sm 28 / md 40 / lg 56 / xl 72 px), cropped to its ink with "
            "optical balance (a square mark grows up to 1.5x), the card growing to fit; md or lg for a "
            "stage-by-vendor row; omit for the fixed small box"
        ),
    )
    icon_tile: bool = Field(
        default=True,
        description=(
            "false = a card's icon sits inline before the label instead of in a tile above it, "
            "so the label stays vertically centred (two ghost cards on one row then align)"
        ),
    )
    shape: NodeShape = Field(
        default="card", description="circle = a hub; pill = a compact band or actor"
    )
    tone: Tone | None = Field(
        default=None,
        description="border, icon and glow colour; default: accent for a core node, neutral otherwise",
    )
    color: PaletteColor | None = Field(
        default=None,
        description=(
            "in place of tone, a template palette colour for border, tint, icon, chips and glow "
            "(brand_1-4 = its brand hues, chart_1-6 = its series)"
        ),
    )
    emphasis: NodeEmphasis = Field(
        default="normal",
        description="core = the hub the rest runs on (larger, glowing); ghost = context, dimmed",
    )
    frame: bool = Field(
        default=True,
        description=(
            "false = the content alone (logo or icon, label, sublabel, chips): no border, surface, shadow "
            "or glow, the box kept for the grid and the edges, which meet the content"
        ),
    )
    chips: Annotated[list[ChipLabel], Field(max_length=MAX_ARCHITECTURE_CHIPS)] = Field(
        default_factory=list, description="small pills inside a card (capabilities, tiers)"
    )
    row: GridIndex = Field(description="grid row, 1 = top (1-6)")
    col: GridIndex = Field(description="grid column, 1 = left (1-6)")
    col_span: GridIndex = Field(
        default=1, description="columns spanned: a band across the top, a bus along the bottom"
    )

    @model_validator(mode="after")
    def _shape_fits_content(self) -> ArchitectureNode:
        if self.shape == "circle" and self.col_span != 1:
            raise ValueError("a circle node cannot span several columns.")
        if self.shape != "card" and self.chips:
            raise ValueError("chips need a card node — a pill or a circle has no room for them.")
        if self.icon and self.logo_file_id:
            # The renderer draws one medium per node; a second one would be a
            # field that validates and changes nothing.
            raise ValueError("give a node an icon OR a logo_file_id, not both: the logo takes the icon's place.")
        if self.logo_height is not None:
            # A field that validates and changes nothing is refused: the
            # height sizes a logo, and only a card's or a band's.
            if not self.logo_file_id:
                raise ValueError("logo_height sizes a node's logo - set logo_file_id, or leave logo_height out.")
            if self.shape != "card":
                raise ValueError(
                    "logo_height applies to a card (or a band): a pill or a circle keeps its fixed logo size."
                )
        if not self.icon_tile:
            # The tile is a card's: without an icon there is none to
            # hide, and a pill or a circle draws its icon bare already.
            if not self.icon:
                raise ValueError("icon_tile: false needs an icon to draw inline - set icon, or leave icon_tile out.")
            if self.shape != "card":
                raise ValueError("icon_tile applies to a card: a pill or a circle draws its icon without a tile already.")
        return self


#: An edge end naming a zone's border instead of a node:
#: ``zone:<id>``, ``zone:<label>`` or ``zone:<0-based index>``, read in that
#: order by :func:`resolve_zone_ref` (mirrored by ``resolveZoneRef`` in
#: ``blocks/architecture-layout.ts``).
ZONE_REF_PREFIX = "zone:"
EdgeEnd = Annotated[
    str, Field(min_length=1, max_length=len(ZONE_REF_PREFIX) + MAX_ARCHITECTURE_LABEL_CHARS)
]
Lane = Annotated[int, Field(ge=1, le=MAX_ARCHITECTURE_LANE)]


class ArchitectureEdge(_Strict):
    """A connection between two nodes, or between a node and a zone's border: a data flow, an exchange, a plain tie."""

    source: EdgeEnd = Field(
        description="node id the edge leaves, or zone:ID (ID = a zone's id, label or index) for that zone's border"
    )
    target: EdgeEnd = Field(
        description="node id it reaches, or zone:ID: it ends on that zone's border, nearest the node"
    )
    source_side: EdgeSide | None = Field(
        default=None, description="the side it leaves, honoured even facing away; default: the router's pick"
    )
    target_side: EdgeSide | None = Field(
        default=None, description="the side it enters (a zone end: the side of the zone's border)"
    )
    lane: Lane | None = Field(
        default=None,
        description=(
            f"1-{MAX_ARCHITECTURE_LANE}: the edges of one pair of nodes on different lanes run parallel, "
            "each at one height on both nodes (lane 1 top / left)"
        ),
    )
    label: NodeLabel | None = Field(
        default=None, description="what flows, 1-3 words: 'Threat data', 'Findings'"
    )
    arrows: EdgeArrows = Field(
        default="end",
        description="end = source -> target; both = an exchange; none = a tie with no direction",
    )
    dashed: bool = Field(default=False, description="optional, planned or asynchronous")
    tone: Tone | None = Field(
        default=None, description="default: accent for arrows, muted for a tie with no arrows"
    )
    route: EdgeRoute = Field(
        default="auto",
        description=(
            "auto = straight to or from a core node when nothing is in the way and no side is pinned, "
            "elbows otherwise"
        ),
    )


class ArchitectureZone(_Strict):
    """A titled panel behind a rectangle of grid cells: a platform, a tier, a boundary."""

    id: NodeId | None = Field(default=None, description="unique among the zones; an edge end names the zone zone:ID")
    label: NodeLabel | None = Field(default=None, description="the title; optional with a logo_file_id or chips")
    icon: Icon | None = None
    logo_file_id: Annotated[str | None, Field(max_length=36)] = Field(
        default=None,
        description=(
            "a template asset (the platform's brand mark) in the top-left corner in place of icon, "
            "the label after it on the same row"
        ),
    )
    tone: Tone | None = Field(default=None, description="tint and title colour; default muted")
    color: PaletteColor | None = Field(
        default=None, description="in place of tone, a template palette colour for border, tint and icon"
    )
    row: GridIndex = Field(description="first row covered (1-6)")
    col: GridIndex = Field(description="first column covered (1-6)")
    row_span: GridIndex = Field(default=1, description="rows covered (1-6)")
    col_span: GridIndex = Field(default=1, description="columns covered (1-6)")
    chips: Annotated[list[ChipLabel], Field(max_length=MAX_ARCHITECTURE_ZONE_CHIPS)] = Field(
        default_factory=list,
        description=(
            "one row of small pills on the head row (the platform's capabilities), drawn bare in the zone's "
            "tone or colour: never wrapped, no grid cell taken"
        ),
    )
    chips_align: Align = Field(
        default="end",
        description="where the chips sit on the head row: end = the top-right corner, start = after the logo / label",
    )

    @model_validator(mode="after")
    def _head_has_something_to_show(self) -> ArchitectureZone:
        if not self.label and not self.logo_file_id and not self.chips:
            raise ValueError("a zone needs a label, a logo_file_id or chips: its head says what the boundary is.")
        if self.icon and self.logo_file_id:
            # One mark per zone head, as on a node: the logo takes the icon's place.
            raise ValueError("give a zone an icon OR a logo_file_id, not both: the logo takes the icon's place.")
        if self.chips_align != "end" and not self.chips:
            # A placement with nothing to place would validate and change nothing.
            raise ValueError("chips_align places the zone's chips: set chips, or leave chips_align out.")
        return self

    def named(self, index: int) -> str:
        """How a refusal names the zone: its index, and its label when it has one."""
        return f"zones.{index} ({self.label!r})" if self.label else f"zones.{index}"

    def cells(self) -> set[tuple[int, int]]:
        return {
            (row, col)
            for row in range(self.row, self.row + self.row_span)
            for col in range(self.col, self.col + self.col_span)
        }


def resolve_zone_ref(ref: str, zones: Sequence[ArchitectureZone]) -> list[int]:
    """The indexes of the zones a ``zone:`` edge end names: by id, else by 0-based index, else by label.

    A label is matched as written or as its plain text; several zones sharing
    it all come back, which the block refuses as ambiguous. A digit-only key
    past the last zone falls through to the labels, so a zone labelled
    ``2026`` is named by its label.
    """
    key = ref.removeprefix(ZONE_REF_PREFIX).strip()
    by_id = [index for index, zone in enumerate(zones) if zone.id == key]
    if by_id:
        return by_id
    if key.isascii() and key.isdigit() and int(key) < len(zones):
        return [int(key)]
    return [
        index
        for index, zone in enumerate(zones)
        if zone.label and key in (zone.label, markup_to_plain_text(zone.label))
    ]


class ArchitectureLegendItem(_Strict):
    label: NodeLabel
    marker: LegendMarker = Field(
        default="arrow", description="the symbol drawn before the label"
    )
    tone: Tone | None = None


class ArchitectureBlock(_Strict):
    """A native architecture / ecosystem diagram — nodes on a grid, labeled edges, zones — drawn as vector graphics in the deck's palette and fonts (no Mermaid)."""

    type: Literal["architecture"]
    nodes: Annotated[
        list[ArchitectureNode], Field(min_length=2, max_length=MAX_ARCHITECTURE_NODES)
    ] = Field(description="every node has its own grid cells; the grid is as large as the nodes make it")
    edges: Annotated[list[ArchitectureEdge], Field(max_length=MAX_ARCHITECTURE_EDGES)] = Field(
        default_factory=list, description="one entry per direction; the renderer routes them"
    )
    zones: Annotated[list[ArchitectureZone], Field(max_length=MAX_ARCHITECTURE_ZONES)] = Field(
        default_factory=list, description="panels behind the nodes they cover; zones never overlap"
    )
    legend: Annotated[
        list[ArchitectureLegendItem], Field(max_length=MAX_ARCHITECTURE_LEGEND_ITEMS)
    ] = Field(default_factory=list, description="what the arrow styles and tones mean")
    caption: Markup | None = None
    size: MediaSize = "md"
    fill: bool = False

    @model_validator(mode="after")
    def _graph_is_consistent(self) -> ArchitectureBlock:
        ids = [node.id for node in self.nodes]
        duplicates = sorted({node_id for node_id in ids if ids.count(node_id) > 1})
        if duplicates:
            raise ValueError(f"node ids must be unique: {', '.join(duplicates)}.")
        zone_ids = [zone.id for zone in self.zones if zone.id]
        duplicates = sorted({zone_id for zone_id in zone_ids if zone_ids.count(zone_id) > 1})
        if duplicates:
            raise ValueError(f"zone ids must be unique: {', '.join(duplicates)}.")
        known = set(ids)
        seen_pairs: dict[tuple[tuple[str, str | int], tuple[str, str | int]], int] = {}
        lanes: dict[tuple[frozenset[str], int], int] = {}
        # (edge index, node id, zone index) of every edge ending on a zone's border.
        zone_ends: list[tuple[int, str, int]] = []
        for index, edge in enumerate(self.edges):
            ends: dict[str, tuple[str, str | int]] = {}
            for end in ("source", "target"):
                ref = getattr(edge, end)
                if ref.startswith(ZONE_REF_PREFIX):
                    ends[end] = ("zone", self._zone_end(index, end, ref))
                elif ref in known:
                    ends[end] = ("node", ref)
                else:
                    raise ValueError(
                        f"edges.{index}.{end} names no node: {ref!r} (nodes: {', '.join(ids)}; "
                        f"a zone is named {ZONE_REF_PREFIX}ID, ID being its id, label or index)."
                    )
            source, target = ends["source"], ends["target"]
            if source[0] == "zone" and target[0] == "zone":
                raise ValueError(
                    f"edges.{index}: {edge.source} -> {edge.target} joins two zones: an edge has a node at one end at least."
                )
            if source == target:
                raise ValueError(f"edges.{index}: a node cannot connect to itself ({edge.source!r}).")
            pair = (source, target)
            if pair in seen_pairs:
                raise ValueError(
                    f"edges.{index}: {edge.source} -> {edge.target} is declared twice (edges.{seen_pairs[pair]}) — merge the "
                    'labels into one edge, or use arrows="both" for an exchange.'
                )
            seen_pairs[pair] = index
            if "zone" in (source[0], target[0]):
                node, zone = (target, source) if source[0] == "zone" else (source, target)
                if edge.lane is not None:
                    raise ValueError(
                        f"edges.{index}: lane orders the edges between two nodes, and this one has a zone "
                        "at one end - leave lane out."
                    )
                zone_ends.append((index, str(node[1]), int(zone[1])))
            elif edge.lane is not None:
                key = (frozenset((edge.source, edge.target)), edge.lane)
                if key in lanes:
                    raise ValueError(
                        f"edges.{index}: lane {edge.lane} between {edge.source} and {edge.target} is already "
                        f"edges.{lanes[key]}'s - two edges on one lane are drawn on top of each other: "
                        "give each edge of the pair its own lane."
                    )
                lanes[key] = index
        occupied: dict[tuple[int, int], str] = {}
        node_cells: dict[str, set[tuple[int, int]]] = {}
        for node in self.nodes:
            last_col = node.col + node.col_span - 1
            if last_col > MAX_ARCHITECTURE_GRID:
                raise ValueError(
                    f"node {node.id!r}: col {node.col} + col_span {node.col_span} runs past "
                    f"column {MAX_ARCHITECTURE_GRID}."
                )
            for col in range(node.col, last_col + 1):
                cell = (node.row, col)
                if cell in occupied:
                    raise ValueError(
                        f"nodes {occupied[cell]!r} and {node.id!r} both occupy row {node.row}, "
                        f"col {col}: give every node its own cells."
                    )
                occupied[cell] = node.id
            node_cells[node.id] = {(node.row, col) for col in range(node.col, last_col + 1)}
        rows = max(node.row for node in self.nodes)
        cols = max(node.col + node.col_span - 1 for node in self.nodes)
        for index, zone in enumerate(self.zones):
            cells = zone.cells()
            if zone.row + zone.row_span - 1 > rows or zone.col + zone.col_span - 1 > cols:
                raise ValueError(
                    f"{zone.named(index)} runs past the grid the nodes define "
                    f"({rows} rows x {cols} columns)."
                )
            # A zone may span empty cells (a panel around a core with free
            # corners is the usual shape) but must group at least one node ...
            if not cells & occupied.keys():
                raise ValueError(
                    f"{zone.named(index)} covers no node: a zone is drawn behind "
                    "the nodes it groups."
                )
            # ... and take every node it touches whole: a panel border drawn
            # through the middle of a spanning band cannot be read either way.
            for node in self.nodes:
                if cells & node_cells[node.id] and not node_cells[node.id] <= cells:
                    raise ValueError(
                        f"{zone.named(index)} cuts through node {node.id!r}: a zone "
                        "covers whole nodes, so widen the zone or narrow the node's col_span."
                    )
        for index, zone in enumerate(self.zones):
            for offset, other in enumerate(self.zones[index + 1 :], start=index + 1):
                if zone.cells() & other.cells():
                    raise ValueError(
                        f"{zone.named(index)} and {other.named(offset)} overlap: zones sit side by "
                        "side, never on top of each other."
                    )
        for index, node_id, zone_index in zone_ends:
            if node_cells[node_id] <= self.zones[zone_index].cells():
                raise ValueError(
                    f"edges.{index}: node {node_id!r} sits in {self.zones[zone_index].named(zone_index)}, so the "
                    "zone's border is not between them: tie it to a node, or to another zone."
                )
        if not self.edges and not self.zones:
            raise ValueError(
                "an architecture needs at least one edge or one zone — nodes alone are a "
                "feature_list or numbered_cards."
            )
        # Every node takes part in the picture: an edge ties it in or a zone
        # groups it. A box floating beside the graph is a bullet, not a node.
        connected = {edge.source for edge in self.edges} | {edge.target for edge in self.edges}
        zoned: set[tuple[int, int]] = set().union(*(zone.cells() for zone in self.zones))
        for node in self.nodes:
            if node.id not in connected and not node_cells[node.id] & zoned:
                raise ValueError(
                    f"node {node.id!r} has no edge and sits in no zone: connect it, group it, "
                    "or drop it."
                )
        return self

    def _zone_end(self, index: int, end: str, ref: str) -> int:
        """The zone a ``zone:`` end of ``edges.<index>`` names; refused when it names none, or several by one label."""
        found = resolve_zone_ref(ref, self.zones)
        if len(found) == 1:
            return found[0]
        if found:
            named = ", ".join(self.zones[zone].named(zone) for zone in found)
            raise ValueError(
                f"edges.{index}.{end} = {ref!r} names {len(found)} zones by their label ({named}): "
                "give them ids and name one by its id."
            )
        listing = ", ".join(
            f"{ZONE_REF_PREFIX}{zone.id or position}" + (f" ({zone.label!r})" if zone.label else "")
            for position, zone in enumerate(self.zones)
        )
        raise ValueError(
            f"edges.{index}.{end} names no zone: {ref!r} ("
            + (f"zones: {listing}" if listing else "the block has no zone")
            + ")."
        )


# ── Blocks: the native org chart ─────────────────────────────────────────────
#
# Reporting lines, teams and headcount are a TREE of seats, never a grid of
# boxes: the agent names each seat's manager and the frontend
# (``blocks/org-chart-layout.ts``, one engine for the viewer, the exports and
# the video figure) lays it out as a tidy tree - equal card widths and one
# baseline per level, children centred under their parent, one bus per parent,
# dotted lines routed last around the cards - and draws it as inline SVG
# through React (``blocks/OrgChart.tsx``). What is validated here is what no
# drawing could repair: the tree itself, and each kind holding what its card
# shows.

OrgName = _markup(MAX_ORG_CHART_NAME_CHARS)
OrgRole = _markup(MAX_ORG_CHART_ROLE_CHARS)
OrgChip = _markup(MAX_ORG_CHART_CHIP_CHARS)
OrgShortText = Annotated[str, Field(min_length=1, max_length=MAX_ORG_CHART_SHORT_CHARS)]
OrgCount = Annotated[int, Field(ge=1, le=MAX_ORG_CHART_COUNT)]
OrgDepth = Annotated[int, Field(ge=1, le=MAX_ORG_CHART_DEPTH)]
#: The seats no one sits in yet: a title, never a name or a photo.
_ORG_NAMELESS_KINDS = ("vacant", "planned")


class OrgChartNode(_Strict):
    """One seat of an org chart: a person, an open or planned seat, a team, a bubble, an external seat."""

    id: NodeId = Field(description="unique in the block; manager, dotted_to, roots and expand name it")
    kind: OrgNodeKind = Field(
        description=(
            "person = a real seat; vacant = open today; planned = a future hire; group = a team drawn as "
            "one card; bubble = a team drawn as a circle whose area is its count; external = contractor, "
            "partner or agency"
        )
    )
    manager: NodeRef | None = Field(default=None, description="the id this seat reports to; none on a root")
    dotted_to: Annotated[list[NodeRef], Field(max_length=MAX_ORG_CHART_DOTTED)] = Field(
        default_factory=list, description="secondary managers, drawn as dashed lines"
    )
    name: OrgName | None = Field(
        default=None, description="the person (required on a person); a group's leader; never on vacant / planned"
    )
    role: OrgRole | None = Field(
        default=None, description="the title; a group's or bubble's team name (required there and on vacant / planned)"
    )
    photo_file_id: Annotated[str | None, Field(max_length=36)] = Field(
        default=None, description="a people_photo asset, on a person or an external seat; initials otherwise"
    )
    icon: Icon | None = Field(default=None, description="drawn in place of the initials (a team, an agency)")
    chips: Annotated[list[OrgChip], Field(max_length=MAX_ORG_CHART_CHIPS)] = Field(
        default_factory=list, description="one row of small pills (skills, tiers, regions)"
    )
    tone: Tone | None = Field(default=None, description="default: the kind's own (warning vacant, accent planned)")
    color: PaletteColor | None = Field(default=None, description="in place of tone, a template palette colour")
    count: OrgCount | None = Field(
        default=None,
        description=(
            "headcount of a group / bubble (required on a bubble), seats of a planned or vacant line "
            "(planned count 3 draws one card, '+3' before its role); a group with reports declares its total"
        ),
    )
    count_label: OrgShortText | None = Field(default=None, description='what the count counts: "FTE", "open roles"')
    period: OrgShortText | None = Field(
        default=None, description='when a planned seat is hired ("H2 FY28"), a vacant seat\'s target date'
    )
    location: Annotated[str | None, Field(max_length=MAX_ORG_CHART_LOCATION_CHARS)] = None
    note: Annotated[str | None, Field(max_length=MAX_ORG_CHART_NOTE_CHARS)] = Field(
        default=None, description="shown in the tooltip only"
    )
    collapsed: bool = Field(default=False, description="fold this seat's reports into one group card with their count")
    emphasis: NodeEmphasis | None = Field(
        default=None, description="default: core on a root, normal elsewhere; ghost = context, dimmed"
    )

    @model_validator(mode="after")
    def _kind_holds_what_its_card_shows(self) -> OrgChartNode:
        node = repr(self.id)
        if self.kind == "person":
            if not self.name:
                raise ValueError(f"node {node} (person) needs a name: a person seat shows who sits in it.")
            if self.count is not None:
                raise ValueError(
                    f"node {node} (person) is one seat and takes no count: a team with a headcount is a "
                    "group or a bubble."
                )
        if self.kind in _ORG_NAMELESS_KINDS:
            if self.name:
                raise ValueError(
                    f"node {node} ({self.kind}) has no one in it yet: drop the name and put the seat's title "
                    "in role."
                )
            if self.photo_file_id:
                raise ValueError(f"node {node} ({self.kind}) has no one in it yet, so no photo.")
        if self.kind in ("vacant", "planned", "group", "bubble") and not self.role:
            what = "the team's name" if self.kind in ("group", "bubble") else "the seat's title"
            raise ValueError(f"node {node} ({self.kind}) needs a role: {what}.")
        if self.kind == "bubble" and self.count is None:
            raise ValueError(f"node {node} (bubble) needs a count: a bubble's area IS its headcount.")
        if self.kind in ("group", "bubble") and self.photo_file_id:
            raise ValueError(f"node {node} ({self.kind}) is a team, not a person: give it an icon, not a photo.")
        if self.photo_file_id and self.icon:
            raise ValueError(f"node {node}: give a seat a photo_file_id OR an icon, not both.")
        if self.period and self.kind not in _ORG_NAMELESS_KINDS:
            raise ValueError(
                f"node {node} ({self.kind}): period dates a planned hire or a vacant seat's target; "
                "a seat filled today has none."
            )
        if self.count_label and self.count is None:
            raise ValueError(f"node {node}: count_label names what count counts, and the node has no count.")
        if self.manager == self.id:
            raise ValueError(f"node {node} cannot report to itself.")
        dotted = list(self.dotted_to)
        if self.id in dotted:
            raise ValueError(f"node {node} cannot have a dotted line to itself.")
        if len(set(dotted)) != len(dotted):
            raise ValueError(f"node {node} names the same dotted_to manager twice.")
        if self.manager and self.manager in dotted:
            raise ValueError(
                f"node {node}: {self.manager!r} is its manager already - a dotted line is to a SECOND manager."
            )
        return self


class OrgChartLegendItem(_Strict):
    """One entry of a custom legend: a kind's card, a tone, or the dotted line."""

    label: NodeLabel
    kind: OrgNodeKind | None = Field(default=None, description="the card this entry explains")
    tone: Tone | None = None
    dotted: bool = Field(default=False, description="the entry explains the dotted line")

    @model_validator(mode="after")
    def _names_a_mark(self) -> OrgChartLegendItem:
        if self.kind is None and self.tone is None and not self.dotted:
            raise ValueError("a legend entry explains a kind, a tone or the dotted line: set one of them.")
        return self


def _org_nodes_within_cap(value: object) -> object:
    """Refuse past the hard cap BEFORE the seats are parsed, naming the first seat over it."""
    if isinstance(value, list) and len(value) > MAX_ORG_CHART_NODES:
        first = value[MAX_ORG_CHART_NODES]
        named = f" (the first past it is {first.get('id')!r})" if isinstance(first, dict) and first.get("id") else ""
        raise ValueError(
            f"{len(value)} seats is more than the {MAX_ORG_CHART_NODES} an org chart holds{named}: fold a "
            "team into one group or bubble with its count, or split the chart over two slides."
        )
    return value


class OrgChartBlock(_Strict):
    """A native org chart - reporting lines, teams and headcount as a tree of seats, drawn as vector graphics in the deck's palette and fonts."""

    type: Literal["org_chart"]
    nodes: Annotated[list[OrgChartNode], BeforeValidator(_org_nodes_within_cap), Field(min_length=2)] = Field(
        description=f"the seats, up to {MAX_ORG_CHART_NODES}; each names its manager, one root (or the roots listed)"
    )
    orientation: OrgOrientation = Field(default="top_down", description="radial = a leadership wheel around the root")
    style: OrgChartStyle = Field(
        default="cards", description="hybrid = people as cards over their teams as bubbles; bubbles = every seat a circle"
    )
    depth_limit: OrgDepth | None = Field(
        default=None, description="levels drawn, the root as 1; deeper seats roll up into a '+N' on the last drawn"
    )
    collapse_below: OrgDepth | None = Field(
        default=None, description="from this level on, a manager's reports fold into one group card with their count"
    )
    expand: Annotated[list[NodeRef], Field(max_length=MAX_ORG_CHART_EXPAND)] = Field(
        default_factory=list, description="ids whose reports stay open whatever collapses"
    )
    show_counts: bool = Field(
        default=False, description="the roll-up headcount under each manager, computed from the tree (+N open / +N planned)"
    )
    count_mode: OrgCountMode = Field(
        default="current", description="current = filled seats; target = with open and planned; both = '14 -> 47'"
    )
    group_by_branch: bool = Field(default=False, description="one tinted zone and tone per top-level branch")
    legend: OrgLegend = Field(default="auto", description="auto = the kinds drawn; custom = legend_items")
    legend_items: Annotated[list[OrgChartLegendItem], Field(max_length=MAX_ORG_CHART_LEGEND_ITEMS)] = Field(
        default_factory=list, description="the entries of a custom legend"
    )
    roots: Annotated[list[NodeRef], Field(max_length=MAX_ORG_CHART_ROOTS)] = Field(
        default_factory=list, description="several roots side by side, only when listed here"
    )
    size: MediaSize = "md"
    fill: bool = False
    label_size: Size = Field(default="md", description="the type of the names and roles")
    photo_size: Size = Field(default="md", description="the photo / initials tile")
    show_dotted: bool = Field(default=True, description="draw the dotted_to lines")
    spine: OrgSpine = Field(
        default="auto", description="leaves stacked in one column under a rail: when a level is too wide, always, never"
    )
    caption: Markup | None = None

    @model_validator(mode="after")
    def _is_one_tree(self) -> OrgChartBlock:
        ids = [node.id for node in self.nodes]
        duplicates = sorted({node_id for node_id in ids if ids.count(node_id) > 1})
        if duplicates:
            raise ValueError(f"node ids must be unique: {', '.join(duplicates)}.")
        known = set(ids)
        by_id = {node.id: node for node in self.nodes}
        for node in self.nodes:
            if node.manager is not None and node.manager not in known:
                raise ValueError(f"node {node.id!r}: manager {node.manager!r} names no node (nodes: {', '.join(ids)}).")
            for other in node.dotted_to:
                if other not in known:
                    raise ValueError(f"node {node.id!r}: dotted_to {other!r} names no node.")
        cycle = _org_cycle(by_id)
        if cycle:
            raise ValueError(
                f"nodes {' -> '.join(cycle)} report to one another in a cycle: an org chart is a tree, "
                "so one of them reports outside it, or is a root."
            )
        managerless = [node.id for node in self.nodes if node.manager is None]
        if self.roots:
            for root in self.roots:
                if root not in known:
                    raise ValueError(f"roots names no node: {root!r}.")
                if by_id[root].manager is not None:
                    raise ValueError(f"node {root!r} is listed in roots and reports to {by_id[root].manager!r}.")
            missing = [node_id for node_id in managerless if node_id not in self.roots]
            if missing:
                raise ValueError(
                    f"node {missing[0]!r} has no manager and is not in roots: give it a manager, or list it."
                )
        elif len(managerless) > 1:
            raise ValueError(
                f"nodes {', '.join(repr(node_id) for node_id in managerless)} have no manager: an org chart has one "
                "root - give them a manager, or list them in roots to draw several trees side by side."
            )
        managers = {node.manager for node in self.nodes if node.manager is not None}
        for node in self.nodes:
            if node.collapsed and node.id not in managers:
                raise ValueError(f"node {node.id!r} is collapsed and has no reports: there is nothing to fold.")
        for node_id in self.expand:
            if node_id not in known:
                raise ValueError(f"expand names no node: {node_id!r}.")
        if self.legend == "custom" and not self.legend_items:
            raise ValueError('legend "custom" needs legend_items: the entries to draw.')
        if self.legend_items and self.legend != "custom":
            raise ValueError('legend_items are drawn by legend "custom" only: set it, or drop them.')
        return self


def _org_cycle(by_id: Mapping[str, OrgChartNode]) -> list[str]:
    """The first reporting cycle among *by_id*, as the ids that form it (the first one repeated), else ``[]``."""
    settled: set[str] = set()
    for start in by_id:
        path: list[str] = []
        on_path: set[str] = set()
        current: str | None = start
        while current is not None and current not in settled:
            if current in on_path:
                loop = path[path.index(current) :]
                return [*loop, current]
            path.append(current)
            on_path.add(current)
            parent = by_id.get(current)
            current = parent.manager if parent is not None else None
        settled.update(path)
    return []


def architecture_reads_as_org_chart(block: object) -> bool:
    """Is *block* an ``architecture`` drawn as a reporting tree: one node across the top row, the rest one row under it, every tie without arrows?

    A legacy shape: before the ``org_chart`` block, a team slide was a band
    over a row of cards tied by plain lines. A reader, never a refusal - the
    stored deck still validates; the tools and the layout check point it at
    ``org_chart``.
    """
    if not isinstance(block, dict) or block.get("type") != "architecture":
        return False
    nodes = [node for node in block.get("nodes") or [] if isinstance(node, dict)]
    edges = [edge for edge in block.get("edges") or [] if isinstance(edge, dict)]
    if len(nodes) < 3 or not edges:
        return False
    top_row = min(int(node.get("row") or 1) for node in nodes)
    tops = [node for node in nodes if int(node.get("row") or 1) == top_row]
    if len(tops) != 1:
        return False
    top = tops[0]
    others = [node for node in nodes if node is not top]
    if any(int(node.get("row") or 1) != top_row + 1 for node in others):
        return False
    cols = [int(node.get("col") or 1) for node in others]
    spans = [int(node.get("col") or 1) + int(node.get("col_span") or 1) - 1 for node in others]
    top_first = int(top.get("col") or 1)
    top_last = top_first + int(top.get("col_span") or 1) - 1
    if top_first > min(cols) or top_last < max(spans):
        return False
    return all(edge.get("arrows") == "none" for edge in edges)


ORG_CHART_HINT = (
    "reporting lines, teams and headcount are an org_chart block (each seat names its manager), "
    "never an architecture block, stacked pills or a drawn picture"
)


def org_chart_hints(spec: object) -> list[tuple[int, str]]:
    """``(0-based slide index, sentence)`` for every architecture block of *spec* drawn as a reporting tree."""
    hints: list[tuple[int, str]] = []
    slides = spec.get("slides") if isinstance(spec, dict) else None
    for index, slide in enumerate(slides if isinstance(slides, list) else []):
        found = [block for block in _slide_blocks(slide) if architecture_reads_as_org_chart(block)]
        if found:
            hints.append(
                (
                    index,
                    "an architecture block draws a reporting tree (one node over a row, ties with no arrows): "
                    f"{ORG_CHART_HINT}.",
                )
            )
    return hints


def org_chart_hint_note(spec: object, *, slide_ids: Iterable[str] | None = None) -> str:
    """The validation hint a deck tool's result carries for :func:`org_chart_hints` ("" when there is none).

    *slide_ids* narrows it to the slides an edit wrote: a surgical edit is not
    told about the slides it did not touch.
    """
    hints = org_chart_hints(spec)
    if slide_ids is not None:
        wanted = set(slide_ids)
        slides = spec.get("slides") if isinstance(spec, dict) else []
        hints = [
            (index, text)
            for index, text in hints
            if isinstance(slides[index], dict) and slides[index].get("id") in wanted
        ]
    if not hints:
        return ""
    numbers = ", ".join(str(index + 1) for index, _text in hints)
    return f"Hint ({unit_noun(spec if isinstance(spec, dict) else None, len(hints))} {numbers}): {hints[0][1]}"


def _slide_blocks(slide: object) -> list[dict]:
    """Every block a slide draws, at any depth (columns, callout children, comparison sides, panels, footer)."""
    found: list[dict] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("type"), str):
                found.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    if isinstance(slide, dict):
        walk({key: value for key, value in slide.items() if key not in ("notes", "title", "subtitle")})
    return found


MAX_POLL_QUESTIONS = 5
MAX_POLL_OPTIONS = 6
MAX_POLL_WORDS = 3
PollQuestionKind = Literal["choice", "quiz", "word_cloud", "rating"]
PollShowResults = Literal["live", "after_lock"]
PollLayout = Literal["active", "all"]
PollControls = Literal["hover", "none"]
PollReveal = Literal["manual", "live", "after_votes", "after_seconds", "never"]
#: ``reveal: after_seconds`` without ``reveal_after_seconds`` (xtm-one #10411).
DEFAULT_POLL_REVEAL_SECONDS = 20
#: ``reveal: after_votes`` without ``reveal_after_votes`` (xtm-one #10411, UX round 1 B2).
DEFAULT_POLL_REVEAL_VOTES = 10
#: The number each conditional ``reveal`` reads; dropped on any other mode.
_POLL_REVEAL_NUMBERS = {"reveal_after_votes": "after_votes", "reveal_after_seconds": "after_seconds"}
PollId = Annotated[str, Field(pattern=ARCHITECTURE_ID_PATTERN, max_length=32)]


class PollQuestion(_Strict):
    """One question of a live ``poll``: the audience answers it from ``/v/CODE``."""

    id: PollId = Field(description="stable within the poll; votes are keyed by it")
    kind: PollQuestionKind = "choice"
    prompt: Label
    options: Annotated[list[Label], Field(max_length=MAX_POLL_OPTIONS)] = Field(
        default_factory=list, description="2-6 for choice and quiz, none otherwise"
    )
    correct: Annotated[int | None, Field(ge=0, le=MAX_POLL_OPTIONS - 1)] = Field(
        default=None, description="quiz only: index of the right option"
    )
    max_words: Annotated[int, Field(ge=1, le=MAX_POLL_WORDS)] = Field(
        default=1, description="word_cloud only: words one voter may send"
    )

    @model_validator(mode="after")
    def _shape_matches_kind(self) -> PollQuestion:
        if self.kind in ("choice", "quiz"):
            if len(self.options) < 2:
                raise ValueError(f"a {self.kind} question needs 2-{MAX_POLL_OPTIONS} options.")
        elif self.options:
            raise ValueError(f"a {self.kind} question takes no options.")
        if self.kind == "quiz":
            if self.correct is None or self.correct >= len(self.options):
                raise ValueError("a quiz question needs correct: the index of one of its options.")
        elif self.correct is not None:
            raise ValueError("correct is for quiz questions only.")
        return self


# A poll's mode (xtm-one #10235) is its layout named by how it runs: ``guided``
# steps one question at a time (``active``), ``autonomous`` opens every question
# at once and needs no presenter action (``all``).
POLL_MODE_LAYOUTS: dict[str, str] = {"guided": "active", "autonomous": "all"}


class PollBlock(_Strict):
    """A live audience poll: short link + QR, results drawn on the slide as votes arrive."""

    type: Literal["poll"]
    id: PollId = Field(description="stable within the deck; the poll's short code and votes hang on it")
    questions: Annotated[list[PollQuestion], Field(min_length=1, max_length=MAX_POLL_QUESTIONS)]
    show_results: PollShowResults = Field(default="live", description="after_lock: bars only once voting is locked")
    show_qr: bool = True
    layout: PollLayout = Field(default="active", description="all: every question side by side, the link on top")
    # Hands-free (xtm-one #10315): no presenter bar, toast or key on the slide; quiz answer marked from the start.
    controls: PollControls = Field(default="hover", description="none: no presenter control")
    reveal: PollReveal = Field(default="manual", description="live: quiz answer shown")
    # Conditions that need no presenter (xtm-one #10411): per question, from its own votes.
    reveal_after_votes: Annotated[int | None, Field(ge=1, le=500)] = None
    reveal_after_seconds: Annotated[int | None, Field(ge=1, le=3600)] = None

    @model_validator(mode="before")
    @classmethod
    def _mode_is_the_layout(cls, data: object) -> object:
        """``mode`` (xtm-one #10235) names the layout by its use: guided = active, autonomous = all.

        One stored field (``layout``), so the editor, the tool and the votes never disagree;
        ``mode`` is accepted on input and folded into it.
        """
        if isinstance(data, dict) and "mode" in data:
            data = dict(data)
            mode = data.pop("mode")
            if mode not in POLL_MODE_LAYOUTS:
                raise ValueError("mode must be guided or autonomous.")
            data["layout"] = POLL_MODE_LAYOUTS[mode]
        return data

    @model_validator(mode="before")
    @classmethod
    def _reveal_number_follows_the_mode(cls, data: object) -> object:
        """A reveal number left from another mode is dropped, never refused (xtm-one #10411, UX B2).

        Switching ``reveal`` from ``after_votes`` to ``after_seconds`` in the editor keeps the
        old count in the draft: the save drops what the new mode does not read.
        """
        if not isinstance(data, dict):
            return data
        stale = [key for key, mode in _POLL_REVEAL_NUMBERS.items() if key in data and data.get("reveal") != mode]
        if not stale:
            return data
        return {key: value for key, value in data.items() if key not in stale}

    @model_validator(mode="after")
    def _question_ids_unique(self) -> PollBlock:
        ids = [question.id for question in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("poll question ids must be unique.")
        return self


def poll_blocks(spec: object) -> list[dict]:
    """Every ``poll`` block of a deck, in deck order (a reader: anything else yields nothing)."""
    found: list[dict] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "poll" and isinstance(node.get("id"), str):
                found.append(node)
                return
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk((spec or {}).get("slides") if isinstance(spec, dict) else None)
    return found


class SpacerBlock(_Strict):
    type: Literal["spacer"]
    size: SpacerSize = "md"
    grow: bool = Field(
        default=False,
        description="take the remaining height (pushes what follows to the bottom)",
    )


class DividerBlock(_Strict):
    type: Literal["divider"]
    label: Label | None = None
    tone: Tone | None = None


MapLabel = _markup(MAX_POSITIONING_MAP_LABEL_CHARS)
MapAxisText = _markup(MAX_POSITIONING_MAP_AXIS_CHARS)
MapCoordinate = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
MapCalloutText = _markup(MAX_POSITIONING_MAP_CALLOUT_TEXT_CHARS)


class MapAxis(_Strict):
    """One axis of a positioning map: what it measures and what its two ends mean."""

    title: MapAxisText = Field(description="what the axis measures, e.g. Coverage")
    low_label: MapAxisText | None = Field(default=None, description="the low end (0), e.g. Single stage")
    high_label: MapAxisText | None = Field(default=None, description="the high end (100), e.g. Whole loop")


class MapQuadrant(_Strict):
    """A quadrant's name, drawn in its outer corner over a faint tint."""

    label: MapAxisText
    tone: Tone | None = Field(default=None, description="the tint; default neutral")
    color: PaletteColor | None = Field(default=None, description="in place of tone, a template palette colour")

    @model_validator(mode="after")
    def _one_colour(self) -> MapQuadrant:
        if self.tone and self.color:
            raise ValueError("give a quadrant a tone OR a color, not both.")
        return self


class MapQuadrants(_Strict):
    """The four quadrants by position; any may be left out."""

    top_left: MapQuadrant | None = None
    top_right: MapQuadrant | None = None
    bottom_left: MapQuadrant | None = None
    bottom_right: MapQuadrant | None = None


class MapItem(_Strict):
    """One mark on the map: a vendor, a product, an option, placed at its x / y."""

    label: MapLabel = Field(description="a short plain name drawn under the mark")
    sublabel: MapLabel | None = Field(default=None, description="one short qualifier under the label")
    x: MapCoordinate = Field(description="0 = the x axis' low end, 100 = its high end")
    y: MapCoordinate = Field(description="0 = the y axis' low end (bottom), 100 = its high end (top)")
    logo_file_id: Annotated[str | None, Field(max_length=36)] = Field(
        default=None, description="a template asset (the vendor's emblem), drawn at one size for every item"
    )
    icon: Icon | None = Field(default=None, description="in place of a logo; neither = a dot")
    emphasis: MapEmphasis = Field(
        default="normal", description="highlight = the one the slide is about (larger, accent glow); ghost = dimmed"
    )
    note: Annotated[str | None, Field(max_length=MAX_POSITIONING_MAP_NOTE_CHARS)] = Field(
        default=None, description="why it sits there, one sentence, shown on hover"
    )

    @model_validator(mode="after")
    def _one_mark(self) -> MapItem:
        if self.icon and self.logo_file_id:
            raise ValueError("give an item an icon OR a logo_file_id, not both: the logo takes the icon's place.")
        return self


class MapCallout(_Strict):
    """One differentiator drawn with the map: what puts the highlight where it is."""

    title: MapLabel
    text: MapCalloutText | None = None


class PositioningMapBlock(_Strict):
    """A native positioning map (competitive landscape, 2x2): two real axes, four named quadrants, items placed by x / y, every mark at one size."""

    type: Literal["positioning_map"]
    title: MapAxisText | None = Field(default=None, description="drawn at the top of the frame, like a chart title")
    x_axis: MapAxis
    y_axis: MapAxis
    axes: MapAxes = Field(
        default="center", description="center = the axes cross in the middle (a 2x2); edges = along the left and bottom"
    )
    quadrants: MapQuadrants | None = None
    items: Annotated[list[MapItem], Field(min_length=1, max_length=MAX_POSITIONING_MAP_ITEMS)] = Field(
        description="labels are placed so they never overlap"
    )
    callouts: Annotated[list[MapCallout], Field(max_length=MAX_POSITIONING_MAP_CALLOUTS)] = Field(
        default_factory=list, description="1-4 differentiators, drawn with the map"
    )
    callouts_position: MapCalloutsPosition = "below"
    caption: Markup | None = None
    size: MediaSize = "md"
    fill: bool = False


_ATOMIC_BLOCK_MODELS: tuple[type[BaseModel], ...] = (
    TextBlock,
    HeadingBlock,
    QuoteBlock,
    ListBlock,
    MetricBlock,
    MetricsGridBlock,
    TableBlock,
    ChartBlock,
    KeyValueBlock,
    BadgesBlock,
    FeatureListBlock,
    PhaseCardBlock,
    NumberedCardsBlock,
    PricingTiersBlock,
    PersonasBlock,
    ProfilesBlock,
    TimelineBlock,
    ProcessFlowBlock,
    SectionHeaderBlock,
    BannerBlock,
    ChallengeAnswerBlock,
    ImageBlock,
    VideoBlock,
    LogoGridBlock,
    LogoWallBlock,
    FormulaBlock,
    ConfigCardsBlock,
    DiagramBlock,
    ArchitectureBlock,
    OrgChartBlock,
    PositioningMapBlock,
    PollBlock,
    SpacerBlock,
    DividerBlock,
)

AtomicBlock = Annotated[
    Union[  # noqa: UP007 - Pydantic needs the explicit Union for the discriminator
        TextBlock,
        HeadingBlock,
        QuoteBlock,
        ListBlock,
        MetricBlock,
        MetricsGridBlock,
        TableBlock,
        ChartBlock,
        KeyValueBlock,
        BadgesBlock,
        FeatureListBlock,
        PhaseCardBlock,
        NumberedCardsBlock,
        PricingTiersBlock,
        PersonasBlock,
        ProfilesBlock,
        TimelineBlock,
        ProcessFlowBlock,
        SectionHeaderBlock,
        BannerBlock,
        ChallengeAnswerBlock,
        ImageBlock,
        VideoBlock,
        LogoGridBlock,
        LogoWallBlock,
        FormulaBlock,
        ConfigCardsBlock,
        DiagramBlock,
        ArchitectureBlock,
        OrgChartBlock,
        PositioningMapBlock,
        PollBlock,
        SpacerBlock,
        DividerBlock,
    ],
    Field(discriminator="type"),
]


# ── Blocks: containers ───────────────────────────────────────────────────────


class CalloutBlock(_Strict):
    """A card with an optional icon, title, copy and child blocks.

    The tone colours the icon and the title; the box is the ``variant``'s -
    ``tinted`` washes it in the tone with a bar on its left edge (the
    historical look, the default), ``card`` / ``outline`` / ``plain`` never
    tint it - so a card callout in the accent tone is a white card with an
    accent header, not a blue box. ``header`` moves the icon out of its
    left-hand column: with the title on one line (``inline``) or above it
    (``stacked``), everything under them uses the callout's full width.
    """

    type: Literal["callout"]
    tone: Tone = "accent"
    icon: Icon | None = None
    title: Markup | None = None
    subtitle: Markup | None = None
    content: LongMarkup | None = None
    children: Annotated[
        list[AtomicBlock],
        Field(default_factory=list, max_length=MAX_BLOCKS_PER_CONTAINER),
    ] = Field(description="atomic blocks only (no columns/callout/comparison)")
    density: Density = "normal"
    fill: bool = Field(default=False, description=CARD_FILL_DESCRIPTION)
    variant: CalloutVariant = Field(
        default="tinted",
        description="the box (tone colours icon and title only): tinted = tone wash; card = surface, line, shadow; "
        "outline = line; plain = none",
    )
    header: CalloutHeader | None = Field(
        default=None,
        description="inline = icon + title on one line, stacked = icon over title, none = no icon; the rest full "
        "width below. Omit: icon in a left column",
    )
    accent_bar: bool | None = Field(default=None, description="left tone bar; omit = on when tinted")
    size: Size | None = Field(default=None, description="sets icon_size and title_size where they are unset")
    icon_size: Size | None = Field(default=None, description="sm 20 / md 28 / lg 36 px")
    title_size: Size | None = None
    padding: Size | None = None
    children_gap: Size | None = None

    @model_validator(mode="after")
    def _has_content(self) -> CalloutBlock:
        if not (self.title or self.content or self.children):
            raise ValueError("a callout needs a title, content or children.")
        return self


class ComparisonSideBlock(_Strict):
    label: Markup
    icon: Icon | None = None
    tone: Tone | None = Field(default=None, description="default: left muted, right accent")
    blocks: Annotated[
        list[AtomicBlock], Field(min_length=1, max_length=MAX_BLOCKS_PER_CONTAINER)
    ]


class ComparisonBlock(_Strict):
    """Two labelled panels side by side (before/after, option A/B...)."""

    type: Literal["comparison"]
    left: ComparisonSideBlock
    right: ComparisonSideBlock
    equal_height: bool = True


LeafBlock = Annotated[
    Union[  # noqa: UP007 - Pydantic needs the explicit Union for the discriminator
        TextBlock,
        HeadingBlock,
        QuoteBlock,
        ListBlock,
        MetricBlock,
        MetricsGridBlock,
        TableBlock,
        ChartBlock,
        KeyValueBlock,
        BadgesBlock,
        FeatureListBlock,
        PhaseCardBlock,
        NumberedCardsBlock,
        PricingTiersBlock,
        PersonasBlock,
        ProfilesBlock,
        TimelineBlock,
        ProcessFlowBlock,
        SectionHeaderBlock,
        BannerBlock,
        ChallengeAnswerBlock,
        ImageBlock,
        VideoBlock,
        LogoGridBlock,
        LogoWallBlock,
        FormulaBlock,
        ConfigCardsBlock,
        DiagramBlock,
        ArchitectureBlock,
        OrgChartBlock,
        PositioningMapBlock,
        PollBlock,
        SpacerBlock,
        DividerBlock,
        CalloutBlock,
        ComparisonBlock,
    ],
    Field(discriminator="type"),
]


class ColumnsBlock(_Strict):
    """Side-by-side columns of leaf blocks (no nested columns)."""

    type: Literal["columns"]
    columns: Annotated[
        list[Annotated[list[LeafBlock], Field(min_length=1, max_length=MAX_BLOCKS_PER_COLUMN)]],
        Field(min_length=2, max_length=MAX_COLUMNS),
    ]
    ratio: Ratio = Field(default="equal", description="wide_* needs exactly 2 columns")
    equal_height: bool = False
    equal_rows: bool = Field(default=False, description=EQUAL_ROWS_DESCRIPTION)
    align: VAlign = "start"
    gap: Size = "md"

    @model_validator(mode="after")
    def _ratio_needs_two_columns(self) -> ColumnsBlock:
        if self.ratio != "equal" and len(self.columns) != 2:
            raise ValueError("ratio wide_left/wide_right requires exactly 2 columns.")
        return self


Block = Annotated[
    Union[  # noqa: UP007 - Pydantic needs the explicit Union for the discriminator
        TextBlock,
        HeadingBlock,
        QuoteBlock,
        ListBlock,
        MetricBlock,
        MetricsGridBlock,
        TableBlock,
        ChartBlock,
        KeyValueBlock,
        BadgesBlock,
        FeatureListBlock,
        PhaseCardBlock,
        NumberedCardsBlock,
        PricingTiersBlock,
        PersonasBlock,
        ProfilesBlock,
        TimelineBlock,
        ProcessFlowBlock,
        SectionHeaderBlock,
        BannerBlock,
        ChallengeAnswerBlock,
        ImageBlock,
        VideoBlock,
        LogoGridBlock,
        LogoWallBlock,
        FormulaBlock,
        ConfigCardsBlock,
        DiagramBlock,
        ArchitectureBlock,
        OrgChartBlock,
        PositioningMapBlock,
        PollBlock,
        SpacerBlock,
        DividerBlock,
        CalloutBlock,
        ComparisonBlock,
        ColumnsBlock,
    ],
    Field(discriminator="type"),
]

_BLOCK_MODELS_BY_TAG: dict[str, type[BaseModel]] = {
    typing.get_args(model.model_fields["type"].annotation)[0]: model
    for model in (*_ATOMIC_BLOCK_MODELS, CalloutBlock, ComparisonBlock, ColumnsBlock)
}
assert set(_BLOCK_MODELS_BY_TAG) == set(BLOCK_TYPES), (  # noqa: S101 - import-time contract
    set(_BLOCK_MODELS_BY_TAG) ^ set(BLOCK_TYPES)
)
#: Catalog order (``BLOCK_TYPES``) — what the reference and the seed list.
_BLOCK_MODELS: dict[str, type[BaseModel]] = {
    block: _BLOCK_MODELS_BY_TAG[block] for block in BLOCK_TYPES
}
# Every grid block listed as taking ``fill`` carries the field, with
# the wording its renderer keeps (the bounded row spread; the card stretch on
# ``numbered_cards``), and off by default - the list is what the
# guidance and the tests read, the models are what the validator accepts.
assert all(  # noqa: S101 - import-time contract
    _BLOCK_MODELS_BY_TAG[block].model_fields["fill"].default is False
    and _BLOCK_MODELS_BY_TAG[block].model_fields["fill"].description == fill_description(block)
    for block in FILL_BLOCK_TYPES
), FILL_BLOCK_TYPES


# ── Deck chrome ──────────────────────────────────────────────────────────────

#: Where the brand logo sits on the OPENING and CLOSING slides - the
#: two whose bottom row carries nothing else (no footer bar; the title slide
#: has no number either), so the corner is a choice there, not a necessity.
#: ``auto`` follows the slide's own composition: the title slide's ``align``
#: (bottom-left under a left-aligned headline, on the slide axis under a
#: centred one) and the closing slide's centred layout. Body and section
#: slides keep the corner whatever the value: the footer bar and the slide
#: number share their row, and the mark must not move between two body
#: slides. Resolved per slide by ``resolveLogoPosition`` (``logo-position.ts``,
#: drawn by ``chrome.tsx``); the frontend ``DeckLogoPosition`` union mirrors
#: this literal.
LogoPosition = Literal["auto", "start", "center", "end"]
LOGO_POSITIONS: tuple[str, ...] = typing.get_args(LogoPosition)


class Chrome(_Strict):
    """Persistent deck furniture drawn around the slide content."""

    show_logo: bool = Field(default=True, description="brand logo from the template")
    logo_position: LogoPosition = Field(
        default="auto",
        description=(
            "logo placement on the title and closing slides: auto (follows the slide's "
            "alignment), start, center or end - body slides keep the corner"
        ),
    )
    show_footer: bool = Field(default=True, description="footer bar on content slides")
    show_slide_numbers: bool = True
    event_label: Annotated[str, Field(min_length=1, max_length=MAX_LABEL_CHARS)] | None = (
        Field(default=None, description='e.g. "All Hands - Sept 2026"')
    )
    footer_text: Annotated[str, Field(min_length=1, max_length=MAX_LABEL_CHARS)] | None = (
        # The v1 footer draws the event label and this text only. A deck whose
        # TEMPLATE declares a footer pattern draws that pattern
        # instead, filled from the title, the opening subtitle, the event
        # label and ``fields`` - this text is then not drawn.
        Field(default=None, description="footer bar text, after the event label (free deck)")
    )
    #: Values for the placeholders the deck's TEMPLATE declares in its footer /
    #: header pattern, one per deck: ``{"classification": "Confidential"}``
    #: fills ``{{classification}}`` on every slide. Template-agnostic here (a
    #: relink keeps the values; the tool reports the ones no variable reads);
    #: keys follow the variable grammar, blanks are dropped, an empty map is
    #: absent.
    fields: dict[str, str] | None = Field(
        default=None,
        description="template header/footer variables by key, e.g. {\"classification\": \"Confidential\"}",
    )

    @field_validator("fields", mode="before")
    @classmethod
    def _clean_fields(cls, value: object) -> dict[str, str] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("chrome.fields must be an object mapping a variable key to its text")
        cleaned: dict[str, str] = {}
        for raw_key, raw_value in value.items():
            # The same spelling the update_deck merge and the reader use
            # (presentation_chrome.canonical_field_key): one normaliser, so
            # the key a merge removes is the key this validator would store.
            key = canonical_field_key(raw_key)
            if not VARIABLE_KEY_RE.match(key):
                raise ValueError(
                    f"chrome.fields key {raw_key!r} is not a variable key (lowercase letters, "
                    "digits and underscores, up to 32 characters, as the template declares it)"
                )
            if raw_value is None:
                continue
            if not isinstance(raw_value, str):
                raise ValueError(f"chrome.fields.{key} must be text, got {type(raw_value).__name__}")
            text = raw_value.strip()
            if not text:
                continue
            if len(text) > MAX_FIELD_VALUE_CHARS:
                raise ValueError(
                    f"chrome.fields.{key} is {len(text)} characters; the maximum is "
                    f"{MAX_FIELD_VALUE_CHARS} - a footer value is one short line"
                )
            cleaned[key] = text
        if len(cleaned) > MAX_DECK_FIELDS:
            raise ValueError(f"chrome.fields carries {len(cleaned)} values; the maximum is {MAX_DECK_FIELDS}")
        return cleaned or None


# ── Slide layouts ────────────────────────────────────────────────────────────


class _SlideBase(_Strict):
    """Fields every layout shares."""

    #: Stable identity, assigned by the server when omitted. Keep it
    #: when re-submitting a deck so unchanged slides keep their React key.
    id: SlideId | None = Field(
        default=None, description="server-assigned; keep it when editing an existing slide"
    )
    #: Speaker notes: what the presenter says over the slide. Shown in the
    #: native speaker view (S key) and the notes handout, written as plain
    #: text into the PowerPoint notes pane; never rendered on the slide.
    #: Deliberately NOT markup-validated - a stray ``*`` in something only the
    #: presenter reads must never refuse a deck (``presentation_notes``).
    notes: str | None = Field(
        default=None, max_length=MAX_NOTES_CHARS, description="speaker notes"
    )
    background_image_file_id: FileId
    #: The colour scheme THIS slide is read in. ``None`` inherits the
    #: deck theme - today's rendering, byte for byte. A value renders the slide
    #: in that scheme whatever the deck theme is, and a deck theme change never
    #: touches it: only an explicit null (``update_slide {theme: null}``, the
    #: editor's Inherit) hands the slide back to the deck. Normalised like the
    #: deck theme (``filigran`` reads as ``dark``); held to the deck's template
    #: by the write paths like the deck theme is.
    theme: Annotated[SlideTheme, BeforeValidator(normalize_theme)] | None = Field(
        default=None,
        description=(
            "this slide's own colour scheme; omit to inherit the deck theme. If set it "
            "always wins over the deck theme, and a deck theme change leaves it alone - "
            "null (or the editor's Inherit) removes it"
        ),
    )
    #: The scrim between ``background_image_file_id`` and the content,
    #: drawn in the slide's scheme - the slide's PAPER colour at a soft or a
    #: strong opacity, heavier at the bottom where the text sits - so a
    #: headline stays readable over a photograph (the ink was chosen against
    #: that paper). Nothing is drawn without a background image.
    overlay: Overlay = Field(
        default="none",
        description="scrim over background_image_file_id, in the slide's scheme: none / soft / strong",
    )
    #: The slide's own palette inside its template; ``None`` keeps the
    #: template's. Applies in both schemes; the chrome (logo, footer) stays the
    #: template's.
    palette: SlidePalette | None = Field(
        default=None,
        description=(
            "xtm_one = this slide's accent, block gradients and chart series from the XTM One "
            "palette (a slide about XTM One); omit to keep the template palette"
        ),
    )


class Speaker(_Strict):
    name: Label
    title: Markup | None = None
    credentials: Annotated[list[Label], Field(default_factory=list, max_length=MAX_CREDENTIALS)]


class CoverSpeaker(Speaker):
    """One speaker of a title slide: the closing slide's speaker plus a portrait."""

    photo_file_id: FileId = Field(
        default=None,
        description="platform file id of the speaker's portrait (get_template role=people_photo, or an uploaded photo)",
    )


def fold_title_speaker(slide: Any) -> Any:
    """A title slide's one-speaker form folded into its ``speakers`` list.

    Before ``speakers``, a title slide named ONE speaker (``speaker``) and its
    portrait apart (``speaker_photo_file_id``). That form stays accepted - the
    decks stored with it, and an agent that still writes it - and always reads
    as the lead speaker: ``speakers[0]``, its portrait kept when the write
    names none. A portrait with no speaker to wear it drew nothing and is
    dropped. Copy-on-write: anything that is not a title slide carrying the old
    keys comes back as the same object. Run by the model on every write and by
    the spec column's read hook (``fold_title_speakers``), so no reader meets
    the old form.
    """
    if not isinstance(slide, Mapping) or slide.get("layout") != "title":
        return slide
    if "speaker" not in slide and "speaker_photo_file_id" not in slide:
        return slide
    out = dict(slide)
    speaker = out.pop("speaker", None)
    photo = out.pop("speaker_photo_file_id", None)
    listed = list(out["speakers"]) if isinstance(out.get("speakers"), list) else []
    if isinstance(speaker, str):
        speaker = {"name": speaker}
    if isinstance(speaker, Mapping):
        lead = dict(speaker)
        kept = listed[0].get("photo_file_id") if listed and isinstance(listed[0], Mapping) else None
        if photo or kept:
            lead.setdefault("photo_file_id", photo or kept)
        listed = [lead, *listed[1:]]
    elif photo and listed and isinstance(listed[0], Mapping):
        listed[0] = {**listed[0], "photo_file_id": photo}
    if listed:
        out["speakers"] = listed
    else:
        out.pop("speakers", None)
    return out


class TitleSlide(_SlideBase):
    """Opening slide: title, subtitle, speakers, event and an icon strip."""

    layout: Literal["title"]
    title: TitleMarkup
    subtitle: TitleMarkup | None = None
    footer: Markup | None = None
    event_label: Label | None = Field(default=None, description="conference / date line")
    speakers: Annotated[list[CoverSpeaker], Field(max_length=MAX_COVER_SPEAKERS)] | None = Field(
        default=None,
        description=(
            f"who presents, in speaking order: up to {MAX_COVER_SPEAKERS} {{name, title, credentials[], "
            "photo_file_id}} - a keynote's one, a co-presented talk's two, a panel's three or four"
        ),
    )
    icon_strip: Annotated[list[Icon], Field(default_factory=list, max_length=MAX_ICON_STRIP)] = (
        Field(description="decorative row of icons under the headline")
    )
    align: Literal["start", "center"] = "start"
    #: The cover's backdrop, drawn in the slide's resolved scheme:
    #: ``plain`` is the flat paper every deck had; ``gradient`` the brand
    #: gradient washed over the paper with the template's mark as a faint
    #: watermark (the PowerPoint cover); ``split`` a text panel next to a
    #: gradient panel that hosts the background image; ``spotlight`` a radial
    #: accent glow behind the headline with two thin arcs in the corner;
    #: ``mosaic`` pixel blocks clustered in the corners the text leaves free;
    #: ``cascade`` / ``horizon`` / ``pillar`` the same tiles stepping down from
    #: the top-right corner, along the bottom edge, down the right edge. The
    #: four tiled covers are a template's own (``TEMPLATE_HEROES``).
    hero: Hero = Field(
        default="plain",
        description=(
            "cover backdrop in the slide's scheme: plain (flat), gradient (brand gradient "
            "wash + faint brand mark), split (text left, gradient / image panel right), "
            "spotlight (accent glow + arcs); a template's own covers, only under a template "
            "that offers them: mosaic (pixel blocks in the free corners), "
            "cascade (tiles stepping down from the top-right corner, the logo large top "
            "left, the headline low over a rule and the speaker / footer row), horizon "
            "(a skyline of tiles along the bottom edge, the logo large top left), pillar "
            "(a column of tiles down the right edge)"
        ),
    )
    #: The colour the hero is drawn in: the template's gradient, or one
    #: palette colour alone - the way a brand whose gradient runs into a
    #: secondary colour still opens on its primary.
    hero_tone: HeroTone = Field(
        default="brand",
        description=(
            "colour of the hero, from the slide's palette: brand (the template's gradient, "
            "gradient_from -> gradient_to; its six chart series for mosaic) or one colour "
            "alone - accent, gradient_from, gradient_to, info, success, warning, error, muted"
        ),
    )
    #: The PowerPoint cover's accent line - "Uncover Threats. / Take Action." -
    #: without markup in the title: the LAST line (after the last line break)
    #: is drawn in the accent tone; a one-line title accents its last word,
    #: found across the runs (`splitAccentLine` in `accent-line.ts`).
    hero_accent_word: bool = Field(
        default=False,
        description=(
            "draw the title's last line (after the last line break; the last word of a "
            "one-line title) in the accent tone"
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _one_speaker_is_the_lead(cls, data: Any) -> Any:
        """The one-speaker form (``speaker`` + ``speaker_photo_file_id``) is ``speakers[0]``."""
        return fold_title_speaker(data)


class SectionSlide(_SlideBase):
    """Chapter divider with a large watermark number."""

    layout: Literal["section"]
    number: Annotated[int, Field(ge=1, le=99)] | None = None
    label: Label | None = Field(default=None, description='e.g. "Part 2"')
    title: TitleMarkup
    subtitle: Markup | None = None
    icon: Icon | None = None


def auto_agenda_columns(count: int) -> int:
    """Columns an agenda of ``count`` items takes when the spec leaves it out.

    One column up to four items, two for five or six, the 3-column grid
    for seven to nine, and two columns of five for the ten-item
    maximum, which no 3-column grid holds in full rows. Mirrored by
    ``agendaColumns()`` in
    ``frontend/src/components/presentation/agenda-layout.ts`` - the renderer
    resolves the same rule for a spec that left ``columns`` out, and
    ``tests/test_presentation_spec_v2.py`` pins the two together.
    """
    if count <= 4:
        return 1
    if count <= 6:
        return 2
    if count <= AGENDA_GRID_MAX_ITEMS:
        return AGENDA_GRID_COLUMNS
    return 2


def auto_agenda_style(columns: int) -> str:
    """The look an agenda takes when ``style`` is left out: the grid at three
    columns, the numbered chips otherwise (mirrored by ``agendaStyle()``)."""
    return "grid" if columns == AGENDA_GRID_COLUMNS else "list"


class AgendaItem(_Strict):
    number: Annotated[int, Field(ge=1, le=99)] | None = None
    title: Markup
    description: Markup | None = None
    icon: Icon | None = None


class AgendaSlide(_SlideBase):
    layout: Literal["agenda"]
    title: TitleMarkup = "Agenda"
    subtitle: Markup | None = None
    items: Annotated[
        list[AgendaItem],
        BeforeValidator(_coerce_text_items("title")),
        Field(min_length=2, max_length=MAX_AGENDA_ITEMS),
    ]
    columns: Literal[1, 2, 3] | None = Field(
        default=None,
        description=(
            "omit for auto (1 up to 4 items, 2 for 5-6 or 10, 3 for 7-9); "
            f"3 needs {AGENDA_GRID_MIN_ITEMS}-{AGENDA_GRID_MAX_ITEMS} items"
        ),
    )
    style: AgendaStyle | None = Field(
        default=None,
        description=(
            'omit for auto ("grid" at 3 columns, "list" otherwise); grid draws '
            '"N. | TITLE" cells in accent caps with the description under, '
            "list the numbered chips"
        ),
    )
    distribute: bool = Field(
        default=True,
        description="spread the rows evenly over the body height; false packs them to the top",
    )
    flow: AgendaFlow = Field(
        default="row",
        description="fill order: row = 1 2 3 / 4 5 6, column = down the first column first",
    )
    current: Annotated[int, Field(ge=1, le=MAX_AGENDA_ITEMS)] | None = Field(
        default=None,
        description="1-based item to highlight when the agenda recurs between chapters",
    )
    footer_block: LeafBlock | None = Field(
        default=None, description="one block pinned at the bottom (callout, quote...)"
    )

    @model_validator(mode="after")
    def _current_in_range(self) -> AgendaSlide:
        if self.current is not None and self.current > len(self.items):
            raise ValueError(
                f"current={self.current} but the agenda has {len(self.items)} item(s)."
            )
        return self

    @model_validator(mode="after")
    def _grid_holds_full_rows(self) -> AgendaSlide:
        count = len(self.items)
        if self.columns == AGENDA_GRID_COLUMNS and not (
            AGENDA_GRID_MIN_ITEMS <= count <= AGENDA_GRID_MAX_ITEMS
        ):
            raise ValueError(
                f"columns={AGENDA_GRID_COLUMNS} needs {AGENDA_GRID_MIN_ITEMS} to "
                f"{AGENDA_GRID_MAX_ITEMS} items (this agenda has {count}); "
                "use columns=2 or split the agenda over two slides."
            )
        return self


def _check_bento(blocks: list[Any], columns: list[Any] | None) -> None:
    """A bento slide: one flow of 3-5 cells, 1-3 of them images, every
    cell a leaf (a nested run has no cell to be)."""
    if columns is not None:
        raise ValueError("bento lays out blocks, not columns: put the cells in blocks.")
    if not BENTO_MIN_CELLS <= len(blocks) <= BENTO_MAX_CELLS:
        raise ValueError(
            f"a bento slide has {BENTO_MIN_CELLS}-{BENTO_MAX_CELLS} blocks (one image cell "
            f"plus 2-4 text or KPI cells); got {len(blocks)}."
        )
    kinds = [getattr(block, "type", None) for block in blocks]
    # A video is the bento's media cell like a picture.
    images = kinds.count("image") + kinds.count("video")
    if not 1 <= images <= BENTO_MAX_IMAGES:
        raise ValueError(f"a bento slide carries 1-{BENTO_MAX_IMAGES} image or video blocks; got {images}.")
    nested = sorted({kind for kind in kinds if kind in CONTAINER_BLOCK_TYPES})
    if nested:
        raise ValueError(f"a bento cell is one block, not a container: {', '.join(nested)}.")


class ContentSlide(_SlideBase):
    """The workhorse: a title plus blocks, in one flow or in 2-3 columns."""

    layout: Literal["content"]
    title: TitleMarkup | None = None
    subtitle: Markup | None = None
    blocks: Annotated[
        list[Block], Field(default_factory=list, max_length=MAX_BLOCKS_PER_SLIDE)
    ] = Field(description="single-flow body; use EITHER blocks OR columns")
    columns: (
        Annotated[
            list[
                Annotated[list[LeafBlock], Field(min_length=1, max_length=MAX_BLOCKS_PER_COLUMN)]
            ],
            Field(min_length=2, max_length=3),
        ]
        | None
    ) = Field(default=None, description="2-3 columns of leaf blocks")
    ratio: Ratio = Field(default="equal", description="wide_* needs exactly 2 columns")
    vertical_align: VAlign = Field(
        default="start", description="stretch = equal-height columns"
    )
    distribute: bool = Field(
        default=False,
        description=(
            "spread the blocks evenly over the body height (free height lands between "
            "blocks, each gap up to a readable maximum); false packs them to the top. "
            "Wins over vertical_align for the run"
        ),
    )
    equal_height: bool = False
    equal_rows: bool = Field(default=False, description=EQUAL_ROWS_DESCRIPTION)
    footer_block: LeafBlock | None = Field(
        default=None, description="one block pinned at the bottom"
    )
    hide_footer: bool = Field(default=False, description="hide the chrome footer here")
    #: Bento grid (spec 3.7): the blocks are the cells of a grid that
    #: fills the body, the first image block the large cell.
    bento: bool = Field(
        default=False,
        description=(
            "bento grid: 3-5 blocks, 1-3 of them images; the first image is the large cell, "
            "the others text / KPI cells (photos fit cover, screenshots and diagrams contain)"
        ),
    )

    @model_validator(mode="after")
    def _blocks_xor_columns(self) -> ContentSlide:
        if self.columns is not None and self.blocks:
            raise ValueError("use EITHER blocks OR columns on a content slide, not both.")
        if self.columns is None and not self.blocks:
            raise ValueError("a content slide needs at least one block (or columns).")
        if self.ratio != "equal" and (self.columns is None or len(self.columns) != 2):
            raise ValueError("ratio wide_left/wide_right requires exactly 2 columns.")
        if self.bento:
            _check_bento(self.blocks, self.columns)
        return self


class RoadmapSlide(_SlideBase):
    """2-4 phase cards side by side."""

    layout: Literal["roadmap"]
    title: TitleMarkup
    subtitle: Markup | None = None
    phases: Annotated[list[Phase], Field(min_length=2, max_length=MAX_ROADMAP_PHASES)]
    footer_block: LeafBlock | None = None
    hide_footer: bool = False


class TakeawaysSlide(_SlideBase):
    """Numbered key-takeaway cards."""

    layout: Literal["takeaways"]
    title: TitleMarkup = "Key takeaways"
    subtitle: Markup | None = None
    items: Annotated[
        list[NumberedCard],
        BeforeValidator(_coerce_text_items("title")),
        Field(min_length=2, max_length=MAX_TAKEAWAYS),
    ]
    columns: Literal[1, 2, 3] | None = Field(default=None, description="omit for auto")
    footer_block: LeafBlock | None = None
    hide_footer: bool = False


#: What a dashboard panel may hold: a FIGURE. A chart, a table, a metric or an
#: org chart (a team's headcount beside its numbers) - not prose, not a
#: callout; a dashboard that needs a paragraph is a ``content`` slide with a
#: ``chart`` block in its columns.
_PANEL_BLOCK_MODELS: tuple[type[BaseModel], ...] = (ChartBlock, TableBlock, MetricBlock, MetricsGridBlock, OrgChartBlock)
PANEL_BLOCK_TYPES: tuple[str, ...] = tuple(
    typing.get_args(model.model_fields["type"].annotation)[0] for model in _PANEL_BLOCK_MODELS
)
PanelBlock = Annotated[
    Union[ChartBlock, TableBlock, MetricBlock, MetricsGridBlock, OrgChartBlock],  # noqa: UP007 - discriminator
    Field(discriminator="type"),
]
assert set(PANEL_BLOCK_TYPES) <= set(BLOCK_TYPES)  # noqa: S101 - import-time contract


#: The refusal of a chart's own ``title`` inside a dashboard panel:
#: the panel titles its chart, so a card never carries two title sources.
PANEL_CHART_TITLE_REFUSAL = (
    "a panel chart is titled by the panel's `title`: move block.title to the panel's title "
    "(drawn top-left of the panel card, the legend beside it) and drop it from the chart."
)


class DashboardPanel(_Strict):
    """One cell of the dashboard grid: an optional title in the template's
    heading style over one figure. ``span`` is the number of the grid's
    columns the panel takes - three at most, two on a two-column grid
    (``DashboardSlide.panel_columns``, checked by the slide). The panel titles
    its figure: a ``chart`` in it carries no ``title`` of its own."""

    title: Label | None = None
    span: Literal[1, 2, 3] = Field(
        default=1, description="columns taken on the panel grid: 1-3, at most the slide's panel_columns"
    )
    block: PanelBlock

    @model_validator(mode="after")
    def _the_panel_titles_its_chart(self) -> DashboardPanel:
        if isinstance(self.block, ChartBlock) and self.block.title is not None:
            raise ValueError(PANEL_CHART_TITLE_REFUSAL)
        return self


def _grid_rows(spans: Sequence[int], columns: int = DASHBOARD_GRID_COLUMNS) -> int:
    """Rows the panels take under CSS grid auto-placement: a panel that does
    not fit the rest of its row starts the next one (sparse flow, the way the
    renderer lays the grid), so ``2, 2, 2`` is three rows, not two."""
    rows, used = 0, columns
    for span in spans:
        if used + span > columns:
            rows += 1
            used = 0
        used += span
    return rows


class DashboardSlide(_SlideBase):
    """A KPI tile row over a 3-column grid of chart / table / metric panels -
    the layout a Metabase or Board dashboard is reproduced on. The
    grid has two rows: at most six 1-column panels, a ``span`` counting
    against the same cells. Six tiles and four panels fit a 16:9 slide without
    the body having to shrink. ``panel_columns: 2`` draws the grid on
    two equal columns - a chart and a table half and half - where a span is 1
    or 2 and four single panels fill the two rows."""

    layout: Literal["dashboard"]
    title: TitleMarkup
    subtitle: Markup | None = None
    tiles: Annotated[
        list[MetricItem],
        Field(
            min_length=MIN_DASHBOARD_TILES,
            max_length=MAX_DASHBOARD_TILES,
            description="the KPI row across the top: 2-8 tiles, each a metric with an optional sparkline",
        ),
    ]
    tile_size: DashboardTileSize = Field(
        default="md",
        description=(
            "type of the tile row: sm = value 36px and 16px label / sublabel / delta, for 5-6 tiles "
            "whose sublabel or delta would wrap; 7-8 tiles step their value down on their own"
        ),
    )
    panels: Annotated[
        list[DashboardPanel],
        Field(
            min_length=1,
            max_length=MAX_DASHBOARD_PANELS,
            description=(
                "1-6 figures on the panel grid (panel_columns), auto-placed on at most two rows: a "
                "panel that does not fit the rest of its row starts the next one (spans 2,2,2 need three)"
            ),
        ),
    ]
    panel_columns: DashboardPanelColumns = Field(
        default=DASHBOARD_GRID_COLUMNS,
        description=(
            "columns of the panel grid: 3, or 2 equal ones for two panels half and half "
            "(a span is then 1 or 2, and four single panels fill the two rows)"
        ),
    )
    footer_block: LeafBlock | None = None
    hide_footer: bool = False

    @model_validator(mode="after")
    def _panels_fit_the_grid(self) -> DashboardSlide:
        columns = self.panel_columns
        too_wide = [
            f"panels.{index}.span ({panel.span})" for index, panel in enumerate(self.panels) if panel.span > columns
        ]
        if too_wide:
            raise ValueError(
                f"{', '.join(too_wide)} {'is' if len(too_wide) == 1 else 'are'} wider than the panel grid: "
                f"panel_columns is {columns}, so a panel spans {' or '.join(map(str, range(1, columns + 1)))} "
                f"columns ({columns} is the full width) - narrow the span or set panel_columns to "
                f"{DASHBOARD_GRID_COLUMNS}."
            )
        spans = [panel.span for panel in self.panels]
        rows = _grid_rows(spans, columns)
        if rows > DASHBOARD_GRID_ROWS:
            wider = f", set panel_columns to {DASHBOARD_GRID_COLUMNS}" if columns < DASHBOARD_GRID_COLUMNS else ""
            raise ValueError(
                f"panels need {rows} rows of the {columns}-column grid "
                f"(spans {', '.join(str(span) for span in spans)}) but a dashboard has room for "
                f"{DASHBOARD_GRID_ROWS} - narrow a span, drop a panel{wider} or split the slide."
            )
        return self


_CONTACT_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
    "web": re.compile(r"^(https?://)?[A-Za-z0-9.-]+\.[A-Za-z]{2,}(/\S*)?$"),
    "linkedin": re.compile(r"^[A-Za-z0-9_./:@-]{2,200}$"),
    "x": re.compile(r"^[A-Za-z0-9_./:@-]{2,200}$"),
    "github": re.compile(r"^[A-Za-z0-9_./:@-]{2,200}$"),
    "phone": re.compile(r"^\+?[0-9 ().-]{4,40}$"),
}


class Contact(_Strict):
    kind: ContactKind
    value: Annotated[str, Field(min_length=1, max_length=200)] = Field(
        description="address / URL / @handle / number, checked per kind"
    )
    label: Label | None = None

    @model_validator(mode="after")
    def _value_matches_kind(self) -> Contact:
        if not _CONTACT_PATTERNS[self.kind].match(self.value):
            raise ValueError(
                f"value {self.value!r} is not a valid {self.kind} contact "
                "(email: name@domain, web: domain or https URL, linkedin/x/github: "
                "handle or URL, phone: digits)."
            )
        return self


class ClosingSlide(_SlideBase):
    """Thank-you slide with speaker and contact lines."""

    layout: Literal["closing"]
    title: TitleMarkup = "Thank you"
    subtitle: Markup | None = None
    speaker: Speaker | None = None
    contacts: Annotated[list[Contact], Field(default_factory=list, max_length=MAX_CONTACTS)]
    tagline: Markup | None = None
    #: The closing's composition: absent or ``center`` is the centred
    #: closing every deck had; ``start`` the left layout of a title slide with
    #: ``align: start``. Absent stays absent, so a stored deck is unchanged.
    align: Literal["start", "center"] | None = Field(
        default=None,
        description="start (left-aligned, like a title slide with align start) or center; default: center",
    )
    #: Same backdrop catalog as the title slide, in this slide's scheme.
    hero: Hero = Field(
        default="plain",
        description=(
            "closing backdrop in the slide's scheme: plain / gradient / split / spotlight, or a "
            "template's own cover it offers (mosaic / cascade / horizon / pillar)"
        ),
    )
    hero_tone: HeroTone = Field(
        default="brand",
        description=(
            "colour of the hero, from the slide's palette: brand (the template's gradient; its "
            "six chart series for mosaic) or one colour alone - accent, gradient_from, "
            "gradient_to, info, success, warning, error, muted"
        ),
    )


Slide = Annotated[
    Union[  # noqa: UP007 - Pydantic needs the explicit Union for the discriminator
        TitleSlide,
        SectionSlide,
        AgendaSlide,
        ContentSlide,
        RoadmapSlide,
        TakeawaysSlide,
        DashboardSlide,
        ClosingSlide,
    ],
    Field(discriminator="layout"),
]

_LAYOUT_MODELS: dict[str, type[BaseModel]] = {
    "title": TitleSlide,
    "section": SectionSlide,
    "agenda": AgendaSlide,
    "content": ContentSlide,
    "roadmap": RoadmapSlide,
    "takeaways": TakeawaysSlide,
    "dashboard": DashboardSlide,
    "closing": ClosingSlide,
}
assert tuple(_LAYOUT_MODELS) == SLIDE_LAYOUTS, (tuple(_LAYOUT_MODELS), SLIDE_LAYOUTS)  # noqa: S101


def slide_model(layout: str) -> type[BaseModel] | None:
    """The model that validates ONE slide of *layout*, ``None`` for an unknown id.

    What a consumer that builds or reads a single slide outside a whole deck
    (the layout switch) validates against — the same model the deck
    union dispatches to, so a slide it accepts is a slide ``validate_spec``
    accepts.
    """
    return _LAYOUT_MODELS.get(layout)


class PresentationSpec(_Strict):
    """A complete, renderable deck."""

    title: Title
    theme: Annotated[Literal["dark", "light"], BeforeValidator(normalize_theme)] = Field(
        default="dark",
        description="Colour scheme the deck is read in; the brand comes from the template",
    )
    aspect_ratio: Annotated[
        Literal["16:9", "4:3", "A4", "letter"],
        BeforeValidator(normalize_aspect_ratio),
    ] = Field(
        default="16:9",
        description="16:9 / 4:3 = slides; A4 / letter = portrait PAGES (product brief, one-pager, datasheet)",
    )
    transition: Literal["none", "fade", "slide", "convex", "concave", "zoom"] = "slide"
    chrome: Chrome = Field(default_factory=Chrome)
    slides: Annotated[list[Slide], Field(min_length=1, max_length=MAX_SLIDE_CAP)]


# ── Validation entry point ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SpecProblem:
    """One field-addressed validation failure, renderable for two readers.

    ``path`` is the path the agent built (``slides.3.subtitle``), with the
    discriminated-union tags Pydantic inserts lifted out into ``layout`` /
    ``blocks``; ``message`` is Pydantic's own wording of what is wrong.
    """

    path: str
    layout: str | None
    blocks: tuple[str, ...]
    message: str

    def for_agent(self) -> str:
        notes = [f"layout={self.layout}"] if self.layout else []
        notes += [f"block={b}" for b in self.blocks]
        note = f" ({', '.join(notes)})" if notes else ""
        return f"{self.path or '(root)'}{note}: {self.message}"

    def for_human(self) -> str:
        """``Slide 4 (title): subtitle - String should have at least 1 character``.

        Slides are numbered the way the editor and the deck number them
        (1-based); the layout stays because a person reads "the title slide",
        not ``layout=title``; block tags become a plain "in <block>" note.
        """
        parts = self.path.split(".") if self.path else []
        where = ""
        if len(parts) >= 2 and parts[0] == "slides" and parts[1].isdigit():
            where = f"Slide {int(parts[1]) + 1}"
            if self.layout:
                where += f" ({self.layout})"
            parts = parts[2:]
        elif parts and parts[0] == "slides" and len(parts) == 1:
            where = "Slides"
            parts = []
        field = ".".join(parts)
        if self.blocks:
            field = f"{field} in {' > '.join(self.blocks)}" if field else " > ".join(self.blocks)
        subject = f"{where}: {field}" if where and field else (where or field or "Presentation")
        return f"{subject} - {self.message}"


class PresentationSpecError(ValueError):
    """A ``PresentationSpec`` payload that failed validation.

    ``str(exc)`` is the agent-readable, field-addressed message: the tool
    returns it verbatim as a tool error so the model can fix the exact slide
    that is wrong instead of regenerating the whole deck blindly. The same
    problems are kept structured on ``problems`` so a route a PERSON is behind
    (the editor's PATCH, duplicate, restore, package import) can answer with
    :meth:`human_message` — slides numbered from 1, no "call the tool again",
    no catalog dump.
    """

    def __init__(
        self,
        message: str,
        *,
        problems: Sequence[SpecProblem] = (),
        total: int | None = None,
    ) -> None:
        super().__init__(message)
        self.problems: tuple[SpecProblem, ...] = tuple(problems)
        #: How many problems Pydantic reported; ``problems`` is capped.
        self.total: int = len(self.problems) if total is None else total

    def human_details(self) -> str:
        """The problems as one bullet line each, for a person; no header."""
        if not self.problems:
            return str(self)
        lines = [f"- {problem.for_human()}" for problem in self.problems]
        if self.total > len(self.problems):
            lines.append(f"- ... and {self.total - len(self.problems)} more")
        return "\n".join(lines)

    def human_message(self) -> str:
        """Header plus :meth:`human_details`, for a route a person is behind."""
        if not self.problems:
            return str(self)
        return "The presentation cannot be saved as it is. Fix the following and try again:\n" + self.human_details()


#: Where the discriminated unions live, i.e. where Pydantic inserts a member
#: tag into an error path: a single union-typed field (``footer_block.callout``),
#: a list of union members (``slides.2.content``, ``blocks.0.text``,
#: ``children.1.list``) and the list-of-lists ``columns`` (``columns.0.1.text``).
#: Anywhere else a part that happens to spell a tag is a FIELD — ``items.1.text``
#: is a list item's text, ``items.0.title`` a card's title — and stays in the path.
#: ``block`` is a dashboard panel's figure (``panels.2.block.chart``).
_UNION_FIELDS = frozenset({"footer_block", "block"})
_UNION_LISTS = frozenset({"slides", "blocks", "children"})
_UNION_GRIDS = frozenset({"columns"})
_ALL_TAGS = frozenset(SLIDE_LAYOUTS) | frozenset(BLOCK_TYPES)
#: How many problems a single error reports; past that the count is enough.
_MAX_REPORTED_PROBLEMS = 15


def _is_union_tag(raw_parts: Sequence[str], index: int) -> bool:
    """Is ``raw_parts[index]`` a union member tag Pydantic inserted (see above)?"""
    if raw_parts[index] not in _ALL_TAGS or index == 0:
        return False
    previous = raw_parts[index - 1]
    if previous in _UNION_FIELDS:
        return True
    if not previous.isdigit() or index < 2:
        return False
    container = raw_parts[index - 2]
    if container in _UNION_LISTS:
        return True
    return container.isdigit() and index >= 3 and raw_parts[index - 3] in _UNION_GRIDS


def _collect_problems(exc: ValidationError) -> tuple[list[SpecProblem], int]:
    """Turn Pydantic errors into addressable :class:`SpecProblem` entries.

    ``slides.3.blocks.1.items.0.text: String should have at most 500
    characters`` beats a JSON dump of Pydantic's error objects: the location is
    the path the agent used to build the payload, so it can patch that one
    field. Pydantic inserts the resolved union member into the path for a
    discriminated union (``slides.2.content.blocks.0.callout.title``), which
    reads like nested fields called "content" and "callout"; those tags are
    lifted out into an explicit ``layout`` / ``blocks`` note so the remaining
    path is the one the agent built. Returns the (capped) problems and the
    total count.
    """
    problems: list[SpecProblem] = []
    errors = exc.errors()
    for err in errors:
        raw_parts = [str(part) for part in err["loc"]]
        parts: list[str] = []
        layout: str | None = None
        blocks: list[str] = []
        for index, part in enumerate(raw_parts):
            if not _is_union_tag(raw_parts, index):
                parts.append(part)
                continue
            if index == 2 and raw_parts[0] == "slides" and part in SLIDE_LAYOUTS:
                layout = part
            else:
                blocks.append(part)
        problems.append(
            SpecProblem(
                path=".".join(parts),
                layout=layout,
                blocks=tuple(blocks),
                message=str(err.get("msg", "invalid value")),
            )
        )
        if len(problems) >= _MAX_REPORTED_PROBLEMS:
            break
    return problems, len(errors)


def _format_errors(problems: Sequence[SpecProblem], total: int) -> str:
    """Render the problems as short, addressable lines for an LLM."""
    lines = [f"  - {problem.for_agent()}" for problem in problems]
    if total > len(problems):
        lines.append(f"  - … and {total - len(problems)} more error(s)")
    return "\n".join(lines)


def _assign_slide_ids(spec: dict) -> None:
    """Fill missing / duplicate slide ids in place (post-dump, so they persist)."""
    seen: set[str] = set()
    for slide in spec.get("slides") or []:
        slide_id = slide.get("id")
        while not slide_id or slide_id in seen:
            slide_id = uuid.uuid4().hex[:12]
        slide["id"] = slide_id
        seen.add(slide_id)


def clamp_slide_cap(value: object) -> int:
    """*value* as a slide cap: a whole number from 1 to :data:`MAX_SLIDE_CAP`.

    Anything that is not a number (a bool, a blank, ``None``) reads as
    :data:`DEFAULT_SLIDE_CAP`; a number past the ceiling IS the ceiling, and
    one under 1 is the default - a template can never switch the cap off, in
    either direction.
    """
    if isinstance(value, bool) or value is None:
        return DEFAULT_SLIDE_CAP
    try:
        cap = int(value)
    except (TypeError, ValueError):
        return DEFAULT_SLIDE_CAP
    if cap < 1:
        return DEFAULT_SLIDE_CAP
    return min(cap, MAX_SLIDE_CAP)


def slide_cap_hint(cap: int, *, units: str = "slides") -> str:
    """How a cap of *cap* came to be and how it is raised.

    Every refusal that names the cap - the validator's, ``insert_slide``'s,
    the editor's 422 - ends with this, so whoever hits it knows the number is
    the deck's template's ``max_slides`` (``DEFAULT_SLIDE_CAP`` for a free
    deck or a template that sets none) and not a platform constant, except at
    the ceiling, which is.
    """
    if cap >= MAX_SLIDE_CAP:
        return f"{MAX_SLIDE_CAP} {units} is the platform ceiling, which no template can raise"
    return (
        "the cap is the deck's template's max_slides setting "
        f"({DEFAULT_SLIDE_CAP} for a deck without a template or a template that sets none, "
        f"up to {MAX_SLIDE_CAP}); raise it on the template, or split the content over two decks"
    )


def validate_spec(payload: object, *, slide_cap: int = DEFAULT_SLIDE_CAP) -> dict:
    """Validate a raw spec payload and return its normalized JSON-safe dict.

    Raises :class:`PresentationSpecError` with a multi-line, field-addressed
    message on any problem. The returned dict is what gets persisted in
    ``Presentation.spec`` — never the raw agent input. Defaults are
    materialized (so the renderer never guesses) and every slide carries an
    ``id``.

    *slide_cap* is how many slides THIS deck may hold: the linked
    template's ``max_slides`` on an authoring path, :data:`MAX_SLIDE_CAP` on a
    path that copies content already accepted (a duplicate, a restore, a
    package import, the read-side normalisation of a stored spec). Clamped to
    the ceiling, which the model enforces regardless.

    A slide wearing a retired v1 layout is upgraded to its v2 equivalent BEFORE
    validation (``app.services.presentation_legacy``): this is the one
    write funnel, so a stored v1-era deck re-saved through any path, a package
    written before the retirement and an agent still emitting a v1 layout all
    land as v2 - and a caller that wants to tell the agent what happened reads
    ``retired_layout_note(payload)`` on the raw payload first.
    """
    if not isinstance(payload, dict):
        raise PresentationSpecError(
            "The presentation spec must be a JSON object with a 'title' and a "
            "'slides' array."
        )
    # Lazy: the upgrader imports this module for its size caps.
    from app.services.presentation_legacy import upgrade_spec

    payload = upgrade_spec(payload)
    try:
        spec = PresentationSpec.model_validate(payload)
    except ValidationError as exc:
        problems, total = _collect_problems(exc)
        raise PresentationSpecError(
            "The presentation spec is invalid and was NOT saved. Fix these "
            "field(s) and call the tool again:\n"
            f"{_format_errors(problems, total)}\n"
            f"Allowed slide layouts: {', '.join(SLIDE_LAYOUTS)}. Allowed block "
            f"types: {', '.join(BLOCK_TYPES)}. Each accepts a fixed set of keys "
            "— get_presentation(reference=\"agenda,table\") reads them, "
            "naming the layouts or blocks to read.",
            problems=problems,
            total=total,
        ) from exc
    data = spec.model_dump(mode="json", exclude_none=True)
    cap = clamp_slide_cap(slide_cap)
    count = len(data.get("slides") or [])
    if count > cap:
        # The deck's OWN cap, under the model's ceiling: the same shape of
        # refusal as a Pydantic bound (field-addressed, structured for the
        # human message), naming the cap and how it is raised.
        units = unit_noun(data, cap)
        problem = SpecProblem(
            path="slides",
            layout=None,
            blocks=(),
            message=(
                f"at most {cap} {units} for this {whole_noun(data)}, got {count}; "
                f"{slide_cap_hint(cap, units=units)}"
            ),
        )
        raise PresentationSpecError(
            "The presentation spec is invalid and was NOT saved. Fix these "
            "field(s) and call the tool again:\n"
            f"{_format_errors([problem], 1)}",
            problems=[problem],
            total=1,
        )
    _assign_slide_ids(data)
    return data


# ── Catalogs ─────────────────────────────────────────────────────────────────


def layout_catalog() -> dict[str, list[str]]:
    """Map each layout id to the field names it accepts.

    Used to build the tool description, the template-container ``layouts``
    guardrail and the built-in seed from the models themselves, so nothing can
    document a field the validator rejects.
    """
    return {
        layout: [name for name in model.model_fields if name != "layout"]
        for layout, model in _LAYOUT_MODELS.items()
    }


def block_catalog() -> dict[str, list[str]]:
    """Map each block type to the field names it accepts (same contract)."""
    return {
        block: [name for name in model.model_fields if name != "type"]
        for block, model in _BLOCK_MODELS.items()
    }


# ── Agent-facing reference, derived from the models ──────────────────────────


def _union_members(annotation: Any) -> tuple[Any, ...] | None:
    origin = typing.get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        return typing.get_args(annotation)
    return None


def _strip_optional(annotation: Any) -> tuple[Any, bool]:
    members = _union_members(annotation)
    if members and type(None) in members:
        rest = [m for m in members if m is not type(None)]
        inner = rest[0] if len(rest) == 1 else Union[tuple(rest)]  # noqa: UP007
        return inner, True
    return annotation, False


def _metadata_bounds(metadata: list[Any]) -> tuple[int | None, int | None]:
    low = high = None
    for item in metadata:
        if isinstance(item, FieldInfo):
            # ``Field(min_length=..)`` inside ``Annotated`` keeps its constraints
            # in its own ``.metadata`` list, not as attributes.
            inner_low, inner_high = _metadata_bounds(item.metadata)
            low = inner_low if inner_low is not None else low
            high = inner_high if inner_high is not None else high
            continue
        if hasattr(item, "min_length"):
            low = item.min_length
        if hasattr(item, "max_length"):
            high = item.max_length
    return low, high


def _has_validator(metadata: list[Any], func: Any) -> bool:
    for item in metadata:
        if isinstance(item, FieldInfo) and _has_validator(item.metadata, func):
            return True
        if getattr(item, "func", None) is func:
            return True
    return False


def _block_union_name(members: tuple[Any, ...]) -> str | None:
    names = {getattr(m, "__name__", None) for m in members}
    if names == {m.__name__ for m in _BLOCK_MODELS.values()}:
        return "Block"
    if names == {m.__name__ for m in _BLOCK_MODELS.values()} - {"ColumnsBlock"}:
        return "LeafBlock"
    if names == {m.__name__ for m in _ATOMIC_BLOCK_MODELS}:
        return "AtomicBlock"
    if names == {m.__name__ for m in _PANEL_BLOCK_MODELS}:
        return "PanelBlock"
    return None


def _type_label(annotation: Any, metadata: list[Any], defs: dict[str, type[BaseModel]]) -> str:
    """Short type rendering for the reference (``markup<=500``, ``Tone``...)."""
    origin = typing.get_origin(annotation)
    if origin is Literal:
        for alias, alias_type in _ENUM_ALIASES.items():
            if typing.get_args(alias_type) == typing.get_args(annotation):
                return alias
        return "|".join(
            f'"{v}"' if isinstance(v, str) else str(v) for v in typing.get_args(annotation)
        )
    if origin is Annotated:
        inner, *extra = typing.get_args(annotation)
        return _type_label(inner, [*metadata, *extra], defs)
    if origin is list:
        (item,) = typing.get_args(annotation)
        low, high = _metadata_bounds(metadata)
        bounds = f" ({low or 0}-{high})" if high is not None else ""
        return f"{_type_label(item, [], defs)}[]{bounds}"
    members = _union_members(annotation)
    if members:
        union_name = _block_union_name(members)
        if union_name:
            return union_name
        return "|".join(_type_label(m, metadata, defs) for m in members)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        defs.setdefault(annotation.__name__, annotation)
        return annotation.__name__
    if annotation is str:
        _low, high = _metadata_bounds(metadata)
        if _has_validator(metadata, check_icon):
            return "icon"
        if _has_validator(metadata, check_markup):
            return f"markup<={high}" if high else "markup"
        if _has_validator(metadata, check_diagram_source):
            return "mermaid"
        if _has_validator(metadata, check_action_url):
            return "url"
        return f"text<={high}" if high else "text"
    if annotation is int:
        return "int"
    if annotation is float:
        return "number"
    if annotation is bool:
        return "bool"
    return getattr(annotation, "__name__", str(annotation))


def _describe_field(name: str, info: FieldInfo, defs: dict[str, type[BaseModel]]) -> str:
    annotation, optional = _strip_optional(info.annotation)
    label = _type_label(annotation, list(info.metadata), defs)
    if name.endswith("file_id"):
        label = "file_id"
    has_default = info.default is not PydanticUndefined or info.default_factory is not None
    optional = optional or has_default
    out = f"{name}{'?' if optional else ''}: {label}"
    default = info.default
    if default is not PydanticUndefined and default is not None:
        if isinstance(default, bool):
            out += f" = {str(default).lower()}"
        elif isinstance(default, str):
            out += f' = "{default}"'
        elif isinstance(default, int):
            out += f" = {default}"
    if info.description:
        out += f" ({info.description})"
    return out


def _describe_model(model: type[BaseModel], skip: tuple[str, ...], defs: dict) -> str:
    return ", ".join(
        _describe_field(name, info, defs)
        for name, info in model.model_fields.items()
        if name not in skip
    )


def spec_reference() -> str:
    """Compact, model-derived reference of layouts, blocks and value types.

    This is what the ``generate_presentation`` tool description, the built-in
    brand template's ``guidance`` and the docs quote: because it is generated
    from the Pydantic models, it can never advertise a field, value or bound
    the validator refuses. ``?`` marks optional fields; ``= x`` a default;
    ``A[] (1-6)`` a list with its size bounds; ``markup<=500`` an inline-markup
    string with its length cap.
    """
    defs: dict[str, type[BaseModel]] = {}
    shared = tuple(name for name in _SlideBase.model_fields)
    layout_lines = [
        f"- {layout}: {_describe_model(model, ('layout', *shared), defs)}"
        for layout, model in _LAYOUT_MODELS.items()
    ]
    block_lines = [
        f"- {block}: {_describe_model(model, ('type',), defs)}"
        for block, model in _BLOCK_MODELS.items()
    ]
    # Referenced models, transitively (describing one may pull in another).
    type_lines: list[str] = []
    described: set[str] = set()
    while len(described) < len(defs):
        for name, model in list(defs.items()):
            if name in described:
                continue
            described.add(name)
            type_lines.append(f"- {name}: {{{_describe_model(model, (), defs)}}}")
    enum_lines = [
        f"- {alias}: " + "|".join(f'"{v}"' for v in typing.get_args(alias_type))
        for alias, alias_type in _ENUM_ALIASES.items()
    ]
    shared_desc = _describe_model(_SlideBase, (), {})
    return (
        "SLIDE LAYOUTS (each slide: {\"layout\": ID, ...}; every layout also "
        f"accepts {shared_desc}):\n"
        + "\n".join(layout_lines)
        + "\n\nBLOCKS (each: {\"type\": ID, ...}; Block = any block, LeafBlock = any "
        "except columns, AtomicBlock = any except columns/callout/comparison, "
        f"PanelBlock = {'/'.join(PANEL_BLOCK_TYPES)}):\n"
        + "\n".join(block_lines)
        + "\n\nTYPES:\n"
        + "\n".join(type_lines)
        + "\n"
        + "\n".join(enum_lines)
        + f"\n- markup: inline text markup — {MARKUP_SUMMARY}; no HTML."
        + "\n- icon: a Lucide icon name (shield-check, rocket, users, chart-line...)."
        + "\n- file_id / *_file_id: id of a platform file (template asset or generated file)."
        + "\n- url: an https:// link or a mailto: address (banner actions)."
        + "\n- table cells: a string, true/false (check / cross) or null (empty)."
    )
