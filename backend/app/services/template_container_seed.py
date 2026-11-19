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

"""Seed the built-in template containers — the brand decks.

Behaviour on every startup, mirroring ``app/agents/seed.py``:

  1. **Insert** containers whose slug does not exist yet.
  2. **Full-sync** existing built-in containers — every metadata field is
     overwritten to match the code, so a brand change ships with a deploy.
  3. **Delete** built-in containers whose slug is no longer declared here, or
     that do not target the current platform mode (assets included, S3 objects
     too — a stale built-in must not leak binaries; the objects are purged once
     the delete is committed, and the purge stops at the first sign of an
     unreachable store, so a start never waits on S3). This half also runs on
     its own, on every start and whatever the AI state
     (:func:`remove_unshipped_builtin_containers`): a built-in a mode
     must not carry has to go even from a platform whose AI is switched off,
     where the rest of the seed does not run.

Admin-created containers are never touched: a built-in row is identified by the
``slug:<slug>`` tag it carries (the same idiom ``seed_builtin_skills`` uses for
``AgentSkill``, which has no slug column either). Because the sync overwrites
metadata, an admin who wants their own palette **duplicates** the brand
container rather than editing it — the REST write surface refuses a mutation of
a built-in with that very advice, and the UI shows a Duplicate action.

Three containers ship:

* ``filigran-brand`` — the Filigran brand deck, built from the design-system
  tokens (``filigran-design-system/packages/filigran-design-system/src/tokens``)
  and seeded in COPILOT mode only (``platform_targets: ["copilot"]``): it
  is the house style Filigran employees present in on Filigran Copilot. It also
  carries the PREDEFINED ASSETS a Filigran deck is made of — the architecture /
  product-flow diagrams, product screenshots, brand illustrations,
  certification badges, product logos, team photos and the partner /
  integration / customer logo packs — so an agent asked for "the XTM platform
  diagram" or "our integrations" places the real figures instead of drawing
  approximations. Those are Filigran's own customers, partners and people,
  which is why any other platform removes it, assets included. It declares
  both colour schemes (``extra_metadata.supported_schemes``): dark from
  the base palette, light from ``theme_variants.light`` - and its own slide
  cap (``extra_metadata.max_slides: 150``): the corporate deck runs
  longer than the platform default allows.
* ``thread-brand`` — the THREAD conference deck (filigran.io/thread), seeded in
  COPILOT mode only like the Filigran deck, whose predefined assets it includes
  (``brand_assets/thread/manifest.json``): dark first with a ``light`` variant,
  its covers drawn on the ``cascade`` hero, the "thread- powered by Filigran"
  lockup in the chrome.
* ``xtm-one-brand`` — the XTM One product deck, seeded in EVERY mode
  (``platform_targets: ["global"]``), dark only: the one working example
  every platform has, and the default template wherever the Filigran deck does
  not ship.

The three ``asset-library`` containers (``filigran-partner-logos``,
``filigran-integration-logos``, ``filigran-customer-logos``) are RETIRED:
a template is a template — its assets are readable and its colours
inherited — and a container that could carry logos but dress no deck asked the
user to learn a distinction that bought them nothing. Their assets are now
predefined assets of the brand deck (roles ``partner_logo`` /
``integration_logo`` / ``customer_logo``, one ``get_template(role=...)`` call
away). On an install that still has the three rows, Phase 1 RE-HOMES their
asset rows into the brand deck — same ``FileAttachment`` ids, so every deck
that placed one of those logos keeps rendering — and deletes the emptied
containers (:data:`RETIRED_ASSET_HOMES`), each only once a re-count finds no
asset row bound to it any more (``_retire_emptied_container``: the FK cascades,
so a row left behind would go down with the container; a non-empty one is kept -
a template asset written meanwhile is moved on the next boot, a row of any other
source is named for an operator, since the re-home never moves those).

Metadata only is seeded here. The ASSETS themselves are uploaded by
``template_asset_seed`` right after, from the manifests under
``app/agents/seed_data/brand_assets/`` — kept separate because an asset upload
needs S3, and a slow or absent object store must never block the metadata sync
that every deck depends on. Note the theme config carries NO file ids: the logo
the deck chrome draws is resolved by asset ROLE at render time
(``presentations.resolve_template_theme``), so the ids only ever live on the
asset rows themselves.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import Text, and_, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.file_attachment import FileAttachment
from app.models.presentation import Presentation
from app.models.report import Report
from app.models.template_container import TEMPLATE_ASSET_SOURCE, TemplateContainer
from app.services.presentation_spec import layout_catalog
from app.services.template_containers import PRESENTATION_KIND

logger = logging.getLogger(__name__)

#: Tag marking a container as code-managed (vs. admin-created).
BUILTIN_TAG = "builtin"

#: Prefix of the tag that carries a built-in container's slug.
SLUG_TAG_PREFIX = "slug:"


def slug_tag(slug: str) -> str:
    """The tag a built-in container carries for *slug* (``slug:<slug>``)."""
    return f"{SLUG_TAG_PREFIX}{slug}"


def builtin_container_filter(slug: str) -> ColumnElement[bool]:
    """SQL predicate selecting the seeded (built-in) container for *slug*.

    The ONE way to look a built-in container up by tag in SQL — shared by the
    deck default resolution (``presentations.resolve_default_template_id``) and
    the asset seed (``template_asset_seed``). ``tags`` is a plain ``JSON``
    column, and SQLAlchemy's ``JSON.contains([...])`` on it does not compile to
    a containment test but to ``tags LIKE '%' || '["x"]'::JSON || '%'``, which
    PostgreSQL rejects outright (there is no ``LIKE`` for ``json``) — a query
    that only fails once it reaches a real database, which is why the fake-
    session tests never saw it. Membership is tested the way
    ``template_containers.apply_structured_filters`` does: on the text form,
    against the QUOTED token, so ``slug:brand`` cannot match ``slug:brand-2``.
    """
    tags_text = cast(TemplateContainer.tags, Text)
    return and_(
        TemplateContainer.is_company_managed.is_(True),
        tags_text.contains(f'"{BUILTIN_TAG}"'),
        tags_text.contains(f'"{slug_tag(slug)}"'),
    )


#: Human-readable label + intent per layout id. The FIELD LISTS are not written
#: here — they are derived from the Pydantic models in ``presentation_spec`` (see
#: ``_build_layouts``), so a seeded container can never advertise a layout, or a
#: field, that the validator would then reject. One generation of layouts
#: (the v1 ones retired): ``content`` + blocks covers every
#: shape the retired four had, and a stored slide wearing one is upgraded.
_LAYOUT_LABELS: dict[str, tuple[str, str]] = {
    "title": (
        "Title",
        "Opening slide: event label, title, subtitle, speaker, optional icon strip.",
    ),
    "section": (
        "Section divider",
        "A numbered chapter break between two parts of the story.",
    ),
    "agenda": (
        "Agenda",
        "The plan of the talk; `current` highlights the item being presented.",
    ),
    "content": (
        "Content (blocks)",
        "The workhorse: a title over a stack of blocks, or 2-4 columns of blocks "
        "(text, lists, metrics, tables, features, timelines, flows, callouts, "
        "images, diagrams).",
    ),
    "roadmap": (
        "Roadmap",
        "2-4 phase cards side by side, each with a status and its goals.",
    ),
    "takeaways": (
        "Key takeaways",
        "2-6 numbered cards that close a section or the whole deck.",
    ),
    "closing": (
        "Closing",
        "Thank-you slide with speaker, contacts and a tagline.",
    ),
}


def _build_layouts() -> list[dict[str, Any]]:
    """Derive the layout catalog from the spec models."""
    layouts: list[dict[str, Any]] = []
    for layout_id, fields in layout_catalog().items():
        name, use_for = _LAYOUT_LABELS.get(layout_id, (layout_id, ""))
        layouts.append(
            {"id": layout_id, "name": name, "fields": fields, "use_for": use_for}
        )
    return layouts


#: The agenda entry of a template that opts into the plain agenda
#: (``design.agenda: "plain"``): every item is drawn alike there, so
#: the entry advertises no ``current`` field and its intent names none.
_PLAIN_AGENDA_USE_FOR = "The plan of the talk: the numbered chapters, every item drawn alike."
_PLAIN_AGENDA_HIDDEN_FIELDS = frozenset({"current"})


def _layouts_for_design(layouts: list[dict[str, Any]], design: object) -> list[dict[str, Any]]:
    """The layout catalog *layouts* as a template declaring *design* advertises it.

    A template with the plain agenda composition draws every agenda item in
    the same state: its agenda entry drops the ``current`` field and
    the sentence about it. The field stays valid in the spec (a stored deck
    still validates); only what the template advertises changes. Every other
    template gets *layouts* unchanged.
    """
    if not isinstance(design, dict) or design.get("agenda") != "plain":
        return layouts
    out: list[dict[str, Any]] = []
    for entry in layouts:
        if entry.get("id") == "agenda":
            entry = {
                **entry,
                "fields": [f for f in entry.get("fields", []) if f not in _PLAIN_AGENDA_HIDDEN_FIELDS],
                "use_for": _PLAIN_AGENDA_USE_FOR,
            }
        out.append(entry)
    return out


def default_layout_entries() -> list[dict[str, Any]]:
    """The labelled layout catalog, for callers outside the seed.

    The "Generate with AI" template generator (``services/template_generation``)
    shows the model exactly this list — same ids, same human labels, same
    ``fields`` derived from the spec models — so a generated catalog can never
    advertise a layout the built-in one would not.
    """
    return _build_layouts()


#: Composition guidance every brand deck carries in ``extra_metadata`` — read by
#: the agent through ``search_templates`` / ``get_template``. Kept short on
#: purpose: the composition recipes proper live in the presentation-design skill.
_BRAND_COMPOSITION = (
    "Open with a `title` slide (event label + speaker), follow with an `agenda`, "
    "use a `section` divider before each chapter, tell the story on `content` "
    "slides (one idea per slide, 3-5 blocks or 2-3 columns max), put the numbers "
    "in a `metrics_grid` early, use `roadmap` for phased plans and `takeaways` "
    "before the `closing` slide. Use inline markup (`**bold**`, `[[accent:text]]`) "
    "sparingly to point at the one thing that matters on the slide. Brand logos "
    "and the footer are drawn by the chrome automatically - never write a "
    "footer into a slide"
)

_BRAND_COLLATERAL = (
    "Sales collateral (product brief, one-pager, "
    "battlecard) is a portrait page - `aspect_ratio` `A4` or `letter` - built "
    "from `banner`, `key_value`, `table` (toned column, check / cross cells, "
    "footnote), `pricing_tiers`, `badges` and `personas` blocks; the deck arcs "
    "are in the presentation-design skill."
)

#: The product figures every brand deck ships - the XTM One deck on
#: every platform, the Filigran ones on top of the Filigran library - as an
#: agent is told to use them, so no two decks describe them apart.
_PRODUCT_ASSETS_GUIDANCE = (
    "product logos are assets with role "
    "`product_logo` — reference them by file id in an `image` block. Prefer the "
    "PREDEFINED assets over drawing your own: the `diagram` assets are the real "
    "architecture and product-flow figures (XTM platform, OpenCTI, OpenAEV, XTM "
    "One agents, CTEM lifecycle), the `screenshot` assets are real product UI"
)
_PICK_BY_DESCRIPTION = "read each asset's description with `get_template` and pick by it."

#: The XTM One deck ships the product figures alone: the Filigran badges and
#: logo libraries are Copilot-only, so its guidance names none of them.
#: Its own logo and mark hold the chrome roles, one role per asset, so a
#: ``product_logo`` listing never shows them: the guidance says where they are.
_XTM_ONE_GUIDANCE = (
    f"{_BRAND_COMPOSITION}; {_PRODUCT_ASSETS_GUIDANCE}; XTM One's own logo and mark are the `logo` and "
    f"`logo_mark` assets, not `product_logo` ones; {_PICK_BY_DESCRIPTION} {_BRAND_COLLATERAL}"
)

#: The Filigran predefined assets, as an agent is told to use them: shared by
#: every deck that ships them (the Filigran deck, and the THREAD deck that
#: includes the Filigran packs), so the two never describe them apart.
_FILIGRAN_ASSETS_GUIDANCE = (
    f"{_PRODUCT_ASSETS_GUIDANCE}, "
    "the `badge` assets are the certifications and awards; "
    f"{_PICK_BY_DESCRIPTION} The partner, integration "
    "and customer logos are assets of this template too (roles `partner_logo`, "
    "`integration_logo`, `customer_logo`, transparent backgrounds): list one "
    "family with `get_template(role=\"integration_logo\")` or search the "
    "captions with `get_template(query=\"crowdstrike\")`, then place the file "
    "ids in a `logo_grid` block."
)

#: The product hues of the Filigran deck by name, so an agent colours
#: an architecture zone or node for a product without guessing the slot.
_BRAND_PRODUCT_HUES = (
    "Product colours for an architecture zone or node `color`: XTM One = `brand_1` "
    "(the XTM One purple), OpenCTI = `brand_2`, OpenAEV = `brand_3`, OpenCRQ = `brand_4`."
)

_BRAND_GUIDANCE = f"{_BRAND_COMPOSITION}; {_FILIGRAN_ASSETS_GUIDANCE} {_BRAND_PRODUCT_HUES} {_BRAND_COLLATERAL}"

#: What a template's description says it ships: every Filigran
#: template carries the Filigran deck's library, every XTM One template the
#: product material that may ship on every platform.
_FILIGRAN_LIBRARY = (
    "Ships every predefined asset of the Filigran deck: brand and product logos, diagrams, product "
    "screenshots, badges, team and event photos, and the partner, integration and customer logos."
)
_XTM_PRODUCT_LIBRARY = (
    "Ships the XTM product logos and marks, the architecture and product-flow diagrams and the product "
    "screenshots as predefined assets."
)

#: How a THREAD keynote is composed: a deck read from the back of a dark
#: auditorium, opening and closing on the cascade covers the template's deck
#: defaults lay. Deck-agnostic: no talk, no speaker, no edition but the
#: footer's default.
_THREAD_COMPOSITION = (
    "A THREAD deck is presented on stage, read from the back of a dark room: one "
    "idea per slide, few words, large numbers. Open on a `title` slide - "
    "`event_label` is the session type as the agenda prints it (KEYNOTE, PANEL, "
    "WORKSHOP, FIRESIDE CHAT), `speakers` every speaker in speaking order with "
    "their `photo_file_id` when there is one, the slide `footer` the date, the "
    "room and the venue - and close on a `closing` slide with the speaker's "
    "contacts and a short tagline; both covers draw the THREAD `cascade` by "
    "default (`mosaic`, `horizon` and `pillar` are the other THREAD covers, on "
    "request). Give every chapter a numbered "
    "`section` divider (a roadmap keynote: one per highlight), keep a body slide "
    "to 3-5 blocks or 2-3 columns, put the proof in a `metrics_grid`, a `quote` or "
    "a customer `logo_grid`. Mark the one phrase of a headline that carries the "
    "message with `[[gradient:...]]` (the THREAD cool gradient) and a label with "
    "`[[accent:...]]` (THREAD cyan) - never both on one slide. The footer reads "
    "the edition (`chrome.fields.edition`, THREAD 2026 by default), the deck "
    "title and an optional classification; the chrome draws the 'thread- "
    "powered by Filigran' lockup on every slide (the cascade cover draws it large "
    "in its corner) and the mosaic 't' emblem in the top-right corner of the "
    "content and section slides - never write a footer, a logo or the emblem into "
    "a slide. The `illustration` assets are the THREAD "
    "mosaic artworks, for a statement slide beside the text."
)

_THREAD_GUIDANCE = f"{_THREAD_COMPOSITION} The Filigran {_FILIGRAN_ASSETS_GUIDANCE} {_BRAND_COLLATERAL}"

#: The "Color Set" of the THREAD design kit (Figma, Filigran marketing), by
#: its own names: the ONLY colours the THREAD palette draws in, in both
#: schemes, besides the neutral steps between Black Anthracite and Alabaster
#: (surfaces, hairlines, muted text) and the two light-scheme tones the set
#: has no colour for (``_THREAD_LIGHT_SHADES``). The mosaic draws in them
#: exactly, on near-black and on white alike - as the kit does.
THREAD_COLOR_SET: dict[str, str] = {
    "lime": "#dfffa6",
    "blue": "#3492ff",
    "coral": "#fd4b39",
    "purple": "#e184ff",
    "mint": "#d0ffdb",
    "cyan": "#00e0ff",
    "pink": "#fd5568",
    "mauve": "#d8a8d6",
    "orange": "#ff8558",
    "peach": "#ffb89e",
    "cobalt_blue": "#001bda",
    "black_anthracite": "#0a0a0a",
    "alabaster": "#fbfbfb",
}
_T = THREAD_COLOR_SET

#: A success and a warning tone read on white, and the set's green (Mint) and
#: orange do not (1.1:1 and 2.3:1): each is the set's colour taken towards
#: Black Anthracite just far enough to read at 3:1 on the light page, the
#: threshold of slide-size text - 42% for Mint, 14% for Orange. Nothing else
#: in the THREAD palette is a shade.
_THREAD_LIGHT_SHADES: dict[str, str] = {"success": "#7d9883", "warning": "#dd744d"}

#: The THREAD footer: the edition first - a declared variable with a default,
#: so every THREAD deck is signed with the conference without a setting - then
#: the deck title and the optional classification of every brand deck (an
#: embargoed announcement is the case it exists for). The mosaic 't' (the
#: template's mark) sits in the top-right corner of the content and section
#: slides as the brand's emblem.
_THREAD_CHROME: dict[str, Any] = {
    "footer": "{{edition}} - {{title}} - {{classification}}",
    "header": None,
    "variables": [
        {
            "key": "edition",
            "label": "Edition",
            "hint": "The conference edition the footer opens with: THREAD 2026, THREAD 2027...",
            "default": "THREAD 2026",
        },
        {
            "key": "classification",
            "label": "Classification",
            "hint": "Who may read this deck: Public, Internal, Confidential...",
        },
    ],
    "emblem": True,
}

#: The footer every brand deck draws on every slide: the template's
#: pattern, filled ONCE per deck from the deck title, its event label and the
#: ``classification`` variable (``chrome.fields.classification``). Like the
#: event, the classification is optional and has NO default: a deck
#: draws it only once it sets one. An empty placeholder collapses with its
#: separator, so a deck with neither reads "Title", never "- Title -".
#: Validated by ``normalize_template_chrome`` like any authored declaration
#: (pinned by the seed tests).
_BRAND_CHROME: dict[str, Any] = {
    "footer": "{{event}} - {{title}} - {{classification}}",
    "header": None,
    "variables": [
        {
            "key": "classification",
            "label": "Classification",
            "hint": "Who may read this deck: Public, Internal, Confidential...",
        }
    ],
}

#: The Filigran Brand Deck's chrome (PPT Design System section 3.3,
#: Brand decisions of 28 Oct 2026): the footer reads title, event and
#: classification separated by a middle dot, each part optional (an empty
#: part drops with its separator); the faint watermark is off and the mark is
#: drawn as the corner emblem on every body slide instead. The XTM One deck
#: keeps ``_BRAND_CHROME``. Slice 2: the emblem is 48 px on the right
#: margin, its top on the top of the slide title, and the footer's right corner
#: reads the page (two digits, from the first slide after the cover) and the
#: copyright with the year computed at render time, instead of ``n / total``.
#: The mark is inked in one colour (``emblem_ink: accent``): Royal Blue, the
#: slide's accent, on a light slide and white on a dark one.
_FILIGRAN_BRAND_CHROME: dict[str, Any] = {
    **_BRAND_CHROME,
    "footer": "{{title}} \u00b7 {{event}} \u00b7 {{classification}}",
    "footer_right": "{{page}} \u00b7 \u00a9 {{year}} Filigran. All rights reserved.",
    "emblem": True,
    "emblem_size": 48,
    "emblem_align": "title",
    "emblem_ink": "accent",
}

#: The Filigran Brand Deck's design tokens (spec 3.0 / 3.4 / 3.5 / 3.6,
#: ``presentation_design``): the flat design. Every other template declares
#: none and keeps the renderer's own look (shadows, card light, eyebrows).
_FILIGRAN_BRAND_DESIGN: dict[str, Any] = {
    "shadows": False,
    "glow": False,
    "eyebrows": False,
    "gradient_strokes": "never",
    "radius": {"card": 12, "inner": 8, "chip": 4, "table": 0},
    "spacing": {
        "margin_x": 96,
        "margin_top": 72,
        "footer": 56,
        "title_body": 40,
        "title_subtitle": 12,
        "tile_gap": 24,
        "block_gap": 32,
        "block_gap_max": 48,
        "card_padding": 24,
        "kpi_padding": 20,
        "banner_padding": 32,
    },
    "body_align": "top",
    # Slice 4 (spec 3.8 / 3.11): the slide compositions. Titles white
    # on dark and Royal Blue on light, the brand cover and closing, the
    # outlined section number with the body footer, the plain numbered agenda
    # grid, every item drawn alike (Brand 4 Nov 2026). The short
    # gradient bar under every body slide title is back (Sam 3 Nov
    # 2026): slice 4 had turned it off.
    "title_underline": True,
    "title_ink": "brand",
    "cover": "brand",
    "section": "outline",
    "agenda": "plain",
    # The cover and closing emblem (brand owner 4 Nov 2026): the
    # mark at 95% of the slide height, cropped by the bottom-right corner, at
    # 7% opacity. Declared here so every startup sync carries it.
    "cover_emblem": {"scale": 95, "anchor": "bottom_right", "opacity": 7},
}

#: Where the assets of a RETIRED built-in container go when Phase 1 removes it:
#: slug of the retired container -> slug of the built-in that inherits
#: its ``FileAttachment`` rows. The rows are re-bound, never copied, so their ids
#: - the ones decks reference logos by - stay valid; the asset sync that runs
#: next recognises a re-homed seeded row by its object key's seed tail and
#: leaves it alone. A retired slug whose heir is missing (or is itself not
#: declared for this platform mode, as the brand deck is in the XTM One modes)
#: falls back to the plain delete of Phase 1.
RETIRED_ASSET_HOMES: dict[str, str] = {
    "filigran-partner-logos": "filigran-brand",
    "filigran-integration-logos": "filigran-brand",
    "filigran-customer-logos": "filigran-brand",
}


#: The built-in brand decks. ``theme_config`` carries the values a deck renders
#: with (only keys in ``app.services.presentations.THEME_CONFIG_KEYS`` are ever
#: forwarded); ``extra_metadata.supported_schemes`` DECLARES the colour schemes
#: the template dresses (the same declaration the template form's Dark
#: / Light switches write), and ``theme_variants`` carries the palette of the
#: second scheme. The free-text ``theme`` column is a LEGACY search label
#: that never changed a pixel: the built-ins leave it empty, so nothing
#: on a template page reads "legacy"; the brand is found by name and by the
#: ``filigran`` / ``xtm-one`` tags.
BUILTIN_TEMPLATE_CONTAINERS: list[dict[str, Any]] = [
    {
        # The Filigran brand deck, on the PPT Design System of the
        # Brand & Communication team (final decisions of 28 Oct 2026):
        # every colour is a hex of the brand extended palette (Jaguar #00020C,
        # Navy #071578, Royal Blue #001BDA, Sky Blue #0FBCFF, Aqua #00F1BD,
        # their tints and shades), except the two backgrounds of the April
        # 2026 guidelines. Dark is the primary scheme: flat #0A0C1C page, the
        # card fill and stroke unchanged, Sky Blue accent, the G5 block
        # gradient (Sky Blue -> Aqua). The ``light`` variant below is the ice
        # blue page with the Royal Blue accent and the G6 gradient: a deck the
        # user asks to be light comes out as a light Filigran deck, not as a
        # dark deck that calls itself light (see ``THEME_VARIANTS_KEY``).
        # Muted text and the light stroke carry their alpha (#FBFBFB 60%,
        # #00020C 65%, #001BDA 20%) as 8-digit hex. Product typefaces in both:
        # Geologica titles, IBM Plex Sans body, IBM Plex Mono code.
        # Copilot only: its assets are Filigran's team photos and
        # customer / partner logos, which must never reach an XTM One platform.
        "slug": "filigran-brand",
        "platform_targets": ["copilot"],
        "name": "Filigran Brand Deck",
        "description": (
            "The Filigran house style for agent-generated presentations, built "
            "from the Filigran design system: brand blue on a flat #0A0C1C dark "
            "page with #0D172B cards (an ice blue light palette when the deck "
            "theme is light), Geologica "
            "and IBM Plex typefaces, 16:9, flat brand colours, the Filigran "
            "emblem in the corner of the body slides. Ships the predefined assets a "
            "Filigran deck is made of: brand and product logos, the XTM platform "
            "/ OpenCTI / OpenAEV / XTM One architecture and flow diagrams, "
            "product screenshots, CTEM illustrations, certification and G2 "
            "badges, team photos, and the partner, integration and customer "
            "logos. Read-only: it is kept in sync with the platform on every "
            "startup; duplicate it to derive your own palette."
        ),
        "kind": "presentation",
        "tags": ["brand", "filigran", "presentation", BUILTIN_TAG],
        "theme": None,
        "theme_config": {
            "background": "#0a0c1c",
            "surface": "#0d172b",
            "line": "#1f3965",
            "foreground": "#fbfbfb",
            # #FBFBFB at 60%.
            "muted": "#fbfbfb99",
            "accent": "#0fbcff",
            "info": "#0fbcff",
            "success": "#4caf50",
            "warning": "#f9a825",
            "error": "#f44336",
            # G5, the block gradient on dark: Sky Blue -> Aqua (the second accent).
            "gradient_from": "#0fbcff",
            "gradient_to": "#00f1bd",
            # The chart palette (spec 3.9): six ordered series in
            # brand colours, never a grey and none of the feedback tones.
            "chart_1": "#0fbcff",
            "chart_2": "#00f1bd",
            "chart_3": "#3378ff",
            "chart_4": "#9fe4ff",
            "chart_5": "#16ebf9",
            "chart_6": "#7587ff",
            # The brand hues: the colours an architecture node or zone
            # wears through `color` (XTM One, OpenCTI, OpenAEV, OpenCRQ).
            # brand_1 is the XTM One product purple (decision of
            # 3 Nov 2026, a deviation from the "no purple" rule): 4.74:1
            # on #0A0C1C and 4.37:1 on #0D172B. brand_2-4 stay in the brand
            # extended palette (Sky Blue, Royal Blue 60%, Sky Blue 40%); no
            # yellow or orange, and no purple in the chart series.
            "brand_1": "#a64dff",
            "brand_2": "#0fbcff",
            "brand_3": "#6676e9",
            "brand_4": "#9fe4ff",
            "heading_font": "'Geologica', system-ui, sans-serif",
            "body_font": "'IBM Plex Sans', system-ui, sans-serif",
            "mono_font": "'IBM Plex Mono', ui-monospace, monospace",
        },
        "aspect_ratio": "16:9",
        "extra_metadata": {
            # A deck theme is a colour SCHEME; the brand is this
            # template. Both schemes are declared: dark is what the
            # base palette is designed for, light is the variant below.
            "supported_schemes": ["dark", "light"],
            # The corporate deck runs long: a board pack or a QBR
            # that splits its dense dashboards as the layout rules ask
            # outgrows the platform default, so the brand deck ships with
            # its own cap, within the ceiling.
            "max_slides": 150,
            "deck_defaults": {
                "theme": "dark",
                "aspect_ratio": "16:9",
                "transition": "slide",
                # The Filigran PowerPoint opens and closes on the brand cover:
                # the gradient wash with the mark as a watermark, on
                # a dark slide whatever the deck's scheme. Slide-level
                # defaults fill only what the agent left unset, so an explicit
                # `hero: "plain"` or `theme` on the slide wins.
                "hero_defaults": {"title": "gradient", "closing": "gradient"},
                "slide_themes": {"title": "dark", "closing": "dark"},
            },
            "theme_variants": {
                # Ice blue #E6F7FF page, pure white #FFFFFF default cards
                # (Sam's decision over the spec's #FBFBFB, which reads
                # grey on the page) stroked with Royal Blue at 20%, Jaguar
                # ink (muted at 65%), Royal Blue accent, Navy as the second
                # accent (the info tone; Aqua only on fills, never text), the
                # G6 block gradient (Royal Blue -> Sky Blue). Fonts and logos
                # are inherited from the base.
                "light": {
                    "background": "#e6f7ff",
                    "surface": "#ffffff",
                    # #001BDA at 20%.
                    "line": "#001bda33",
                    "foreground": "#00020c",
                    # #00020C at 65%.
                    "muted": "#00020ca6",
                    "accent": "#001bda",
                    "info": "#071578",
                    "success": "#4caf50",
                    "warning": "#f9a825",
                    "error": "#d32f2f",
                    # The tones as TEXT (Brand decision A): the fills
                    # above stay on fills, borders, bars and chart series;
                    # a title, list item, badge, delta or table cell drawn in
                    # a tone takes these, at 4.5:1 or more on every light
                    # surface (#E6F7FF page, #FFFFFF card, #DAF1FF, the 4%
                    # callout and 9% table tints, and the 16% org chart seat
                    # tile behind initials): #F9A825 is 1.9:1 and #4CAF50
                    # 2.7:1 on the card. Success and error are darker still:
                    # #2D7B31 / #CC2B2B fell to
                    # 4.40 and 4.04 on their own 16% tile.
                    "success_text": "#256628",
                    "warning_text": "#a05a00",
                    "error_text": "#a82626",
                    "gradient_from": "#001bda",
                    "gradient_to": "#0fbcff",
                    # Spec 3.9, light: brand colours only, never a grey.
                    "chart_1": "#001bda",
                    "chart_2": "#00b58e",
                    "chart_3": "#071578",
                    "chart_4": "#0fbcff",
                    "chart_5": "#3378ff",
                    "chart_6": "#7587ff",
                    # The product hues read for paper: the XTM One purple
                    # (7.40:1 on #E6F7FF, 7.85:1 on #FFFFFF), then
                    # Royal Blue, Sky Blue shade, Navy.
                    "brand_1": "#5e1ad5",
                    "brand_2": "#001bda",
                    "brand_3": "#0b8dbf",
                    "brand_4": "#071578",
                },
            },
            "guidance": _BRAND_GUIDANCE,
            # Spec 3.3: no large watermark; the mark is the corner emblem of
            # every body slide, and the footer reads title, event and
            # classification separated by middle dots.
            "chrome": _FILIGRAN_BRAND_CHROME,
            # Spec 3.0 / 3.4 / 3.5 / 3.6: the flat design. No shadow
            # or light effect on any block, no eyebrow, strokes in one colour
            # (the pricing tier and a highlighted callout keep their gradient
            # border), radius 12 / 8 / 4 / 0, the 8 px grid of the 1920 x 1080
            # canvas, the body top-aligned under the title.
            "design": _FILIGRAN_BRAND_DESIGN,
            "brand": {
                "source": "filigran-design-system + filigran.io",
                "products": ["OpenCTI", "OpenAEV", "OpenCRQ", "XTM One", "XTM Hub"],
            },
        },
    },
    {
        # The THREAD conference deck: the identity of filigran.io/thread for
        # the talks given at THREAD, Filigran's conference, every colour taken
        # from the kit's Color Set (``THREAD_COLOR_SET``). Black
        # Anthracite paper with neutral cards and hairlines, Alabaster ink,
        # Cyan as the accent, the cool gradient Lime -> Blue, six mosaic
        # colours as the chart series (so the brand mosaic covers are drawn
        # in them), Elms Sans display over IBM Plex Sans / Mono. The four
        # product colours of an architecture box are the set's nearest:
        # Purple (XTM One), Cyan (OpenCTI), Orange (OpenAEV), Blue (OpenCRQ).
        # Dark first, like the stage; the ``light`` variant is the same set on
        # Alabaster. Copilot only: it includes the Filigran
        # packs - team photos, customer and partner logos - that the Filigran
        # deck carries.
        "slug": "thread-brand",
        "platform_targets": ["copilot"],
        "name": "THREAD Conference Deck",
        "description": (
            "The THREAD conference house style for keynotes, panels and workshops: "
            "near-black stage surfaces (a light palette when the deck theme is "
            "light), THREAD cyan and the lime-to-blue cool gradient, Elms Sans and "
            "IBM Plex typefaces, 16:9, covers on the marketing kit's cascade of "
            "pixel tiles with the lockup large in the corner, the official 'thread- "
            "powered by Filigran' lockup in the chrome and the mosaic 't' emblem in "
            "the corner of the content and section slides. Ships the THREAD identity "
            "from the marketing kit (lockup in white and in dark ink, "
            "emblem, app icon, mosaic artworks) and every Filigran predefined asset: product "
            "logos, architecture and flow diagrams, product screenshots, badges, "
            "team photos, and the partner, integration and customer logos. "
            "Read-only: it is kept in sync with the platform on every startup — "
            "duplicate it to derive your own palette."
        ),
        "kind": "presentation",
        "tags": ["brand", "thread", "conference", "filigran", "presentation", BUILTIN_TAG],
        "theme": None,
        "theme_config": {
            "background": _T["black_anthracite"],
            "surface": "#131313",
            "line": "#2a2a2a",
            "foreground": _T["alabaster"],
            "muted": "#aeaeae",
            "accent": _T["cyan"],
            "info": _T["blue"],
            "success": _T["mint"],
            "warning": _T["orange"],
            "error": _T["coral"],
            "gradient_from": _T["lime"],
            "gradient_to": _T["blue"],
            # Six of the set's mosaic colours, ordered so neighbours never
            # collapse: Cyan (the accent), Purple, Lime, Blue, Orange, Pink.
            # Coral is the error tone; Mauve, Mint and Peach sit too close to
            # Purple, Lime and Orange to tell two series apart.
            "chart_1": _T["cyan"],
            "chart_2": _T["purple"],
            "chart_3": _T["lime"],
            "chart_4": _T["blue"],
            "chart_5": _T["orange"],
            "chart_6": _T["pink"],
            "brand_1": _T["purple"],
            "brand_2": _T["cyan"],
            "brand_3": _T["orange"],
            "brand_4": _T["blue"],
            "heading_font": "'Elms Sans', 'Geologica', system-ui, sans-serif",
            "body_font": "'IBM Plex Sans', system-ui, sans-serif",
            "mono_font": "'IBM Plex Mono', ui-monospace, monospace",
        },
        "aspect_ratio": "16:9",
        "extra_metadata": {
            "supported_schemes": ["dark", "light"],
            # The marketing kit's pixel-tile covers: THREAD's own
            # artwork, offered by no other built-in.
            "heroes": ["mosaic", "cascade", "horizon", "pillar"],
            "deck_defaults": {
                "theme": "dark",
                "aspect_ratio": "16:9",
                "transition": "fade",
                "hero_defaults": {"title": "cascade", "closing": "cascade"},
            },
            "theme_variants": {
                # The same set on paper, as the kit lays it on white: an
                # Alabaster page, white cards, Black Anthracite ink, Cobalt
                # Blue - the set's one colour that reads as text on white
                # (9.4:1) - as the accent and the start of the gradient (Cobalt
                # -> Blue), Blue and Coral for info and error (3:1, slide-size
                # text), the mosaic in its own colours exactly, the most
                # legible first for a chart drawn on white. Fonts and the
                # emblem are the base's; the logo is the lockup in dark ink
                # (``logo_light``).
                "light": {
                    "background": _T["alabaster"],
                    "surface": "#ffffff",
                    "line": "#dedede",
                    "foreground": _T["black_anthracite"],
                    "muted": "#666666",
                    "accent": _T["cobalt_blue"],
                    "info": _T["blue"],
                    "success": _THREAD_LIGHT_SHADES["success"],
                    "warning": _THREAD_LIGHT_SHADES["warning"],
                    "error": _T["coral"],
                    "gradient_from": _T["cobalt_blue"],
                    "gradient_to": _T["blue"],
                    "chart_1": _T["blue"],
                    "chart_2": _T["purple"],
                    "chart_3": _T["orange"],
                    "chart_4": _T["pink"],
                    "chart_5": _T["cyan"],
                    "chart_6": _T["lime"],
                    # The product colours on paper: Cobalt Blue for OpenCRQ,
                    # Blue for OpenCTI (Cyan is too pale for a box border).
                    "brand_1": _T["purple"],
                    "brand_2": _T["blue"],
                    "brand_3": _T["orange"],
                    "brand_4": _T["cobalt_blue"],
                },
            },
            "guidance": _THREAD_GUIDANCE,
            "chrome": _THREAD_CHROME,
            "brand": {
                "source": "filigran.io/thread (website .conference-theme-thread) + filigran-design-system",
                "products": ["OpenCTI", "OpenAEV", "OpenCRQ", "XTM One", "XTM Hub"],
            },
        },
    },
    {
        # The XTM One product deck. Values are the platform's own design tokens:
        # the XTM One purple accent (``--cyan`` under ``[data-theme="xtm-one"]``
        # in ``frontend/src/index.css``) over the dark card surface. Every mode:
        # it carries the product's own logos and the public product
        # material - nothing of Filigran's customers, partners or
        # people - so it is the working example no platform goes without.
        "slug": "xtm-one-brand",
        "platform_targets": ["global"],
        "name": "XTM One Brand Deck",
        "description": (
            "XTM One product template for agent-generated presentations: "
            "product palette (purple accent on the dark surface), product "
            f"typefaces, 16:9. {_XTM_PRODUCT_LIBRARY} Read-only: it is kept in sync with the platform on "
            "every startup — duplicate it to derive your own palette."
        ),
        "kind": "presentation",
        "tags": ["brand", "xtm-one", "presentation", BUILTIN_TAG],
        "theme": None,
        "theme_config": {
            "background": "hsl(220 54% 8%)",
            "surface": "hsl(220 45% 12%)",
            "line": "hsl(220 30% 22%)",
            "foreground": "hsl(0 0% 100%)",
            "muted": "hsl(230 4% 70%)",
            # XTM One purple (#8c3dff), the accent of the product's own theme.
            "accent": "hsl(270 100% 65%)",
            "info": "hsl(197 100% 53%)",
            "success": "hsl(142 70% 45%)",
            "warning": "hsl(32 95% 55%)",
            "error": "hsl(4 88% 58%)",
            "gradient_from": "hsl(270 100% 65%)",
            "gradient_to": "hsl(197 100% 53%)",
            # Chart palette: the product purple first, then the cyan,
            # tonic turquoise, yellow, orange, darkblue - no feedback tone.
            "chart_1": "hsl(270 100% 65%)",
            "chart_2": "hsl(197 100% 53%)",
            "chart_3": "#00f0bc",
            "chart_4": "#f2be3a",
            "chart_5": "#f2933a",
            "chart_6": "#7587ff",
            # The brand hues: the platform's product colours, as on
            # the Filigran deck - XTM One purple (this deck's accent),
            # OpenCTI blue, OpenAEV orange, OpenCRQ blue.
            "brand_1": "hsl(270 100% 65%)",
            "brand_2": "#0fbcff",
            "brand_3": "#f2933a",
            "brand_4": "#7587ff",
            "heading_font": "'Geologica', system-ui, sans-serif",
            "body_font": "'IBM Plex Sans', system-ui, sans-serif",
            "mono_font": "'IBM Plex Mono', ui-monospace, monospace",
        },
        "aspect_ratio": "16:9",
        "extra_metadata": {
            # Dark only: no light variant, and the declaration says so.
            "supported_schemes": ["dark"],
            "deck_defaults": {
                "theme": "dark",
                "aspect_ratio": "16:9",
                "transition": "slide",
                # The product deck opens and closes on the gradient cover too;
                # dark only, so no slide theme to declare.
                "hero_defaults": {"title": "gradient", "closing": "gradient"},
            },
            "guidance": _XTM_ONE_GUIDANCE,
            "chrome": _BRAND_CHROME,
        },
    },
]


# ── Video templates ──────────────────────────────────────────────────
#
# A brand's video wears the palette of its deck: each video template takes its
# deck's ``theme_config`` (and light variant) by reference, so a colour is
# declared once per brand. The Filigran videos are the one exception:
# they keep the palette they had when the Filigran deck moved to the PPT
# Design System, declared once below for all of them. What a video template adds is its rules
# (``extra_metadata.video``, ``video_studio.templates.VideoTemplateStyle``):
# the look it presets, the intro and outro cards it adds, the choices it locks.
# Its assets are its deck's: ``brand_assets/<slug>/manifest.json``
# includes the brand's packs, never copies them, and lists the marks its cards
# and watermark draw under captions of their own.


def _deck_palette(slug: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The base palette and the scheme variants of the brand deck *slug*."""
    deck = next(entry for entry in BUILTIN_TEMPLATE_CONTAINERS if entry["slug"] == slug)
    return dict(deck["theme_config"]), dict(deck["extra_metadata"].get("theme_variants") or {})


#: How a product recording sits on every brand video: a browser window on the
#: brand backdrop, the camera following what is clicked and typed, a drawn
#: cursor, the waits compressed.
_RECORDING_LOOK: dict[str, Any] = {
    "camera": {"mode": "auto", "max_zoom": 1.7},
    "cursor": {"visible": True, "style": "arrow", "size": 1.35, "click_effect": "ripple"},
    "frame": {"style": "window", "url_bar": True, "padding": 0.02, "radius": 18, "shadow": True},
    "idle": {"mode": "compress"},
}

# The art direction of the AI shots (``VideoTemplateStyle.shots``):
# the platform adds the style to the prompt of every shot drawn from text and
# the avoided terms to the negative prompt of a model that takes one, and a
# made shot showing one of them says so. It steers the shots, never limits
# how many a video has: a video may be AI shots, demos and animated pictures
# alone. The avoided terms are the stock imagery of a generated "cyber"
# picture and never text or a logo: a shot made from a real screenshot
# carries both.
_AI_CLICHES: list[str] = [
    "laser beams",
    "lens flares",
    "starfields",
    "glowing particle networks",
    "glowing nodes and links",
    "light trails",
    "neon wireframes",
    "holograms",
    "floating binary code",
    "digital rain",
    "sci-fi HUD overlays",
    "glowing globes with arcs",
    "cities drawn as glowing grids",
    "hooded hackers",
    "glowing padlocks and shields",
    "plastic CGI look",
]


def _shot_style(palette: str) -> str:
    """A brand's shots: filmed, not rendered - real light, places and materials, in the brand's tones."""
    return (
        "Photographic and grounded, as if filmed with a real cinema camera: natural or practical light, real places "
        "and materials, people candid and natural rather than posing, a shallow depth of field and a slow, steady "
        f"camera. {palette} No lettering, logos, symbols or invented screens drawn in the shot."
    )


_FILIGRAN_SHOT_STYLE = _shot_style("Deep navy shadows with restrained cyan and turquoise highlights, never saturated neon.")
_THREAD_SHOT_STYLE = _shot_style(
    "Near-black shadows with stage-light accents in cyan, purple and lime, bold but never saturated neon."
)
_XTM_ONE_SHOT_STYLE = _shot_style("Deep navy shadows with restrained purple and cyan highlights, never saturated neon.")


def _shots(style: str) -> dict[str, Any]:
    return {"style": style, "avoid": list(_AI_CLICHES)}

_FILIGRAN_VIDEO_GUIDANCE = (
    "A demo narrated by a product manager: value and mechanism, never the action on screen; camera static unless "
    "a detail needs it. Open on what is new and why it is better, then 2-5 recordings of the real UI, one "
    "capability each (8-18 s), and close on the call to action the template adds. Each scene says how the "
    "capability works and what it gives in one or two sentences, never what is clicked or written on screen; name "
    "the product (OpenCTI, OpenAEV, XTM One) the first time it appears and an entity twice at most in the whole "
    "video. One callout per scene at most, the capability's name only; a lower third names a screen, never repeats "
    "the narration."
)
_THREAD_VIDEO_GUIDANCE = (
    "A THREAD video is played on stage and shared after the conference: a teaser, a session opener, a product "
    "announcement. Keep it short (30-90 s), the narration energetic and the sentences few; let the mosaic cards "
    "carry the identity and the recordings the proof. Name the session type in the intro's kicker "
    "(template_scenes.intro.kicker: KEYNOTE, WORKSHOP) when it is not the conference itself, and close on the outro "
    "the template adds."
)
_XTM_ONE_VIDEO_GUIDANCE = (
    "An XTM One video walks a colleague through a workflow of the platform: one goal, the steps in the order "
    "they are done, a narration that says why each step matters. Open on the title card the template adds; "
    "close on your own outro card when there is a next step to give."
)

# The Filigran videos keep the design-system palette the brand deck wore until
# the PPT Design System redesign: the deck's new tokens are a deck
# decision, the video templates are out of its scope and keep their look.
_FILIGRAN_VIDEO_PALETTE: dict[str, Any] = {
    "background": "#070d18",
    "surface": "#0d172b",
    "line": "#1f3965",
    "foreground": "#f2f2f3",
    "muted": "#afb0b6",
    "accent": "#42caff",
    "info": "#42caff",
    "success": "#17ab1f",
    "warning": "#e6700f",
    "error": "#f14337",
    "gradient_from": "#42caff",
    "gradient_to": "#00f0bc",
    "chart_1": "#42caff",
    "chart_2": "#00f0bc",
    "chart_3": "#a47af0",
    "chart_4": "#f2be3a",
    "chart_5": "#f2933a",
    "chart_6": "#7587ff",
    "brand_1": "hsl(270 100% 65%)",
    "brand_2": "#0fbcff",
    "brand_3": "#f2933a",
    "brand_4": "#7587ff",
    "heading_font": "'Geologica', system-ui, sans-serif",
    "body_font": "'IBM Plex Sans', system-ui, sans-serif",
    "mono_font": "'IBM Plex Mono', ui-monospace, monospace",
}
_FILIGRAN_VIDEO_VARIANTS: dict[str, Any] = {
    "light": {
        "background": "#f2f2f3",
        "surface": "#ffffff",
        "line": "#cacbce",
        "foreground": "#18191b",
        "muted": "#494a50",
        "accent": "#0015a8",
        "info": "#0079a8",
        "success": "#117916",
        "warning": "#b8550a",
        "error": "#b8180a",
        "gradient_from": "#0015a8",
        "gradient_to": "#009474",
        "chart_1": "#0015a8",
        "chart_2": "#009474",
        "chart_3": "#651fe5",
        "chart_4": "#eaa510",
        "chart_5": "#e6700f",
        "chart_6": "#0079a8",
        "brand_1": "#5e1ad5",
        "brand_2": "#0079a8",
        "brand_3": "#e6700f",
        "brand_4": "#0015a8",
    }
}
_THREAD_VIDEO_PALETTE, _ = _deck_palette("thread-brand")
_XTM_ONE_VIDEO_PALETTE, _ = _deck_palette("xtm-one-brand")

# How each built-in video moves (``theme.motion``): a personality and the brand's signature on its cards - the
# logo uncovered from its left, a light crossing the accent words -, the cuts on the music's beat where music carries
# the film, motion blur on the short films (it is drawn at the high and ultra qualities only, at three times the
# frames). A brand template locks its personality; a video still sets its own speed, beat sync and motion blur.
_FILIGRAN_MOTION: dict[str, Any] = {"personality": "precise", "logo": "mask", "accent": "sweep"}
_THREAD_MOTION: dict[str, Any] = {"personality": "energetic", "beat_sync": "cuts", "motion_blur": "on"}
_XTM_ONE_MOTION: dict[str, Any] = {"personality": "precise", "accent": "sweep"}
_FILIGRAN_FILM_MOTION: dict[str, Any] = {
    "personality": "cinematic",
    "logo": "mask",
    "accent": "sweep",
    "beat_sync": "cuts",
    "motion_blur": "on",
}
_EVENT_LOOP_MOTION: dict[str, Any] = {"personality": "energetic", "logo": "mask", "beat_sync": "all", "motion_blur": "on"}
_XTM_ONE_FILM_MOTION: dict[str, Any] = {"personality": "cinematic", "accent": "sweep", "beat_sync": "cuts", "motion_blur": "on"}
_BRIEFING_MOTION: dict[str, Any] = {"personality": "calm"}
_SOCIAL_MOTION: dict[str, Any] = {
    "personality": "energetic",
    "speed": 1.15,
    "title": "words",
    "beat_sync": "all",
    "motion_blur": "on",
}
_MOTION_LOCK = "theme.motion.personality"

BUILTIN_TEMPLATE_CONTAINERS.extend(
    [
        {
            # The Filigran product video: the brand deck's palette (both
            # schemes), the aurora backdrop, the mark as a watermark, every
            # recording in the same browser window, a Filigran title card and
            # a "Book a demo" closing card on every video. Copilot only, like
            # the brand deck: it is Filigran's own marketing voice.
            "slug": "filigran-video",
            "platform_targets": ["copilot"],
            "name": "Filigran Product Video",
            "description": (
                "The Filigran house style for product videos: the brand palette on the aurora backdrop (a light "
                "palette for a light video), Geologica and IBM Plex, the Filigran mark as a watermark, product "
                "recordings framed in the same browser window, a Filigran title card and a 'Book a demo' closing "
                f"card added to every video. {_FILIGRAN_LIBRARY} Read-only: kept in sync with the platform on "
                "every startup - duplicate it to derive your own."
            ),
            "kind": "video",
            "tags": ["brand", "filigran", "video", BUILTIN_TAG],
            "theme": None,
            "theme_config": _FILIGRAN_VIDEO_PALETTE,
            "aspect_ratio": "16:9",
            "extra_metadata": {
                "supported_schemes": ["dark", "light"],
                "theme_variants": _FILIGRAN_VIDEO_VARIANTS,
                "guidance": _FILIGRAN_VIDEO_GUIDANCE,
                "video": {
                    "aspect_ratios": ["16:9", "9:16", "1:1", "4:5"],
                    "purposes": ["product_demo"],
                    "defaults": {
                        # A demo told as a product manager tells it live: calm moves and calm crossfades, the
                        # footage at its own speed.
                        "pace": "calm",
                        "theme": {"mode": "dark", "background": "aurora", "watermark": "bottom_right", "motion": _FILIGRAN_MOTION},
                        "default_transition": {"type": "crossfade", "pace": "calm"},
                        "captions": {"enabled": True, "burn_in": True, "style": "pill", "position": "bottom"},
                        "voiceover": {
                            "style": (
                                "A product manager presenting the product live to peers: warm, confident and "
                                "clear, never salesy."
                            ),
                            "speed": 1.0,
                        },
                    },
                    "scene_defaults": {
                        # Centred on the aurora: set left, the block leaves the right half of the frame empty.
                        "title": {"style": "aurora", "align": "center", "show_logo": True},
                        "recording": {
                            **_RECORDING_LOOK,
                            # Still unless a scene asks for a detail, and then a calm, shallow move.
                            "camera": {"mode": "static", "pace": "calm", "max_zoom": 1.3, "move_seconds": 2.0},
                        },
                        "slide": {"motion": "ken_burns"},
                        "image": {"motion": "ken_burns"},
                        "outro": {"style": "aurora", "show_logo": True},
                    },
                    "overlay_defaults": {"callout": {"style": "glass", "duration": 3.0}, "badge": {"style": "accent"}},
                    # The card's logo spells Filigran: no kicker says it again.
                    "intro": {"required": True, "scene": {"duration": 3.6}},
                    "brand_name": "Filigran",
                    "outro": {
                        "required": True,
                        "scene": {
                            "title": "See it on your own data",
                            "cta": "Book a demo",
                            "url": "filigran.io",
                            "duration": 6.0,
                        },
                    },
                    "locked": [
                        "theme.palette",
                        "theme.background",
                        "theme.watermark",
                        "captions.style",
                        "scenes.recording.frame",
                        "scenes.recording.cursor.style",
                        _MOTION_LOCK,
                    ],
                    "shots": _shots(_FILIGRAN_SHOT_STYLE),
                },
                "brand": {"source": "filigran-design-system + filigran.io", "deck": "filigran-brand"},
            },
        },
        {
            # The THREAD conference video: the THREAD deck's palette, dark
            # like the stage, the cards and the recordings drawn over the
            # THREAD pixel mosaic in the Color Set's six series colours, the
            # mosaic 't' emblem as a watermark, the edition as the intro's
            # kicker and a "See you there" closing card under the lockup,
            # which already spells THREAD and its Filigran baseline. Copilot only.
            "slug": "thread-video",
            "platform_targets": ["copilot"],
            "name": "THREAD Conference Video",
            "description": (
                "The THREAD conference style for teasers, session openers and announcements: the THREAD Color "
                "Set on near-black, Elms Sans and IBM Plex, every card and recording drawn over the THREAD pixel "
                "mosaic, the mosaic 't' emblem as a watermark, the edition on the title card and a 'See you there' "
                "closing card under the THREAD lockup. Ships every predefined asset of the THREAD deck: the THREAD "
                "lockups, emblem and mosaic artworks, and the Filigran product logos, diagrams, screenshots, badges, "
                "photos and partner, integration and customer logos. Read-only: kept in sync with the platform on "
                "every startup - duplicate it to derive your own."
            ),
            "kind": "video",
            "tags": ["brand", "thread", "conference", "filigran", "video", BUILTIN_TAG],
            "theme": None,
            "theme_config": _THREAD_VIDEO_PALETTE,
            "aspect_ratio": "16:9",
            "extra_metadata": {
                "supported_schemes": ["dark"],
                "guidance": _THREAD_VIDEO_GUIDANCE,
                "video": {
                    "aspect_ratios": ["16:9", "9:16", "1:1"],
                    "purposes": ["event", "launch"],
                    "defaults": {
                        "theme": {"mode": "dark", "background": "mosaic", "watermark": "top_right", "motion": _THREAD_MOTION},
                        "default_transition": {"type": "auto", "pace": "dynamic"},
                        "captions": {"enabled": True, "burn_in": True, "style": "bar", "position": "bottom"},
                        "voiceover": {
                            "style": (
                                "The host of a security conference opening a session: bright, energetic and "
                                "crisp, with a confident pace."
                            ),
                            "speed": 1.05,
                        },
                    },
                    "scene_defaults": {
                        "title": {"style": "aurora", "align": "left", "show_logo": True},
                        "recording": {
                            **_RECORDING_LOOK,
                            "camera": {"mode": "auto", "max_zoom": 1.8},
                            "cursor": {"visible": True, "style": "halo", "size": 1.4, "click_effect": "pulse"},
                            "frame": {
                                "style": "window",
                                "url_bar": True,
                                "padding": 0.03,
                                "radius": 14,
                                "shadow": True,
                            },
                        },
                        "figure": {"perspective": "tilt"},
                        "outro": {"style": "aurora", "show_logo": True},
                    },
                    "overlay_defaults": {"callout": {"style": "dark"}, "badge": {"style": "accent"}},
                    "intro": {"required": True, "scene": {"kicker": "2026", "duration": 3.4}},
                    "brand_name": "THREAD",
                    "outro": {
                        "required": True,
                        "scene": {"title": "See you there", "url": "filigran.io/thread", "duration": 6.0},
                    },
                    "locked": [
                        "theme.palette",
                        "theme.background",
                        "theme.watermark",
                        "captions.style",
                        "scenes.recording.frame",
                        "scenes.recording.cursor",
                        _MOTION_LOCK,
                    ],
                    "shots": _shots(_THREAD_SHOT_STYLE),
                },
                "brand": {"source": "filigran.io/thread + the THREAD design kit", "deck": "thread-brand"},
            },
        },
        {
            # The XTM One product video, on every platform: the product deck's
            # palette, the mesh backdrop, the same browser window for every
            # recording and an XTM One title card. Fewer locks than the
            # Filigran ones: it is the example every install starts from.
            "slug": "xtm-one-video",
            "platform_targets": ["global"],
            "name": "XTM One Product Video",
            "description": (
                "XTM One product style for walkthrough videos: the product palette (purple accent on the dark "
                "surface) on the mesh backdrop, the product typefaces, recordings framed in the same browser "
                f"window, an XTM One title card added to every video. {_XTM_PRODUCT_LIBRARY} Read-only: kept in "
                "sync with the platform on every startup - duplicate it to derive your own."
            ),
            "kind": "video",
            "tags": ["brand", "xtm-one", "video", BUILTIN_TAG],
            "theme": None,
            "theme_config": _XTM_ONE_VIDEO_PALETTE,
            "aspect_ratio": "16:9",
            "extra_metadata": {
                "supported_schemes": ["dark"],
                "guidance": _XTM_ONE_VIDEO_GUIDANCE,
                "video": {
                    "aspect_ratios": ["16:9", "9:16", "1:1", "4:5"],
                    "purposes": ["product_demo"],
                    "defaults": {
                        "theme": {"mode": "dark", "background": "mesh", "watermark": "bottom_right", "motion": _XTM_ONE_MOTION},
                        "default_transition": {"type": "auto", "pace": "calm"},
                        "captions": {"enabled": True, "burn_in": True, "style": "pill", "position": "bottom"},
                        "voiceover": {
                            "style": "A clear, friendly product walkthrough voice: neutral, precise, unhurried.",
                            "speed": 1.0,
                        },
                    },
                    "scene_defaults": {
                        "title": {"style": "aurora", "align": "center", "show_logo": True},
                        "recording": _RECORDING_LOOK,
                        "outro": {"style": "aurora", "show_logo": True},
                    },
                    "intro": {"required": True, "scene": {"duration": 3.4}},
                    "brand_name": "XTM One",
                    "outro": {"required": False, "scene": {"style": "aurora", "duration": 4.5}},
                    "locked": ["theme.palette", "scenes.recording.frame"],
                    "shots": _shots(_XTM_ONE_SHOT_STYLE),
                },
                "brand": {"source": "frontend design tokens", "deck": "xtm-one-brand"},
            },
        },
    ]
)


# ── Videos that are not product demos ──────────────────────────────
#
# The brand film, the event loop, the launch teaser and the threat briefing
# carry a message, not a workflow: statements in big type (the voice of the
# Filigran booth and launch films), AI shots and extracts of event films as
# their pictures, a product shown as proof, never as the plot. Their assets
# are their deck's, as for the product videos above, so the assistant has the
# brand's own material at hand (``brand_assets/<slug>/manifest.json`` includes
# the packs).

#: How a statement reads on every brand video: the lines one after the other,
#: an AI shot or a film behind it darkened enough for the type to read.
_STATEMENT_LOOK: dict[str, Any] = {"reveal": "lines", "align": "center", "dim": 0.6, "duration": 4.0}

_FILIGRAN_BRAND_FILM_GUIDANCE = (
    "A Filigran brand film tells one story in 60-180 s, the way the Filigran booth and launch films do: open on a "
    "tension in two or three statements ('Too much *noise*.' / 'Not enough *context*.'), answer it with Filigran "
    "(the XTM platform, the open-source community, the proof: figures, customers, events), and close on the tagline "
    "the template adds. Statements carry the message, one idea each, 3-8 words a line, the key word in *accent*. "
    "Pictures: AI shots made for this video (video_id) so they take the template's look - their prompts say the "
    "subject, the action and the camera, never a style -, product recordings and screenshots as the proof (a screen "
    "in a shot showing the real product, compose_picture), the template's team, event and product pictures "
    "(get_template lists them) animated, and extracts of event films when the user has some. A film of AI shots, "
    "demos and animated pictures alone is a complete film. Motion scenes carry the brand's own motion: a kinetic_title "
    "for the claim, a stat_counter for a figure, a logo_reveal to sign off. Narration is optional: a brand film often "
    "runs on music and statements alone."
)
_FILIGRAN_EVENT_LOOP_GUIDANCE = (
    "An event loop plays silently on a booth screen people walk past: 25-90 s, no narration, every message on screen. "
    "One idea per scene, 3-4 s each: short statements (3-7 words, the key word in *accent*) alternating with AI shots "
    "made for this video (video_id) so they take the template's look, product screenshots and event pictures; repeat "
    "the brand, end on the tagline the template adds; motion scenes (split_statement, stat_counter, constellation) keep "
    "the screen moving between the pictures. It loops, so its last scene leads back to its first."
)
_XTM_ONE_BRAND_FILM_GUIDANCE = (
    "A brand film tells one story in 45-150 s: a tension stated in a few big statements, the answer, the proof "
    "(figures counted up, chips for what sets it apart, product recordings and screenshots, footage the user has), a "
    "closing call to action. AI shots made for this video (video_id) carry the pictures in the template's look; a "
    "product recording is proof, never the plot; motion scenes (kinetic_title, stat_counter, logo_reveal) move in the "
    "brand's own motion. Narrate when the story needs a voice; let the statements speak when it does not."
)
_THREAT_BRIEFING_GUIDANCE = (
    "A threat briefing explains one threat to a security audience in 60-120 s, from the data the assistant has "
    "(intelligence platforms, reports, the web): what happened (a figure: victims, countries, severity), who is "
    "behind it and how (techniques as chips, the MITRE ATT&CK ids), what is at risk and what to do. The numbers and "
    "the timeline go in statements, a deck slide or a figure scene; the intelligence platform's own screens (a "
    "recording or a screenshot of the report, the graph, the indicators) are the evidence when they can be shown; "
    "AI shots made for this video (video_id) picture the rest - the places, the people and the work a threat "
    "touches, filmed rather than drawn, never a real logo or person; a stat_counter motion scene states the headline "
    "figure, a list_build what to do. Narrate in a calm analyst voice, one fact per sentence."
)
_SOCIAL_TEASER_GUIDANCE = (
    "A social teaser is 15-30 s and vertical first: hook in the first two seconds with a statement or an AI shot, "
    "one idea per 2-3 s scene (a kinetic_title motion scene makes a hook), words revealed one by one, captions "
    "burned in (most people watch muted), and a clear call to action at the end."
)

BUILTIN_TEMPLATE_CONTAINERS.extend(
    [
        {
            # The Filigran brand film: the brand palette, statements over AI
            # shots and event footage, the Filigran library as assets, the
            # "Uncover threats. Take action." close. Copilot only.
            "slug": "filigran-brand-film",
            "platform_targets": ["copilot"],
            "name": "Filigran Brand Film",
            "description": (
                "The Filigran house style for brand films, launch films and company introductions - not product "
                "demos: big statements revealed line by line over AI shots and event footage, the brand palette on "
                "the aurora backdrop and the 'Uncover threats. Take action.' closing card. "
                f"{_FILIGRAN_LIBRARY} Read-only: kept in sync with the platform on every startup - duplicate it to "
                "derive your own."
            ),
            "kind": "video",
            "tags": ["brand", "filigran", "video", "brand-film", BUILTIN_TAG],
            "theme": None,
            "theme_config": _FILIGRAN_VIDEO_PALETTE,
            "aspect_ratio": "16:9",
            "extra_metadata": {
                "supported_schemes": ["dark", "light"],
                "theme_variants": _FILIGRAN_VIDEO_VARIANTS,
                "guidance": _FILIGRAN_BRAND_FILM_GUIDANCE,
                "video": {
                    "aspect_ratios": ["16:9", "9:16", "1:1"],
                    "purposes": ["brand_film", "launch"],
                    "defaults": {
                        "theme": {"mode": "dark", "background": "aurora", "watermark": "bottom_right", "motion": _FILIGRAN_FILM_MOTION},
                        "default_transition": {"type": "auto", "pace": "classic"},
                        "captions": {"enabled": True, "burn_in": True, "style": "pill", "position": "bottom"},
                        "voiceover": {
                            "style": "A confident, cinematic brand voice: calm, assured and human, never salesy.",
                            "speed": 1.0,
                        },
                    },
                    "scene_defaults": {
                        "statement": _STATEMENT_LOOK,
                        "title": {"style": "aurora", "align": "center", "show_logo": True},
                        "recording": _RECORDING_LOOK,
                        "clip": {"fit": "cover"},
                        "image": {"motion": "ken_burns"},
                        "figure": {"perspective": "tilt"},
                        "motion": {"duration": 4.5},
                        "outro": {"style": "aurora", "show_logo": True},
                    },
                    "overlay_defaults": {"badge": {"style": "accent"}, "lower_third": {"style": "glass"}},
                    # A true opening and closing: centred cards with the logo; the watermark waits for the content.
                    # The logo spells Filigran: no kicker says it again.
                    "intro": {"required": True, "scene": {"duration": 3.6}},
                    "brand_name": "Filigran",
                    "outro": {
                        "required": True,
                        "scene": {"title": "Uncover threats.\n*Take action.*", "url": "filigran.io", "duration": 5.0},
                    },
                    "locked": ["theme.palette", "scenes.recording.frame", _MOTION_LOCK],
                    "shots": _shots(_FILIGRAN_SHOT_STYLE),
                },
                "brand": {"source": "the Filigran booth and launch films + filigran.io", "deck": "filigran-brand"},
            },
        },
        {
            # The Filigran event loop: silent, big type, loops on a booth
            # screen (16:9 and the 9:16 of a totem). Copilot only.
            "slug": "filigran-event-loop",
            "platform_targets": ["copilot"],
            "name": "Filigran Event Loop",
            "description": (
                "A silent loop for a booth or a stage screen: short statements in big type alternating with AI "
                "shots, product screenshots and event pictures, 3-4 s a scene, no narration, the brand palette and "
                f"the tagline at the end, 16:9 or vertical for a totem. {_FILIGRAN_LIBRARY} Read-only: kept in sync "
                "with the platform on every startup - duplicate it to derive your own."
            ),
            "kind": "video",
            "tags": ["brand", "filigran", "video", "event", BUILTIN_TAG],
            "theme": None,
            "theme_config": _FILIGRAN_VIDEO_PALETTE,
            "aspect_ratio": "16:9",
            "extra_metadata": {
                "supported_schemes": ["dark"],
                "guidance": _FILIGRAN_EVENT_LOOP_GUIDANCE,
                "video": {
                    "aspect_ratios": ["16:9", "9:16"],
                    "purposes": ["event"],
                    "defaults": {
                        "theme": {"mode": "dark", "background": "aurora", "watermark": "none", "motion": _EVENT_LOOP_MOTION},
                        "default_transition": {"type": "auto", "pace": "dynamic"},
                        "voiceover": {"enabled": False},
                        "captions": {"enabled": False},
                    },
                    "scene_defaults": {
                        "statement": {**_STATEMENT_LOOK, "duration": 3.5},
                        "motion": {"duration": 3.5},
                        "clip": {"fit": "cover", "audio": False},
                        "image": {"motion": "zoom_in"},
                        "recording": _RECORDING_LOOK,
                        "outro": {"style": "aurora", "show_logo": True},
                    },
                    "outro": {
                        "required": True,
                        "scene": {"title": "Uncover threats.\n*Take action.*", "url": "filigran.io", "duration": 4.5},
                    },
                    "brand_name": "Filigran",
                    "locked": ["theme.palette", _MOTION_LOCK],
                    "shots": _shots(_FILIGRAN_SHOT_STYLE),
                },
                "brand": {"source": "the Filigran booth films (ITSA, booth loop)", "deck": "filigran-brand"},
            },
        },
        {
            # The XTM One brand film, on every platform: the product palette,
            # the same statements-over-pictures grammar, no forced closing card.
            "slug": "xtm-one-brand-film",
            "platform_targets": ["global"],
            "name": "XTM One Brand Film",
            "description": (
                "A brand, launch or event film in the XTM One style - not a product walkthrough: big statements "
                "over AI shots and real footage, figures counted up, the product palette on the mesh backdrop. "
                f"{_XTM_PRODUCT_LIBRARY} Read-only: kept in sync with the platform on every startup - duplicate it "
                "to derive your own."
            ),
            "kind": "video",
            "tags": ["brand", "xtm-one", "video", "brand-film", BUILTIN_TAG],
            "theme": None,
            "theme_config": _XTM_ONE_VIDEO_PALETTE,
            "aspect_ratio": "16:9",
            "extra_metadata": {
                "supported_schemes": ["dark"],
                "guidance": _XTM_ONE_BRAND_FILM_GUIDANCE,
                "video": {
                    "aspect_ratios": ["16:9", "9:16", "1:1", "4:5"],
                    "purposes": ["brand_film", "launch", "event"],
                    "defaults": {
                        "theme": {"mode": "dark", "background": "mesh", "watermark": "bottom_right", "motion": _XTM_ONE_FILM_MOTION},
                        "default_transition": {"type": "auto", "pace": "classic"},
                        "captions": {"enabled": True, "burn_in": True, "style": "pill", "position": "bottom"},
                        "voiceover": {
                            "style": "A warm, assured narrator: confident and human, unhurried.",
                            "speed": 1.0,
                        },
                    },
                    "scene_defaults": {
                        "statement": _STATEMENT_LOOK,
                        "title": {"style": "aurora", "align": "center", "show_logo": True},
                        "recording": _RECORDING_LOOK,
                        "clip": {"fit": "cover"},
                        "figure": {"perspective": "tilt"},
                        "motion": {"duration": 4.5},
                        "outro": {"style": "aurora", "show_logo": True},
                    },
                    "intro": {"required": True, "scene": {"duration": 3.4}},
                    "brand_name": "XTM One",
                    "outro": {"required": False, "scene": {"style": "aurora", "duration": 4.5}},
                    "locked": ["theme.palette"],
                    "shots": _shots(_XTM_ONE_SHOT_STYLE),
                },
                "brand": {"source": "frontend design tokens", "deck": "xtm-one-brand"},
            },
        },
        {
            # The threat briefing, on every platform: an explainer built from
            # the data - statements with figures and techniques, AI shots of
            # the threat, a calm analyst narration.
            "slug": "threat-briefing-video",
            "platform_targets": ["global"],
            "name": "Threat Briefing",
            "description": (
                "An explainer for one threat, campaign or vulnerability, built from the intelligence the assistant "
                "has: figures and ATT&CK techniques in statements, AI shots in the template's art direction and the "
                "platform's own screens as evidence, a calm analyst narration with captions, on the grid backdrop. "
                f"{_XTM_PRODUCT_LIBRARY} Read-only: kept in sync with the platform on every startup - duplicate it "
                "to derive your own."
            ),
            "kind": "video",
            "tags": ["xtm-one", "video", "threat-intelligence", "explainer", BUILTIN_TAG],
            "theme": None,
            "theme_config": _XTM_ONE_VIDEO_PALETTE,
            "aspect_ratio": "16:9",
            "extra_metadata": {
                "supported_schemes": ["dark"],
                "guidance": _THREAT_BRIEFING_GUIDANCE,
                "video": {
                    "aspect_ratios": ["16:9", "9:16", "1:1"],
                    "purposes": ["explainer"],
                    "defaults": {
                        "theme": {"mode": "dark", "background": "grid", "watermark": "bottom_right", "motion": _BRIEFING_MOTION},
                        "default_transition": {"type": "auto", "pace": "calm"},
                        "captions": {"enabled": True, "burn_in": True, "style": "bar", "position": "bottom"},
                        "voiceover": {
                            "style": "A calm, precise threat analyst briefing peers: factual, measured, no drama.",
                            "speed": 1.0,
                        },
                    },
                    "scene_defaults": {
                        "statement": {**_STATEMENT_LOOK, "align": "left", "dim": 0.7},
                        "title": {"style": "spotlight", "align": "left", "show_logo": True},
                        "clip": {"fit": "cover"},
                        "slide": {"motion": "zoom_in"},
                        "outro": {"style": "minimal", "show_logo": True},
                    },
                    "intro": {"required": True, "scene": {"kicker": "Threat briefing", "duration": 3.4}},
                    "brand_name": "XTM One",
                    "locked": ["theme.palette"],
                    "shots": _shots(_XTM_ONE_SHOT_STYLE),
                },
                "brand": {"source": "frontend design tokens", "deck": "xtm-one-brand"},
            },
        },
        {
            # The social teaser, on every platform: vertical first, fast cuts,
            # captions burned in, words revealed one by one.
            "slug": "social-teaser-video",
            "platform_targets": ["global"],
            "name": "Social Teaser",
            "description": (
                "A 15-30 s teaser for social feeds and launches, vertical first: a hook in the first two seconds, "
                "words revealed one by one, quick cuts, captions burned in for muted viewing, a call to action at "
                f"the end. {_XTM_PRODUCT_LIBRARY} Read-only: kept in sync with the platform on every startup - "
                "duplicate it to derive your own."
            ),
            "kind": "video",
            "tags": ["xtm-one", "video", "social", BUILTIN_TAG],
            "theme": None,
            "theme_config": _XTM_ONE_VIDEO_PALETTE,
            "aspect_ratio": "9:16",
            "extra_metadata": {
                "supported_schemes": ["dark"],
                "guidance": _SOCIAL_TEASER_GUIDANCE,
                "video": {
                    "aspect_ratios": ["9:16", "1:1", "4:5", "16:9"],
                    "purposes": ["social", "launch"],
                    "defaults": {
                        "theme": {"mode": "dark", "background": "gradient", "watermark": "top_right", "motion": _SOCIAL_MOTION},
                        "default_transition": {"type": "auto", "pace": "dynamic"},
                        "captions": {"enabled": True, "burn_in": True, "style": "outline", "position": "bottom"},
                        "voiceover": {"style": "Upbeat and punchy, a quick confident pace.", "speed": 1.08},
                    },
                    "scene_defaults": {
                        "statement": {**_STATEMENT_LOOK, "reveal": "words", "duration": 2.6},
                        "motion": {"duration": 3.0},
                        "clip": {"fit": "cover"},
                        "image": {"motion": "zoom_in"},
                        "outro": {"style": "spotlight", "show_logo": True},
                    },
                    "brand_name": "XTM One",
                    "locked": ["theme.palette"],
                    "shots": _shots(_XTM_ONE_SHOT_STYLE),
                },
                "brand": {"source": "frontend design tokens", "deck": "xtm-one-brand"},
            },
        },
    ]
)


# ── Report templates ─────────────────────────────────────────────────
#
# A report that names no template wears the platform's brand
# (``reports.resolve_default_report_template_id``, read time): the Filigran
# report in Copilot mode, the XTM One report everywhere else, the same split as
# the decks. Report keys only (``reports.REPORT_THEME_CONFIG_KEYS``): the
# masthead ``background`` stays dark in both schemes (the masthead ink is
# white), ``paper`` / ``card`` / ``line`` are the document surfaces, and the
# four risk grades are drawn as TEXT on the card, so each is 4.5:1 or more on
# ``card`` and ``paper`` of its scheme. The base is the dark palette; the light
# variant (the default report theme, made for print and PDF) is complete, as
# ``presentations.scheme_palette`` drops the base's scheme-bound keys for it.

_REPORT_FONTS: dict[str, str] = {
    "heading_font": "'Geologica', system-ui, sans-serif",
    "body_font": "'IBM Plex Sans', system-ui, sans-serif",
}

BUILTIN_TEMPLATE_CONTAINERS.extend(
    [
        {
            # The Filigran report: the brand deck's palette on paper. Copilot
            # only, like the deck whose brand it carries.
            "slug": "filigran-report",
            "platform_targets": ["copilot"],
            "name": "Filigran Brand Report",
            "description": (
                "The Filigran house style for agent-generated reports, from the "
                "Filigran design system: a navy masthead, Royal Blue accents on an "
                "ice blue page with white cards for print and PDF (light, the "
                "default), Sky Blue on the #0A0C1C page for a screen readout "
                "(dark), Geologica and IBM Plex typefaces, the Filigran logos. "
                "The template a report wears when it names none, on Filigran "
                "Copilot. Read-only: it is kept in sync with the platform on every "
                "startup; duplicate it to derive your own palette."
            ),
            "kind": "report",
            "tags": ["brand", "filigran", "report", BUILTIN_TAG],
            "theme": None,
            "theme_config": {
                "background": "#0d172b",
                "foreground": "#fbfbfb",
                "accent": "#0fbcff",
                "muted": "#b9bcc6",
                "paper": "#0a0c1c",
                "card": "#0d172b",
                "line": "#1f3965",
                "critical": "#ff6f61",
                "elevated": "#f9a825",
                "moderate": "#0fbcff",
                "controlled": "#4caf50",
                **_REPORT_FONTS,
            },
            "aspect_ratio": "A4",
            "extra_metadata": {
                "supported_schemes": ["dark", "light"],
                "theme_variants": {
                    "light": {
                        # The masthead in Navy, the brand's Royal Blue on the
                        # ice blue page and white cards, Jaguar ink.
                        "background": "#071578",
                        "foreground": "#00020c",
                        "accent": "#001bda",
                        "muted": "#4b4e5c",
                        "paper": "#e6f7ff",
                        "card": "#ffffff",
                        "line": "#c7d0f5",
                        "critical": "#a82626",
                        "elevated": "#a05a00",
                        "moderate": "#071578",
                        "controlled": "#256628",
                    },
                },
                "brand": {"source": "Filigran design system", "deck": "filigran-brand"},
            },
        },
        {
            # The XTM One report: the product palette, every mode.
            "slug": "xtm-one-report",
            "platform_targets": ["global"],
            "name": "XTM One Brand Report",
            "description": (
                "XTM One product template for agent-generated reports: a dark "
                "navy masthead, the XTM One purple accent on a light page with "
                "white cards for print and PDF (light, the default), on the dark "
                "product surface for a screen readout (dark), product typefaces, "
                "the XTM One logos. The template a report wears when it names "
                "none, wherever the Filigran report does not ship. Read-only: it "
                "is kept in sync with the platform on every startup; duplicate it "
                "to derive your own palette."
            ),
            "kind": "report",
            "tags": ["brand", "xtm-one", "report", BUILTIN_TAG],
            "theme": None,
            "theme_config": {
                "background": "hsl(220 45% 12%)",
                "foreground": "hsl(0 0% 100%)",
                "accent": "hsl(270 100% 72%)",
                "muted": "hsl(230 4% 70%)",
                "paper": "hsl(220 54% 8%)",
                "card": "hsl(220 45% 12%)",
                "line": "hsl(220 30% 22%)",
                "critical": "hsl(4 88% 64%)",
                "elevated": "hsl(32 95% 55%)",
                "moderate": "hsl(197 100% 53%)",
                "controlled": "hsl(142 70% 45%)",
                **_REPORT_FONTS,
            },
            "aspect_ratio": "A4",
            "extra_metadata": {
                "supported_schemes": ["dark", "light"],
                "theme_variants": {
                    "light": {
                        # The product navy masthead, the XTM One purple read
                        # for paper (7.85:1 on white, the brand deck's
                        # light brand_1), slate ink.
                        "background": "hsl(220 54% 8%)",
                        "foreground": "hsl(220 34% 15%)",
                        "accent": "#5e1ad5",
                        "muted": "hsl(217 14% 38%)",
                        "paper": "hsl(220 30% 97%)",
                        "card": "#ffffff",
                        "line": "hsl(220 20% 88%)",
                        "critical": "#a82626",
                        "elevated": "#a05a00",
                        "moderate": "#0b5f8f",
                        "controlled": "#256628",
                    },
                },
                "brand": {"source": "frontend design tokens", "deck": "xtm-one-brand"},
            },
        },
    ]
)


def _extract_slug(container: TemplateContainer) -> str | None:
    for tag in container.tags or []:
        if isinstance(tag, str) and tag.startswith(SLUG_TAG_PREFIX):
            return tag[len(SLUG_TAG_PREFIX) :]
    return None


def builtin_containers_for(platform_mode: str) -> list[dict[str, Any]]:
    """The declared built-ins that ship in *platform_mode*.

    A video template ships only while the "VIDEO_STUDIO" feature flag is on:
    off, the seed and :func:`remove_unshipped_builtin_containers`
    remove the ones seeded while it was on, and their assets go with them.
    """
    from app.agents.seed import seed_matches_platform
    from app.core.feature_flags import is_feature_enabled

    videos = is_feature_enabled("VIDEO_STUDIO")
    return [
        data
        for data in BUILTIN_TEMPLATE_CONTAINERS
        if seed_matches_platform(data, platform_mode) and (videos or data["kind"] != "video")
    ]


async def remove_unshipped_builtin_containers(db: AsyncSession, *, platform_mode: str) -> int:
    """Delete the built-in containers *platform_mode* does not ship. Returns rows removed.

    Phase 1 of :func:`seed_builtin_template_containers` on its own, for the
    boot path that skips the seed: ``_bootstrap_platform_data`` runs this on
    every start, OUTSIDE the AI gate. A platform whose AI is switched
    off provisions no built-in content, yet it must still lose a built-in its
    mode does not carry - the Filigran Brand Deck's customer logos have no
    business on an XTM One platform whatever its AI state. A retired container
    whose heir ships here is left to the full seed, which moves its assets
    first (Phase 3).
    """
    current_slugs = {c["slug"] for c in builtin_containers_for(platform_mode)}
    managed = await _load_company_managed(db)
    purge_keys: list[str] = []
    removed, _retired = await _drop_unshipped(db, managed, current_slugs, platform_mode, purge_keys)
    if removed:
        await db.commit()
    await _purge_after_commit(purge_keys)
    return removed


async def _load_company_managed(db: AsyncSession) -> list[TemplateContainer]:
    result = await db.execute(
        select(TemplateContainer).where(TemplateContainer.is_company_managed.is_(True))
    )
    return list(result.scalars().all())


async def _drop_unshipped(
    db: AsyncSession,
    managed: list[TemplateContainer],
    current_slugs: set[str],
    platform_mode: str,
    purge_keys: list[str],
) -> tuple[int, list[tuple[TemplateContainer, str]]]:
    """Delete the built-ins in *managed* that *platform_mode* does not ship.

    Returns the number removed and the retired containers whose assets have an
    heir that ships here (``RETIRED_ASSET_HOMES``): those are only noted, their
    rows move in Phase 3 once the heir is guaranteed to exist, and the emptied
    container goes with them. Scoped to rows carrying BOTH the builtin tag and a
    slug tag, so an admin-created company-managed container is never deleted.
    The deletion takes every asset of the container with it, rows and objects
    (:func:`_delete_container_assets`): the rows here, the object keys appended
    to *purge_keys* for the caller to purge once it committed. Decks, reports and
    videos linked to it keep their content and lose the link (``ON DELETE SET NULL``).
    """
    removed = 0
    retired: list[tuple[TemplateContainer, str]] = []
    for existing in managed:
        slug = _extract_slug(existing)
        if slug is None or slug in current_slugs:
            continue
        if BUILTIN_TAG not in (existing.tags or []):
            continue
        heir_slug = RETIRED_ASSET_HOMES.get(slug)
        if heir_slug is not None and heir_slug in current_slugs:
            retired.append((existing, heir_slug))
            continue
        decks = await _count_rows_bound_to(db, Presentation, existing)
        reports = await _count_rows_bound_to(db, Report, existing)
        logger.info(
            "Removing built-in template container %s, which %s mode does not ship, with "
            "its assets; %d deck(s) and %d report(s) linked to it lose the link "
            "(ON DELETE SET NULL)",
            existing.name,
            platform_mode,
            decks,
            reports,
        )
        purge_keys.extend(await _delete_container_assets(db, existing))
        await db.delete(existing)
        removed += 1
    return removed, retired


async def seed_builtin_template_containers(
    db: AsyncSession, *, platform_mode: str = "copilot"
) -> int:
    """Synchronize the built-in template containers. Returns rows inserted.

    Which containers exist is decided per container by ``platform_targets``
    (``app.agents.seed.seed_matches_platform``): the Filigran brand deck ships
    in Copilot mode only, the XTM One deck in every mode. Switching a
    deployment's mode removes the containers that no longer target it on the
    next startup (Phase 1), same as ``seed_builtin_templates`` removes agent
    templates.

    ``dev`` counts as an XTM One mode, matching every seeder in
    ``app.agents.seed``. It did not originally (it tested ``== "xtm_one"``),
    which left a dev deployment with the built-in Template Designer agent
    but no built-in container for it to duplicate — the one case its
    persona is explicitly taught to handle.
    """
    containers = builtin_containers_for(platform_mode)
    current_slugs = {c["slug"] for c in containers}
    managed = await _load_company_managed(db)
    purge_keys: list[str] = []

    # ── Phase 1: drop built-in containers that are no longer declared ────────
    removed, retired = await _drop_unshipped(db, managed, current_slugs, platform_mode, purge_keys)

    by_slug = {
        slug: container
        for container in managed
        if (slug := _extract_slug(container)) is not None
    }

    # ── Phase 2: upsert ──────────────────────────────────────────────────────
    inserted = 0
    layouts = _build_layouts()
    owner_id = None
    if containers:
        from app.api.v1.helpers import resolve_company_managed_owner_id

        # Company-managed rows keep a real owner (platform invariant); on a
        # brand-new database the admin row may not exist yet, in which case the
        # next startup's sync pass backfills it.
        owner_id = await resolve_company_managed_owner_id(db)

    for data in containers:
        slug = data["slug"]
        tags = [*data["tags"], slug_tag(slug)]
        existing = by_slug.get(slug)
        # The slide layout catalog belongs to decks only: a container of any
        # other kind has nothing to lay out, and advertising layouts on it would
        # invite an agent to pass its id as a deck's template.
        container_layouts = (
            _layouts_for_design(layouts, (data.get("extra_metadata") or {}).get("design"))
            if data["kind"] == PRESENTATION_KIND
            else []
        )

        if existing is not None:
            # Full sync — brand updates must reach existing deployments.
            existing.name = data["name"]
            existing.description = data["description"]
            existing.kind = data["kind"]
            existing.tags = tags
            existing.theme = data["theme"]
            existing.theme_config = data["theme_config"]
            existing.aspect_ratio = data["aspect_ratio"]
            existing.layouts = container_layouts
            existing.extra_metadata = data["extra_metadata"]
            existing.is_company_managed = True
            if existing.user_id is None and owner_id is not None:
                existing.user_id = owner_id
            continue

        created = TemplateContainer(
            user_id=owner_id,
            is_company_managed=True,
            name=data["name"],
            description=data["description"],
            kind=data["kind"],
            tags=tags,
            theme=data["theme"],
            theme_config=data["theme_config"],
            aspect_ratio=data["aspect_ratio"],
            layouts=container_layouts,
            extra_metadata=data["extra_metadata"],
        )
        db.add(created)
        by_slug[slug] = created
        inserted += 1
        logger.info("Provisioning built-in template container: %s", data["name"])

    # ── Phase 3: re-home the assets of retired containers, then drop them ────
    if retired:
        await db.flush()  # a heir inserted just above needs its id
        for container, heir_slug in retired:
            heir = by_slug.get(heir_slug)
            await _lock_container_row(db, container)
            if heir is None:  # pragma: no cover - the slug was declared above
                purge_keys.extend(await _delete_container_assets(db, container))
            else:
                await _rehome_container_assets(db, container, heir)
            if await _retire_emptied_container(db, container):
                removed += 1

    if inserted or removed or containers:
        await db.commit()
    await _purge_after_commit(purge_keys)
    return inserted


async def _count_rows_bound_to(db: AsyncSession, model: type, container: TemplateContainer) -> int:
    """How many rows of *model* still point their ``template_container_id`` at *container*."""
    result = await db.execute(
        select(func.count())
        .select_from(model)
        .where(model.template_container_id == container.id)
    )
    return int(result.scalar_one() or 0)


async def _bound_asset_sources(db: AsyncSession, container: TemplateContainer) -> dict[str, int]:
    """Per-``source`` count of the ``FileAttachment`` rows still bound to *container*.

    Every bound row, whatever its source: that is exactly the set the FK
    cascade would destroy.
    """
    result = await db.execute(
        select(FileAttachment.source, func.count())
        .where(FileAttachment.template_container_id == container.id)
        .group_by(FileAttachment.source)
    )
    return {str(source): int(count or 0) for source, count in result.all()}


async def _lock_container_row(db: AsyncSession, container: TemplateContainer) -> None:
    """Hold a ``FOR UPDATE`` lock on *container*'s row until the transaction ends.

    This is what makes the re-count in :func:`_retire_emptied_container`
    complete rather than merely narrow. An ``INSERT`` into ``file_attachments``
    that references the container takes ``FOR KEY SHARE`` on this row for its
    foreign-key check, and ``FOR UPDATE`` conflicts with it: an insert already
    in flight holds this lock off until it commits or rolls back - and the move
    and the count that follow then see its row - while one arriving later waits
    on its FK check until this transaction ends, and then fails against a
    deleted container (nothing is orphaned) or lands in a kept one. Taken before
    the move and released by the single commit, the lock also covers the gap
    between the count and the ``DELETE``, which the ORM only flushes at commit.
    """
    await db.execute(
        select(TemplateContainer.id)
        .where(TemplateContainer.id == container.id)
        .with_for_update()
    )


async def _retire_emptied_container(db: AsyncSession, container: TemplateContainer) -> bool:
    """Delete a retired built-in once nothing it carried would go down with it.

    Returns whether the row was deleted. ``file_attachments.template_container_id``
    is ``ON DELETE CASCADE``, so an asset row still bound to the container at
    this point would be destroyed together with the only record of its object
    key. The rows are therefore RE-COUNTED here, in the same transaction as the
    move, and any row left keeps the container. The warning promises only what
    the next startup delivers, which is why the count is split by ``source``:

    - a ``template_asset`` row is one written between the move and this count;
      the next startup's re-home moves it and then retires the container;
    - a row of any other source is one the re-home never moves, by design
      (:func:`_rehome_container_assets` moves template assets only), so no
      retry can clear it: the container is kept until an operator detaches or
      deletes that row, and the warning names the sources and the count.

    The caller holds a ``FOR UPDATE`` lock on the container row from before the
    move to the commit (:func:`_lock_container_row`), so the count is complete:
    a row committed after it cannot exist, because an insert referencing the
    container waits on its FK check until this transaction ends and then fails
    against a deleted container or lands in a kept one. No application path
    writes such a row anyway - a built-in refuses asset writes on the REST route
    (403) and in the agent tool alike, and the seeds are serialized by
    ``bootstrap_lock()`` - but the lock is what turns that from an observation
    into a guarantee, out-of-band writes included.

    Decks and reports linked to the container are counted and logged, not moved.
    Their FK is ``ON DELETE SET NULL``: they become free decks, which is what a
    link to a logo library was worth - a container of a non-presentation kind
    dresses no deck (no supported scheme, no chrome logo of its own), and the
    logos a slide placed are referenced by ``FileAttachment`` id, which the
    re-home keeps valid. Re-pointing them at the heir would silently restyle a
    deck in a brand its owner never chose; what they lose is a label.
    """
    bound = await _bound_asset_sources(db, container)
    if bound:
        foreign = {source: count for source, count in bound.items() if source != TEMPLATE_ASSET_SOURCE}
        if foreign:
            logger.warning(
                "Keeping retired built-in template container %s: %d row(s) bound to it "
                "are not template assets (%s); the re-home never moves those and the "
                "delete would cascade to them. Detach them from the container (or "
                "delete them) and the next startup retires it",
                container.name,
                sum(foreign.values()),
                ", ".join(f"{count} {source}" for source, count in sorted(foreign.items())),
            )
        else:
            logger.warning(
                "Keeping retired built-in template container %s: %d template asset "
                "row(s) were bound to it after the re-home (written while it ran); "
                "the next startup moves them and retires it",
                container.name,
                bound[TEMPLATE_ASSET_SOURCE],
            )
        return False
    decks = await _count_rows_bound_to(db, Presentation, container)
    reports = await _count_rows_bound_to(db, Report, container)
    if decks or reports:
        logger.info(
            "Retiring built-in template container %s: %d deck(s) and %d report(s) "
            "linked to it lose the link (ON DELETE SET NULL) and keep rendering "
            "without a template; the file ids they place stay valid",
            container.name,
            decks,
            reports,
        )
    await db.delete(container)
    return True


async def _rehome_container_assets(
    db: AsyncSession, retired: TemplateContainer, heir: TemplateContainer
) -> int:
    """Re-bind every asset row of *retired* to *heir*. Returns rows moved.

    The rows keep their ids and their object keys: a deck that placed one of
    these logos references the ``FileAttachment`` id, and every reader goes
    through the row to the object, so nothing else has to change for the logo
    to keep rendering. ``template_asset_seed`` recognises a moved seeded row by
    the seed tail of its key (``seed/<hash16>-<filename>``), whatever container
    segment the key still carries, so the next asset sync neither re-uploads
    nor deletes it. Rows with no owner inherit the heir's.
    """
    result = await db.execute(
        update(FileAttachment)
        .where(
            FileAttachment.template_container_id == retired.id,
            FileAttachment.source == TEMPLATE_ASSET_SOURCE,
        )
        .values(template_container_id=heir.id)
        .execution_options(synchronize_session=False)
    )
    moved = int(getattr(result, "rowcount", 0) or 0)
    if heir.user_id is not None:
        await db.execute(
            update(FileAttachment)
            .where(
                FileAttachment.template_container_id == heir.id,
                FileAttachment.source == TEMPLATE_ASSET_SOURCE,
                FileAttachment.user_id.is_(None),
            )
            .values(user_id=heir.user_id)
            .execution_options(synchronize_session=False)
        )
    logger.info(
        "Re-homed %d assets of retired built-in template container %s into %s",
        moved,
        retired.name,
        heir.name,
    )
    return moved


async def _delete_container_assets(db: AsyncSession, container: TemplateContainer) -> list[str]:
    """Delete a stale built-in's asset rows; return their object keys.

    The FK cascade would drop the rows anyway; deleting them here is what hands
    back the keys of their objects, so a removed built-in does not leave its
    logos and photos behind in the bucket. The objects themselves are purged by
    the caller once its transaction committed (:func:`_purge_after_commit`):
    never before, since a rollback brings the rows back, and never inside the
    transaction, since the first start after an upgrade removes a whole brand
    deck and an object store that does not answer would hold its locks
    for one timeout per object.
    """
    from app.services.file_cleanup import delete_file_attachment_rows

    return await delete_file_attachment_rows(db, template_container_id=container.id)


async def _purge_after_commit(keys: list[str]) -> None:
    """Purge the objects of the asset rows a committed removal deleted.

    Best-effort and bounded: a storage failure is logged, never raised, and the
    purge gives up at the first sign of an unreachable store - boot must not
    depend on S3 (``file_cleanup.purge_storage_objects``). Whatever it leaves -
    a store down at this start, a process killed after the commit, a delete the
    store refused - the scheduler's hourly
    ``template_asset_seed.purge_orphaned_seed_objects`` finds in the bucket and
    deletes, so a removed built-in's objects never outlive it.
    """
    if not keys:
        return
    from app.services.file_cleanup import purge_storage_objects

    await purge_storage_objects(keys)
